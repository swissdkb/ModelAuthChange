from __future__ import annotations

import copy
import base64
from pathlib import Path
import subprocess

from .manager import Manager, checked_name, find_account, fingerprint, now, superseded, upsert
from .providers import claude
from .rpc import isolated_home
from .security import AppError, atomic_write, json_bytes
from .vault import Vault


class ClaudeIdentityMismatch(AppError):
    """Known token/metadata mismatch; never eligible for automatic account saving."""


class ClaudeManager(Manager):
    """Separate vault, shared Claude home. Never invoke a model for auth or usage."""
    service = "claude"
    label = "Claude Code"
    resume_command = "claude --resume"

    def __init__(self, vault: Vault, home: Path, config: Path, binary: str | None = None,
                 idle_check=None):
        self.vault = vault
        self.home = home
        self.config = config
        self.explicit_binary = binary
        self.idle_check = idle_check or claude.ensure_idle

    def live(self):
        auth = claude.read_auth(self.home, self.config)
        if auth is None:
            return None, None
        ident = claude.identity(auth)
        # Claude stores tokens and identity in separate files. An old client's
        # delayed write can pair one user's tokens with another user's metadata.
        # Reject known mismatches rather than saving those tokens under the wrong user.
        for saved in self.vault.read()["accounts"]:
            if saved["identity"]["account_id"] == ident["account_id"]:
                continue
            previous = saved["auth"].get("claudeAiOauth", {})
            if any(auth["claudeAiOauth"].get(field) and auth["claudeAiOauth"].get(field) == previous.get(field)
                   for field in ("accessToken", "refreshToken")):
                raise ClaudeIdentityMismatch("Claude 令牌与账号身份不一致，可能有旧会话写回了认证。"
                                            "请执行 use 目标账号 claude 重新应用；未保存混合凭据。")
        return auth, ident

    def _store(self, doc: dict, name: str, auth: dict, source: str):
        return upsert(doc, name, auth, source, provider=claude, service="claude")

    def snapshot(self, name: str) -> dict:
        with self.vault.locked():
            doc = self.vault.read()
            auth, ident = self.live()
            if auth is None:
                raise AppError("未找到 Claude Code 官方订阅认证，请先完成登录。")
            saved = next((a for a in doc["accounts"] if a["identity"]["key"] == ident["key"]), None)
            if saved and superseded(saved, auth):
                raise AppError("该账号已有更新的登录凭据待应用，请先 use 账号 claude。")
            account = self._store(doc, name, auth, "current")
            self.vault.write(doc)
            return account

    def import_ccswitch(self, source: Path):
        raise AppError("CC Switch 导入仅支持 Codex；Claude 请用 save 或 login 添加。")

    def login(self, name: str, device: bool = False) -> dict:
        checked_name(name)
        if device:
            raise AppError("Claude 不支持此工具的 --device-auth；请使用官方浏览器登录。")
        self.idle_check()
        binary = claude.find_binary(self.explicit_binary)
        with self.vault.locked():
            doc = self.vault.read()
            with isolated_home(self.vault.root / "jobs", codex_config=False) as home:
                env = claude.worker_env(home)
                for folder in (home / "appdata", home / "localappdata"):
                    folder.mkdir()
                result = subprocess.run([binary, "auth", "login", "--claudeai"], cwd=home, env=env)
                if result.returncode:
                    raise AppError("Claude 官方登录未完成，当前登录文件未修改。")
                auth = claude.read_auth(home, home / ".claude.json")
                if auth is None:
                    raise AppError("隔离登录后未找到完整 Claude 认证，当前登录文件未修改。")
                account = self._store(doc, name, auth, "login")
                live_auth, live_ident = self.live()
                if live_ident and live_ident["key"] == account["identity"]["key"]:
                    account["superseded_live_hash"] = fingerprint(json_bytes(live_auth))
                self.vault.write(doc)
                return account

    def refresh(self, name: str) -> dict:
        # No private OAuth refresh implementation: use the official browser flow.
        account = find_account(self.vault.read(), name)
        return self.login(account["name"])

    def switch(self, name: str, *, require_idle: bool = False) -> dict:
        if require_idle:
            self.idle_check()
        with self.vault.locked():
            doc = self.vault.read()
            target = find_account(doc, name)
            claude.validate_auth(target["auth"])
            credential_path = self.home / ".credentials.json"
            paths = (self.config, credential_path)
            originals = [p.read_bytes() if p.exists() else None for p in paths]
            mixed_identity = False
            try:
                auth, ident = self.live()
            except ClaudeIdentityMismatch:
                # Repair from a known saved account without auto-saving a mixed pair.
                auth, ident = None, None
                mixed_identity = True
            if [p.read_bytes() if p.exists() else None for p in paths] != originals:
                raise AppError("操作期间 Claude 文件已变化，已停止切换。")
            if ident and ident["key"] == target["identity"]["key"] and not superseded(target, auth):
                target.update(auth=auth, saved_at=now())
                self.vault.write(doc)
                return target
            metadata = claude.read_object(self.config)
            credentials = claude.read_object(credential_path)
            if auth:
                previous = next((a for a in doc["accounts"] if a["identity"]["key"] == ident["key"]), None)
                if previous is None or not superseded(previous, auth):
                    self._store(doc, previous["name"] if previous else "原账号-" + ident["key"][:8],
                                auth, "current")
            # Backup BOTH files losslessly, including a possible UTF-8 BOM.
            doc["recovery"] = {"files": [
                {"path": str(p), "before_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None,
                 "before_hash": fingerprint(raw)} for p, raw in zip(paths, originals)],
                "target_id": target["id"], "saved_at": now(), "mixed_identity": mixed_identity}
            self.vault.write(doc)
            metadata["oauthAccount"] = copy.deepcopy(target["auth"]["oauthAccount"])
            credentials["claudeAiOauth"] = copy.deepcopy(target["auth"]["claudeAiOauth"])
            replacements = [json_bytes(metadata), json_bytes(credentials)]
            if require_idle:
                self.idle_check()
            for path, original in zip(paths, originals):
                if (path.read_bytes() if path.exists() else None) != original:
                    raise AppError("操作期间 Claude 文件已变化，已停止切换。")
            written = []
            try:
                for path, original, replacement in zip(paths, originals, replacements):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if (path.read_bytes() if path.exists() else None) != original:
                        raise AppError("操作期间 Claude 文件已变化，已停止切换。")
                    atomic_write(path, replacement)
                    written.append((path, original, replacement))
            except BaseException:
                rollback_failed = False
                for path, original, replacement in reversed(written):
                    try:
                        # Do not roll back over someone else's concurrent changes.
                        if path.read_bytes() != replacement:
                            rollback_failed = True
                            continue
                        if original is None:
                            path.unlink()
                        else:
                            atomic_write(path, original)
                    except OSError:
                        rollback_failed = True
                if rollback_failed:
                    raise AppError("Claude 切换中止且回滚未完成。原文件已保存在加密库 recovery 中；"
                                   "请勿启动 Claude，先检查文件权限或外部修改。") from None
                raise
            return target

    def usage(self, name: str) -> dict:
        with self.vault.locked():
            doc = self.vault.read()
            account = find_account(doc, name)
            try:
                auth, ident = self.live()
                active = bool(ident and ident["key"] == account["identity"]["key"])
                credential = auth if active and not superseded(account, auth) else account["auth"]
                payload = claude.query_usage(credential)
                account["usage"] = {"checked_at": now(), "payload": payload, "error": None}
                self.vault.write(doc)
                return account["usage"]
            except AppError as error:
                account["usage"] = {**(account.get("usage") or {}), "error": str(error), "failed_at": now()}
                self.vault.write(doc)
                raise
