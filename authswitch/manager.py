from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from .providers import codex
from .rpc import CodexRPC, isolated_home, worker_env
from .security import AppError, atomic_write, json_bytes
from .vault import Vault


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def find_account(doc: dict, name: str) -> dict:
    matches = [a for a in doc["accounts"] if a["name"] == name or a["id"] == name]
    if not matches:
        try:
            index = int(name) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(doc["accounts"]):
            return doc["accounts"][index]
        prefix = name.casefold()
        if prefix and not name.lstrip("+-").isdigit():
            matches = [a for a in doc["accounts"] if a["name"].casefold().startswith(prefix)]
    if len(matches) > 1:
        labels = "、".join(a["name"] for a in matches)
        raise AppError("账号前缀匹配多个账号：" + labels + "。请补全名称或使用编号。")
    if len(matches) != 1:
        raise AppError("找不到唯一匹配的账号，请先运行 list 查看编号、名称或 ID。")
    return matches[0]


def checked_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise AppError("账号名称需为 1–80 个可显示字符。")
    if name.casefold() in ("codex", "claude"):
        raise AppError("codex 和 claude 是末尾服务标识，请选择其他账号别名。")
    return name


def upsert(doc: dict, name: str, auth: dict, source: str, *, provider=codex, service="codex") -> dict:
    name = checked_name(name)
    ident = provider.identity(auth)
    by_name = next((a for a in doc["accounts"] if a["name"] == name), None)
    by_key = next((a for a in doc["accounts"] if a["identity"]["key"] == ident["key"]), None)
    if by_name and by_name["identity"]["key"] != ident["key"]:
        raise AppError("这个名称已属于另一个账号，请使用不同名称。")
    account = by_key
    if account is None:
        account = {"id": uuid.uuid4().hex[:12], "name": name, "service": service,
                   "created_at": now()}
        doc["accounts"].append(account)
    account.update(identity=ident, auth=copy.deepcopy(auth), saved_at=now(), source=source)
    return account


def superseded(account: dict, auth: dict | None) -> bool:
    return bool(auth and account.get("superseded_live_hash") == fingerprint(json_bytes(auth))
                and account["auth"] != auth)


class Manager:
    service = "codex"
    label = "Codex"
    resume_command = "codex resume"

    def __init__(self, vault: Vault, codex_home: Path, binary: str | None = None,
                 idle_check=None):
        self.vault = vault
        self.home = codex_home
        self.explicit_binary = binary
        self.idle_check = idle_check or codex.ensure_idle

    def live(self):
        codex.require_file_storage(self.home)
        auth = codex.read_auth(self.home)
        return (auth, codex.identity(auth)) if auth else (None, None)

    def snapshot(self, name: str) -> dict:
        with self.vault.locked():
            doc = self.vault.read()
            auth, _ = self.live()
            if auth is None:
                raise AppError("尚未找到本机 Codex 的订阅认证。")
            if not auth["tokens"].get("access_token"):
                raise AppError("当前认证缺少访问令牌，请先完成官方登录。")
            saved = next((a for a in doc["accounts"]
                          if a["identity"]["key"] == codex.identity(auth)["key"]), None)
            if saved and superseded(saved, auth):
                raise AppError("该账号已有更新的登录凭据待应用，请先 use 切换，避免保存旧凭据。")
            account = upsert(doc, name, auth, "current")
            self.vault.write(doc)
            return account

    def import_ccswitch(self, source: Path) -> tuple[int, int]:
        # Parse and validate everything before making a single vault write.
        incoming = codex.imported_accounts(source)
        with self.vault.locked():
            doc = self.vault.read()
            added = skipped = 0
            for entry in incoming:
                if any(a["identity"]["key"] == entry["identity"]["key"] for a in doc["accounts"]):
                    skipped += 1
                    continue  # A repeated import must not restore an obsolete refresh token.
                name = entry["label"]
                if any(a["name"] == name for a in doc["accounts"]):
                    name += "-" + entry["identity"]["key"][:6]
                upsert(doc, name, entry["auth"], "ccswitch")
                added += 1
            self.vault.write(doc)
            return added, skipped

    def login(self, name: str, device: bool = False) -> dict:
        checked_name(name)
        self.idle_check()
        binary = codex.find_binary(self.explicit_binary)
        with self.vault.locked():
            doc = self.vault.read()
            with isolated_home(self.vault.root / "jobs") as home:
                command = [binary, "--no-daemon", "login"]
                if device:
                    command.append("--device-auth")
                result = subprocess.run(command, cwd=home, env=worker_env(home))
                if result.returncode:
                    raise AppError("官方登录未完成，原来的认证未修改。")
                auth = codex.read_auth(home)
                if auth is None:
                    raise AppError("登录后未找到订阅认证，原来的认证未修改。")
                account = upsert(doc, name, auth, "login")
                live_auth, live_ident = self.live()
                if live_ident and live_ident["key"] == account["identity"]["key"]:
                    account["superseded_live_hash"] = fingerprint(json_bytes(live_auth))
                self.vault.write(doc)
                return account

    def _prepared_auth(self, account: dict, doc: dict, force: bool = False) -> dict:
        """Materialize refresh-only CC Switch entries in a private Codex home."""
        auth = account["auth"]
        if auth["tokens"].get("access_token") and not force:
            return auth
        binary = codex.find_binary(self.explicit_binary)
        with isolated_home(self.vault.root / "jobs", auth) as home:
            try:
                with CodexRPC(binary, home) as rpc:
                    rpc.call("account/read", {"refreshToken": True})
            finally:
                self._save_refreshed(home, account, doc)
        auth = account["auth"]
        if not auth["tokens"].get("access_token"):
            raise AppError("该导入账号未能补全访问令牌，请使用 login 重新授权。")
        return auth

    def _save_refreshed(self, home: Path, account: dict, doc: dict):
        auth = codex.read_auth(home)
        if auth and codex.identity(auth)["key"] == account["identity"]["key"]:
            account["auth"] = auth
            account["saved_at"] = now()
            self.vault.write(doc)

    def switch(self, name: str, *, require_idle: bool = False) -> dict:
        if require_idle:
            self.idle_check()
        with self.vault.locked():
            doc = self.vault.read()
            target = find_account(doc, name)
            path = self.home / "auth.json"
            original = path.read_bytes() if path.exists() else None
            auth, ident = self.live()
            if (path.read_bytes() if path.exists() else None) != original:
                raise AppError("操作期间本机认证已变化，已停止切换。请重新执行。")
            if ident and ident["key"] == target["identity"]["key"] and not superseded(target, auth):
                target["auth"] = auth
                target["saved_at"] = now()
                self.vault.write(doc)
                return target
            if auth:
                previous = next((a for a in doc["accounts"]
                                 if a["identity"]["key"] == ident["key"]), None)
                if not previous or not superseded(previous, auth):
                    label = previous["name"] if previous else "原账号-" + ident["key"][:8]
                    upsert(doc, label, auth, "current")
            # Persist the outgoing, potentially refreshed tokens BEFORE replacing live auth.
            doc["recovery"] = {"before": original.decode("utf-8-sig") if original else None,
                               "before_hash": fingerprint(original),
                               "target_id": target["id"], "saved_at": now()}
            self.vault.write(doc)
            prepared = self._prepared_auth(target, doc)
            if require_idle:
                self.idle_check()
            current = path.read_bytes() if path.exists() else None
            if current != original:
                raise AppError("操作期间本机认证已变化，已停止切换。请重新执行。")
            self.home.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json_bytes(prepared))
            # No post-swap vault write is necessary; current account is detected from live identity.
            return target

    def refresh(self, name: str) -> dict:
        """Refresh an account in isolation. Applying it to the shared home remains explicit."""
        self.idle_check()
        with self.vault.locked():
            doc = self.vault.read()
            account = find_account(doc, name)
            auth, ident = self.live()
            if ident and ident["key"] == account["identity"]["key"]:
                if not superseded(account, auth):
                    account["auth"] = auth
                account["superseded_live_hash"] = fingerprint(json_bytes(auth))
                self.vault.write(doc)
            self._prepared_auth(account, doc, force=True)
            return account

    def rename(self, name: str, new_name: str):
        new_name = checked_name(new_name)
        with self.vault.locked():
            doc = self.vault.read()
            account = find_account(doc, name)
            if any(a["name"] == new_name and a["id"] != account["id"] for a in doc["accounts"]):
                raise AppError("这个名称已被使用。")
            account["name"] = new_name
            self.vault.write(doc)

    def reorder(self, selectors: list[str]) -> list[dict]:
        """Move selected accounts to the front, resolving all against the original order."""
        if not selectors:
            raise AppError("请指定账号顺序，例如 order main backup，或 order 2 1。")
        with self.vault.locked():
            doc = self.vault.read()
            selected = [find_account(doc, selector) for selector in selectors]
            ids = [account["id"] for account in selected]
            if len(ids) != len(set(ids)):
                raise AppError("同一个账号不能重复出现在顺序中，未修改列表。")
            selected_ids = set(ids)
            doc["accounts"] = selected + [a for a in doc["accounts"] if a["id"] not in selected_ids]
            self.vault.write(doc)
            return doc["accounts"]

    def usage(self, name: str) -> dict:
        binary = codex.find_binary(self.explicit_binary)
        with self.vault.locked():
            doc = self.vault.read()
            account = find_account(doc, name)
            auth, ident = self.live()
            active = bool(ident and ident["key"] == account["identity"]["key"])
            credential = auth if active and not superseded(account, auth) else account["auth"]
            access = credential["tokens"].get("access_token")
            expiry = codex.jwt_claims(access or "").get("exp")
            expired = isinstance(expiry, (int, float)) and expiry <= time.time() + 30
            managed = not access or (expired and not active)
            if expired and active:
                raise AppError("当前账号访问令牌已过期。请先退出客户端，执行 refresh 账号，再 use 账号。")
            if managed:
                # CC Switch might otherwise keep and later replay an older refresh token.
                self.idle_check()
            try:
                with isolated_home(self.vault.root / "jobs", credential if managed else None) as home:
                    try:
                        with CodexRPC(binary, home) as rpc:
                            if managed:
                                rpc.call("account/read", {"refreshToken": True})
                            else:
                                rpc.call("account/login/start", {
                                    "type": "chatgptAuthTokens",
                                    "accessToken": credential["tokens"]["access_token"],
                                    "chatgptAccountId": credential["tokens"]["account_id"]})
                            payload = rpc.call("account/rateLimits/read")
                    finally:
                        if managed:
                            self._save_refreshed(home, account, doc)
                account["usage"] = {"checked_at": now(), "payload": payload, "error": None}
                self.vault.write(doc)
                return account["usage"]
            except AppError as error:
                old = account.get("usage", {})
                account["usage"] = {**old, "error": str(error), "failed_at": now()}
                self.vault.write(doc)
                raise
