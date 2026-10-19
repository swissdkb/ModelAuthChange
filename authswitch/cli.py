from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys

from .manager import Manager, find_account
from .claude_manager import ClaudeManager
from .providers import codex, claude
from .security import AppError
from .vault import Vault


def clean(value) -> str:
    """Keep account labels and server-provided display text out of terminal control sequences."""
    return "".join(c for c in str(value) if ord(c) >= 32 and ord(c) != 127)


def reset_text(epoch, at: datetime | None = None) -> str:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or not math.isfinite(epoch):
        return "? reset"
    try:
        target = datetime.fromtimestamp(epoch, timezone.utc).astimezone()
        at = (at or datetime.now(timezone.utc)).astimezone()
        fmt = "%H:%M" if target.date() == at.date() else "%m/%d %H:%M"
        if target.year != at.year:
            fmt = "%Y/%m/%d %H:%M"
        text = target.strftime(fmt) + " reset"
        if target <= at:
            text += " · 待刷新"
        return text
    except (ValueError, OverflowError, OSError):
        return "? reset"


def color_enabled() -> bool:
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    # Enable VT sequences only for the attached output console, never redirected output.
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetStdHandle.argtypes = [wintypes.DWORD]
    api.GetStdHandle.restype = wintypes.HANDLE
    api.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = api.GetStdHandle(-11 & 0xFFFFFFFF)
    mode = wintypes.DWORD()
    return bool(api.GetConsoleMode(handle, ctypes.byref(mode))
                and api.SetConsoleMode(handle, mode.value | 4))


def window_text(window: dict, *, color: bool | None = None, at: datetime | None = None) -> str:
    used = window.get("usedPercent")
    if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100:
        remaining = 100 - used
        amount = f"{remaining:g}%"
        use_color = color if color is not None else color_enabled()
        if use_color:
            code = 32 if remaining >= 50 else 33 if remaining >= 20 else 31
            # End the percentage color before the reset time; other text stays white.
            amount = f"\033[{code}m{amount}\033[37m"
    else:
        amount = "--%"
    mins = window.get("windowDurationMins")
    if isinstance(mins, (int, float)) and not isinstance(mins, bool) and math.isfinite(mins) and mins > 0:
        duration = f"{mins / 1440:g}d" if mins % 1440 == 0 else (
            f"{mins / 60:g}h" if mins % 60 == 0 else f"{mins:g}m")
    else:
        duration = "?"
    return f"{duration} {amount} ({reset_text(window.get('resetsAt'), at)})"


def show_usage(usage: dict | None, *, cached: bool = False):
    if not usage:
        print("  用量：尚未查询")
        return
    if usage.get("error"):
        print("  查询失败：" + clean(usage["error"]))
    cache_note = ""
    if cached or usage.get("error"):
        cache_note = "旧缓存" if usage.get("error") else "缓存"
        try:
            checked = datetime.fromisoformat(usage.get("checked_at", "")).astimezone()
            cache_note += checked.strftime(" %m/%d %H:%M")
        except (ValueError, TypeError):
            pass
        cache_note = " [" + cache_note + "]"
    payload = usage.get("payload") or {}
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = payload.get("rateLimits")
        buckets = {"codex": single} if isinstance(single, dict) else {}
    count = 0
    use_color = color_enabled()
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        label = clean(bucket.get("limitName") or key)
        windows = []
        for field in ("primary", "secondary"):
            window = bucket.get(field)
            if isinstance(window, dict):
                windows.append(window_text(window, color=use_color))
                count += 1
        if windows:
            prefix = "" if len(buckets) == 1 and key in ("codex", "claude") else label + " | "
            line = prefix + " | ".join(windows) + cache_note
            if use_color:
                line = "\033[37m" + line + "\033[0m"
            print("  " + line)
    if not count and not usage.get("error"):
        print("  服务未返回额度窗口（不推断为无限额度）。")


def public_account(account: dict, current: bool) -> dict:
    return {"id": account["id"], "name": account["name"], "service": account["service"],
            "email": account["identity"].get("email"), "current": current,
            "needs_refresh": not bool(account["auth"].get("claudeAiOauth", {}).get("accessToken")
                                      if account["service"] == "claude"
                                      else account["auth"]["tokens"].get("access_token")),
            "saved_at": account["saved_at"], "usage": account.get("usage")}


def list_accounts(manager: Manager, as_json: bool = False, numbered: bool = False,
                  refresh: bool = False):
    doc = manager.vault.read()
    failures = {}
    if refresh:
        # Query each displayed account once. Failures must not hide the account list.
        for account in doc["accounts"]:
            print("查询 " + clean(account["name"]) + " …", file=sys.stderr, flush=True)
            try:
                manager.usage(account["id"])
            except AppError as error:
                failures[account["id"]] = str(error)
        doc = manager.vault.read()
        # Some failures (e.g. missing executable) happen before usage() writes cache.
        for account in doc["accounts"]:
            if account["id"] in failures:
                account["usage"] = {**(account.get("usage") or {}),
                                    "error": failures[account["id"]]}
    warning = None
    try:
        _, live = manager.live()
    except AppError as error:
        live, warning = None, str(error)
    public = [public_account(a, bool(live and a["identity"]["key"] == live["key"]))
              for a in doc["accounts"]]
    if as_json:
        print(json.dumps({"service": manager.service, "accounts": public, "warning": warning},
                         ensure_ascii=False, indent=2))
        return public
    if warning:
        print("当前认证状态：" + warning)
    if not public:
        print("还没有保存账号。使用 save 名称 " + manager.service + " 保存当前登录，或 login 添加账号。")
    for number, item in enumerate(public, 1):
        status = " ← 当前" if item["current"] else ""
        if item["needs_refresh"]:
            status += " [导入凭据待补全]"
        prefix = f"{number}. " if numbered else ""
        print(f"\n{prefix}{clean(item['name'])} [{item['id']}]{status}")
        print("  登录：" + clean(item["email"] or "未提供邮箱"))
        show_usage(item["usage"], cached=not refresh)
    return public


def switch_from_list(manager: Manager, refresh: bool = False) -> int:
    accounts = list_accounts(manager, numbered=True, refresh=refresh)
    if not accounts:
        return 0
    while True:
        choice = input("\n输入账号编号切换，回车或 0 退出：").strip()
        if choice.lower() in ("", "0", "q"):
            return 0
        try:
            index = int(choice) - 1
        except ValueError:
            index = -1
        if not 0 <= index < len(accounts):
            print(f"请输入 1–{len(accounts)} 的账号编号，或 0 退出。")
            continue
        # Pass the ID from the displayed snapshot, never re-resolve a list index.
        account = manager.switch(accounts[index]["id"])
        print("已切换到 " + clean(account["name"]) + "。重新运行 " + manager.resume_command + " 选择原来的对话。")
        return 0


def doctor(manager: Manager):
    if manager.service == "claude":
        print("AuthSwitch 0.2.0 · Claude Code 官方订阅")
        print("Claude 配置目录：", manager.home)
        print("Claude 账号身份文件：", manager.config)
        print("加密凭据目录：", manager.vault.root)
        try:
            print("Claude 可执行文件：", claude.find_binary(manager.explicit_binary))
            _, ident = manager.live()
            print("本机订阅认证：", "存在" if ident else "未找到")
        except AppError as error:
            print(str(error))
        print("已保存账号：", len(manager.vault.read()["accounts"]))
        found = codex.running_clients("claude")
        print("运行中的客户端：", "、".join(found) if found else "无")
        print("此命令只检查，不更改配置或登录状态。")
        return
    print("AuthSwitch 0.2.0 · Codex 官方订阅")
    print("Codex 配置目录：", manager.home)
    print("加密凭据目录：", manager.vault.root)
    print("凭据存储方式：", clean(codex.storage_mode(manager.home)))
    try:
        print("Codex 可执行文件：", codex.find_binary(manager.explicit_binary))
    except AppError as error:
        print(str(error))
    try:
        _, ident = manager.live()
        print("本机订阅认证：", "存在" if ident else "未找到")
    except AppError as error:
        print("本机订阅认证：", str(error))
    print("已保存账号：", len(manager.vault.read()["accounts"]))
    found = codex.running_clients()
    print("运行中的客户端：", "、".join(found) if found else "无")
    print("此命令只检查，不更改配置或登录状态。")


def query(manager: Manager, name: str | None, all_accounts: bool):
    doc = manager.vault.read()
    if all_accounts:
        names = [a["name"] for a in doc["accounts"]]
    elif name:
        names = [find_account(doc, name)["name"]]
    else:
        _, ident = manager.live()
        current = next((a for a in doc["accounts"]
                        if ident and a["identity"]["key"] == ident["key"]), None)
        if current is None:
            raise AppError("请指定账号名称，或先 save 保存当前账号。")
        names = [current["name"]]
    if not names:
        raise AppError("没有可查询的账号，请先保存或导入。")
    failures = 0
    for name in names:
        print("\n查询 " + clean(name) + " …", flush=True)
        try:
            show_usage(manager.usage(name))
        except AppError as error:
            failures += 1
            print("  " + str(error))
            old = find_account(manager.vault.read(), name).get("usage")
            if old and old.get("payload"):
                show_usage(old)
    return 1 if failures else 0


def menu(manager: Manager):
    while True:
        print("\nAuthSwitch · " + manager.label + " 官方订阅账号")
        list_accounts(manager)
        print("\n1 切换账号    2 刷新全部用量    3 保存当前账号")
        if manager.service == "codex":
            print("4 从 CC Switch 导入    5 官方登录添加/更新    6 检查环境")
        else:
            print("5 官方登录添加/更新    6 检查环境")
        print("7 刷新账号凭据    8 重命名账号    9 调整顺序    0 退出")
        choice = input("请选择：").strip()
        try:
            if choice == "0":
                return 0
            if choice == "1":
                switch_from_list(manager, refresh=True)
            elif choice == "2":
                query(manager, None, True)
            elif choice == "3":
                account = manager.snapshot(input("账号名称：").strip())
                print("已保存 " + clean(account["name"]))
            elif choice == "4":
                source = Path.home() / ".cc-switch"
                count, skipped = manager.import_ccswitch(source)
                print(f"已导入 {count} 个账号，跳过已存在的 {skipped} 个账号；原文件未修改。")
            elif choice == "5":
                account = manager.login(input("账号名称：").strip())
                print("已保存 " + clean(account["name"]) + "；使用切换功能激活。")
            elif choice == "6":
                doctor(manager)
            elif choice == "7":
                account = manager.refresh(input("账号名称或 ID：").strip())
                print("已刷新 " + clean(account["name"]) + "；使用切换功能激活。")
            elif choice == "8":
                manager.rename(input("原名称或 ID：").strip(), input("新名称：").strip())
                print("账号已重命名。")
            elif choice == "9":
                list_accounts(manager, numbered=True)
                selectors = input("按期望顺序输入账号编号（空格分隔，例如 2 1）：").split()
                if selectors:
                    show_order(manager.reorder(selectors))
        except AppError as error:
            print("操作未完成：" + str(error))


def parser(service: str = "codex", *, combined_help: bool = False) -> argparse.ArgumentParser:
    label = "Claude Code" if service == "claude" else "Codex"
    description = "AuthSwitch：Codex / Claude Code 官方订阅账号认证切换" if combined_help else f"AuthSwitch：{label} 官方订阅账号认证切换"
    p = argparse.ArgumentParser(prog="auth", description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="服务名放最后：[codex|claude]；省略时默认 codex。\n\n"
               "常用示例：\n"
               "  auth list                  查看 Codex 账号与用量\n"
               "  auth list claude           查看 Claude 账号与用量\n"
               "  auth use main codex        切换 Codex 账号\n"
               "  auth use main claude       切换 Claude 账号\n"
               "  auth order main backup     调整 Codex 列表顺序\n"
               "  auth usage --all claude    查询 Claude 全部账号用量\n\n"
               "帮助统一展示两个服务：auth list --help / auth use --help\n"
               "仅 Codex：import-ccswitch、login --device-auth。\n"
               "refresh：Codex 刷新令牌；Claude 通过官方浏览器重新授权；之后均需 use 激活。")
    p.add_argument("--data-dir", type=Path, help="加密凭据目录，默认 %%LOCALAPPDATA%%/AuthSwitch")
    if service == "codex" or combined_help:
        p.add_argument("--codex-home", type=Path, help="Codex：共用配置目录")
        p.add_argument("--codex-bin", help="Codex：真正的 codex.exe 路径")
    if service == "claude" or combined_help:
        p.add_argument("--claude-home", type=Path, help="Claude：共用配置目录")
        p.add_argument("--claude-config", type=Path, help="Claude：账号身份文件路径（.claude.json）")
        p.add_argument("--claude-bin", help="Claude：真正的 claude.exe 路径")
    sub = p.add_subparsers(dest="command")
    examples = {"doctor": "doctor", "list": "list", "save": "save main", "use": "use main",
                "login": "login backup", "refresh": "refresh main", "rename": "rename main work",
                "usage": "usage --all", "order": "order main backup"}

    def add_command(name, **kwargs):
        example = examples.get(name)
        if example:
            extra = ""
            if name == "login":
                extra = "\n--device-auth 仅 Codex 支持；Claude 使用官方浏览器登录。"
            elif name == "refresh":
                extra = "\nCodex：刷新保存的令牌。Claude：官方浏览器重新授权。之后均需 use 激活。"
            kwargs["epilog"] = (f"示例：\n  auth {example} codex\n  auth {example} claude\n"
                                "服务名放最后，省略时默认 codex。" + extra)
        else:
            kwargs["epilog"] = "仅 Codex 支持：auth import-ccswitch [选项] codex"
        return sub.add_parser(name, formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs)

    add_command("doctor", help="只读检查环境")
    ls = add_command("list", help="刷新一次所有账号用量，在终端输入编号切换")
    ls.add_argument("--json", action="store_true", help="只输出公开元数据，不输出令牌")
    ls.add_argument("--no-interactive", action="store_true", help="只查看列表，不提示切换")
    ls.add_argument("--cached", action="store_true", help="只显示缓存，不联网查询")
    save = add_command("save", help="保存当前订阅认证，不修改当前登录")
    save.add_argument("name")
    if service == "codex" or combined_help:
        imp = add_command("import-ccswitch", help="仅 Codex：导入 CC Switch 订阅账号")
        imp.add_argument("--source", type=Path, default=Path.home() / ".cc-switch")
        imp.add_argument("--dry-run", action="store_true", help="只预览可导入账号，不写凭据库")
    use = add_command("use", help="退出所选服务的客户端后切换认证，保留对话和设置")
    use.add_argument("name", help="账号编号、名称、唯一名称前缀或 ID")
    login = add_command("login", help="在隔离目录完成官方登录，保存后可 use 激活")
    login.add_argument("name")
    if service == "codex" or combined_help:
        login.add_argument("--device-auth", action="store_true", help="仅 Codex：官方设备码登录")
    refresh = add_command("refresh", help="Codex 刷新令牌 / Claude 浏览器重新授权，之后 use 激活")
    refresh.add_argument("name")
    rename = add_command("rename", help="修改账号名称")
    rename.add_argument("name")
    rename.add_argument("new_name")
    order = add_command("order", help="调整列表顺序和编号，不切换账号")
    order.add_argument("names", nargs="+", help="按期望顺序填写账号别名、编号或 ID；未填写的账号依次排在后面")
    usage = add_command("usage", help="查询额度和重置时间，不切换当前账号")
    group = usage.add_mutually_exclusive_group()
    group.add_argument("name", nargs="?")
    group.add_argument("--all", action="store_true")
    return p


def service_args(argv: list[str]) -> tuple[str, list[str]]:
    """Strip the reserved trailing selector BEFORE parsing, including --help."""
    if argv and argv[-1].casefold() in ("codex", "claude"):
        return argv[-1].casefold(), argv[:-1]
    return "codex", argv


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    service, arguments = service_args(list(sys.argv[1:] if argv is None else argv))
    # Help is service-independent and exits before touching any auth/vault files.
    command_parser = parser(service, combined_help=any(arg in ("-h", "--help") for arg in arguments))
    args = command_parser.parse_args(arguments)
    root = args.data_dir or Path(os.environ.get("AUTHSWITCH_HOME") or
                   str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AuthSwitch"))
    if service == "claude":
        home = args.claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    else:
        home = args.codex_home or Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    root, home = root.expanduser().resolve(), home.expanduser().resolve()
    if root == home or root in home.parents or home in root.parents:
        raise AppError("凭据库目录和 Codex 配置目录必须彼此独立，不能互相包含。")
    if service == "claude":
        # Leave the original Codex vault in place; Claude has an independent sub-vault.
        config = args.claude_config or (home / ".claude.json" if args.claude_home or os.environ.get("CLAUDE_CONFIG_DIR")
                                       else Path.home() / ".claude.json")
        config = config.expanduser().resolve()
        if config == root or root in config.parents or config == home / ".credentials.json":
            raise AppError("Claude 账号身份文件不能指向凭据库或令牌文件。")
        manager = ClaudeManager(Vault(root / "claude"), home, config, args.claude_bin)
    else:
        manager = Manager(Vault(root), home, args.codex_bin)
    if args.command is None:
        if not sys.stdin.isatty():
            parser(service, combined_help=True).print_help()
            return 0
        return menu(manager)
    if args.command == "doctor":
        doctor(manager)
    elif args.command == "list":
        if not args.json and not args.no_interactive and sys.stdin.isatty() and sys.stdout.isatty():
            return switch_from_list(manager, refresh=not args.cached)
        list_accounts(manager, args.json, numbered=True, refresh=not args.cached)
    elif args.command == "save":
        account = manager.snapshot(args.name)
        print("已加密保存：" + clean(account["name"]) + "；当前认证未修改。")
    elif args.command == "import-ccswitch":
        if args.dry_run:
            incoming = codex.imported_accounts(args.source)
            print(f"可导入 {len(incoming)} 个 Codex 官方订阅账号；未写入任何文件。")
            for entry in incoming:
                print("  " + clean(entry["label"]))
        else:
            added, skipped = manager.import_ccswitch(args.source)
            print(f"已导入 {added} 个账号；跳过已存在的 {skipped} 个账号。CC Switch 原文件未修改。")
    elif args.command == "use":
        account = manager.switch(args.name)
        print("已切换到 " + clean(account["name"]) + "。重新运行 " + manager.resume_command + " 选择原来的对话。")
    elif args.command == "login":
        account = manager.login(args.name, getattr(args, "device_auth", False))
        print("已保存 " + clean(account["name"]) + "；使用 use 激活。")
    elif args.command == "refresh":
        account = manager.refresh(args.name)
        print("已刷新 " + clean(account["name"]) + " 的保存凭据；使用 use 激活。")
    elif args.command == "rename":
        manager.rename(args.name, args.new_name)
        print("账号已重命名。")
    elif args.command == "order":
        show_order(manager.reorder(args.names))
    elif args.command == "usage":
        return query(manager, args.name, args.all)
    return 0


def show_order(accounts: list[dict]):
    print("已保存列表顺序（未切换账号）：")
    for number, account in enumerate(accounts, 1):
        print(f"  {number}. {clean(account['name'])}")


def entrypoint():
    try:
        return main()
    except (AppError, PermissionError) as error:
        print("操作未完成：" + (str(error) if isinstance(error, AppError)
              else "没有所需的文件访问权限，未绕过系统权限。"), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"系统操作失败（{type(error).__name__}），请运行 doctor 检查路径和权限。", file=sys.stderr)
        return 1
