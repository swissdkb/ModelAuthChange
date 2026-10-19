from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tomllib

from ..security import AppError


def jwt_claims(token: str) -> dict:
    # Only display/identity metadata; this is not signature verification.
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def validate_auth(auth: dict) -> dict:
    if not isinstance(auth, dict) or auth.get("auth_mode") not in (None, "chatgpt"):
        raise AppError("只支持 ChatGPT 官方订阅登录凭据。")
    if auth.get("OPENAI_API_KEY"):
        raise AppError("该文件不是官方订阅认证。")
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise AppError("未找到订阅令牌；可能仍由 CC Switch 接管。")
    for field in ("account_id", "id_token", "refresh_token"):
        if not isinstance(tokens.get(field), str) or not tokens[field]:
            raise AppError("订阅认证不完整，缺少账号身份或刷新令牌。")
    if not isinstance(tokens.get("access_token", ""), str):
        raise AppError("访问令牌格式不正确。")
    return auth


def identity(auth: dict) -> dict:
    validate_auth(auth)
    tokens = auth["tokens"]
    claims = jwt_claims(tokens["id_token"])
    profile = claims.get("https://api.openai.com/profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    email = claims.get("email") or profile.get("email") or ""
    subject = claims.get("sub") or email
    if not isinstance(subject, str) or not subject or not isinstance(email, str):
        raise AppError("无法识别订阅登录的用户身份，请重新登录后保存。")
    key = hashlib.sha256((tokens["account_id"] + "\0" + subject).encode()).hexdigest()
    return {"key": key, "email": email, "account_id": tokens["account_id"]}


def read_auth(home: Path) -> dict | None:
    path = home / "auth.json"
    if not path.exists():
        return None
    try:
        return validate_auth(json.loads(path.read_text(encoding="utf-8-sig")))
    except (ValueError, TypeError):
        raise AppError("Codex auth.json 无法解析，未执行操作。") from None


def storage_mode(home: Path) -> str:
    path = home / "config.toml"
    if not path.exists():
        return "file"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, TypeError):
        raise AppError("Codex config.toml 无法解析。") from None
    return config.get("cli_auth_credentials_store", "file")


def require_file_storage(home: Path) -> None:
    if storage_mode(home) != "file":
        raise AppError("当前 Codex 使用 keyring/auto/ephemeral 存储；首版只支持 file。"
                       "不会自动修改你的 config.toml。")


def running_clients(service: str = "codex") -> list[str]:
    if os.name != "nt":
        raise AppError("首版进程检查仅支持 Windows。")
    # Toolhelp does not depend on the WMI/tasklist service or localized output.
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.Process32FirstW.restype = wintypes.BOOL
    api.Process32NextW.argtypes = api.Process32FirstW.argtypes
    api.Process32NextW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise AppError("无法检查运行进程，未执行操作。")
    found = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = api.Process32FirstW(handle, ctypes.byref(entry))
        if not valid:
            raise AppError("无法检查运行进程，未执行操作。")
        while valid:
            name = entry.name.lower()
            shared = name in ("cc-switch.exe", "ccswitch.exe")
            match = (name in ("codex.exe", "codex") or name.startswith("codex-code-mode-host")) if service == "codex" else name in ("claude.exe", "claude")
            if shared or match:
                found.append(f"{entry.name} (PID {entry.pid})")
            valid = api.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise AppError("进程列表未读取完整，未执行操作。")
    finally:
        api.CloseHandle(handle)
    return found


def ensure_idle() -> None:
    found = running_clients()
    if found:
        raise AppError("请先退出 Codex（含桌面端/IDE 会话）和 CC Switch，再操作：\n  "
                       + "\n  ".join(found))


def find_binary(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".exe":
            return str(path)
        raise AppError("--codex-bin 需要指向真正的 codex.exe。")
    path = shutil.which("codex.exe")
    if path:
        return path
    # npm's .cmd launcher is avoided: do not invoke a shell with user arguments.
    npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex"
    candidates = sorted(npm.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
    if candidates:
        return str(candidates[0])
    raise AppError("未找到 codex.exe，请使用 --codex-bin 指定可执行文件。")


def imported_accounts(source: Path) -> list[dict]:
    """Read CC Switch OAuth export; never touch its SQLite DB or write to it."""
    if source.is_dir():
        source = source / "codex_oauth_auth.json"
    try:
        doc = json.loads(source.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        raise AppError("无法读取 CC Switch 的 codex_oauth_auth.json。") from None
    if not isinstance(doc, dict):
        raise AppError("CC Switch 订阅文件格式不支持。")
    values = doc.get("accounts", {})
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list) or not values:
        raise AppError("CC Switch 中没有可导入的订阅账号。")
    result = []
    for entry in values:
        try:
            # CC Switch keeps refresh/id tokens but may omit the access token.
            account_id = entry.get("chatgpt_account_id") or entry.get("account_id")
            millis = entry.get("token_updated_at_ms")
            refreshed = (datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
                         if isinstance(millis, (int, float)) else "1970-01-01T00:00:00Z")
            auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                    "tokens": {"account_id": account_id,
                               "access_token": entry.get("access_token") or "",
                               "refresh_token": entry.get("refresh_token"),
                               "id_token": entry.get("id_token")}, "last_refresh": refreshed}
            ident = identity(auth)
            result.append({"identity": ident, "auth": auth,
                           "label": entry.get("email") or ident["email"] or "Codex"})
        except (AttributeError, ValueError, OverflowError, AppError):
            raise AppError("CC Switch 中存在格式不支持或不完整的账号，未导入任何账号。") from None
    return result
