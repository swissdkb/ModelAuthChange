"""Windows Claude Code subscription credentials and read-only usage adapter."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request

from .codex import running_clients
from ..security import AppError

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def read_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise AppError(f"Claude {path.name} 无法解析，未执行操作。") from None


def validate_auth(auth: dict) -> dict:
    if not isinstance(auth, dict):
        raise AppError("Claude 认证格式不支持。")
    oauth = auth.get("claudeAiOauth")
    account = auth.get("oauthAccount")
    if not isinstance(oauth, dict) or not isinstance(account, dict):
        raise AppError("Claude 缺少订阅令牌或账号身份，请先完成官方订阅登录。")
    for key in ("accessToken", "refreshToken"):
        if not isinstance(oauth.get(key), str) or not oauth[key].strip():
            raise AppError("Claude 订阅认证不完整，请重新登录。")
    for key in ("accountUuid", "organizationUuid"):
        if not isinstance(account.get(key), str) or not account[key]:
            raise AppError("Claude 缺少用户或组织标识，请重新登录。")
    email = account.get("emailAddress")
    if email is not None and not isinstance(email, str):
        raise AppError("Claude 账号邮箱格式不支持。")
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list) or "user:inference" not in scopes:
        raise AppError("这不是可用于 Claude Code 的官方订阅认证。")
    return auth


def identity(auth: dict) -> dict:
    validate_auth(auth)
    account = auth["oauthAccount"]
    key = hashlib.sha256(("claude\0" + account["organizationUuid"] + "\0" + account["accountUuid"]).encode()).hexdigest()
    return {"key": key, "email": account.get("emailAddress") or "",
            "account_id": account["accountUuid"], "organization_id": account["organizationUuid"]}


def read_auth(home: Path, config: Path) -> dict | None:
    # Read twice: snapshotting a running client must not mix two refresh generations.
    paths = (home / ".credentials.json", config)
    before = tuple(p.read_bytes() if p.exists() else None for p in paths)
    credentials, metadata = (read_object(p) for p in paths)
    if not credentials.get("claudeAiOauth"):
        return None
    auth = {"claudeAiOauth": copy.deepcopy(credentials["claudeAiOauth"]),
            "oauthAccount": copy.deepcopy(metadata.get("oauthAccount"))}
    validate_auth(auth)
    after = tuple(p.read_bytes() if p.exists() else None for p in paths)
    if before != after:
        raise AppError("Claude 认证正在更新，请稍后重新保存。")
    return auth


def ensure_idle():
    found = running_clients("claude")
    if found:
        raise AppError("请先退出 Claude Code（含 IDE 会话）和 CC Switch，再操作：\n  " + "\n  ".join(found))


def find_binary(explicit: str | None = None) -> str:
    if explicit:
        binary = Path(explicit).expanduser().resolve()
        if binary.is_file() and binary.suffix.lower() == ".exe":
            return str(binary)
        raise AppError("--claude-bin 需要指向真正的 claude.exe。")
    binary = shutil.which("claude.exe")
    if binary:
        return binary
    binary = Path.home() / ".local/bin/claude.exe"
    if binary.is_file():
        return str(binary)
    raise AppError("未找到 claude.exe，请使用 --claude-bin 指定。")


def worker_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("CLAUDE_", "ANTHROPIC_"))}
    env.update(CLAUDE_CONFIG_DIR=str(home), USERPROFILE=str(home), HOME=str(home),
               APPDATA=str(home / "appdata"), LOCALAPPDATA=str(home / "localappdata"))
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # Never forward a subscription bearer token to a redirected host.
        return None


def normalize_usage(value: dict) -> dict:
    """Adapt Claude's variable buckets to the shared compact quota renderer."""
    if not isinstance(value, dict):
        raise AppError("Claude 用量返回格式不支持。")
    buckets = {}
    for key, minutes in (("five_hour", 300), ("seven_day", 10080)):
        raw = value.get(key)
        if isinstance(raw, dict):
            buckets.setdefault("claude", {"limitId": "claude"})[
                "primary" if key == "five_hour" else "secondary"] = usage_window(raw, minutes)
    for key, raw in value.items():
        if key.startswith("seven_day_") and isinstance(raw, dict):
            label = key.removeprefix("seven_day_").replace("_", " ")
            buckets[key] = {"limitId": key, "limitName": label,
                            "primary": usage_window(raw, 10080)}
    if not buckets:
        raise AppError("Claude 未返回可识别的订阅额度窗口，不推断为无限额度。")
    return {"rateLimitsByLimitId": buckets}


def usage_window(raw: dict, minutes: int) -> dict:
    used = raw.get("utilization")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 100:
        used = None
    reset = raw.get("resets_at")
    epoch = None
    if isinstance(reset, str):
        try:
            parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                epoch = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": epoch}


def query_usage(auth: dict) -> dict:
    validate_auth(auth)
    expiry = auth["claudeAiOauth"].get("expiresAt")
    if isinstance(expiry, (int, float)) and expiry <= time.time() * 1000 + 30000:
        raise AppError("Claude 访问令牌已过期；先让原会话更新后 save，或退出后 login 重新授权。")
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + auth["claudeAiOauth"]["accessToken"],
        "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
        "User-Agent": "AuthSwitch/0.2.0"}, method="GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AppError("Claude 用量返回过大，已停止读取。")
        return normalize_usage(json.loads(raw))
    except urllib.error.HTTPError as error:
        messages = {401: "Claude 登录已失效，请重新授权。", 403: "Claude 拒绝用量查询，请在原生 /usage 中查看。",
                    429: "Claude 用量查询过于频繁，请稍后重试。"}
        raise AppError(messages.get(error.code, f"Claude 用量查询失败（HTTP {error.code}）。")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AppError("Claude 用量查询网络失败，请稍后重试。") from None
    except (ValueError, TypeError):
        raise AppError("Claude 用量返回无法解析，原认证未修改。") from None
