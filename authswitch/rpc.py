"""A short-lived, isolated stdio client; no thread/turn or model request is made."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .security import AppError, atomic_write, json_bytes, private_directory


def worker_env(home: Path) -> dict:
    env = os.environ.copy()
    # Only this child process is changed. Never inherit alternate auth sources.
    for key in list(env):
        if key.upper().startswith(("OPENAI_", "CODEX_")):
            del env[key]
    env["CODEX_HOME"] = str(home)
    return env


@contextmanager
def isolated_home(root: Path, auth: dict | None = None, *, codex_config: bool = True):
    private_directory(root)
    # Inherit the already private parent ACL first. Python 3.13's Windows
    # TemporaryDirectory creates an owner-only ACL that breaks restricted tokens.
    home = root / ("job-" + uuid.uuid4().hex)
    home.mkdir()
    try:
        private_directory(home)
        if codex_config:
            atomic_write(home / "config.toml", b'cli_auth_credentials_store = "file"\n')
        if auth is not None:
            atomic_write(home / "auth.json", json_bytes(auth))
        yield home
    finally:
        resolved, parent = home.resolve(), root.resolve()
        if resolved.parent != parent or home.is_symlink():
            raise AppError("临时目录路径异常，未递归删除。")
        shutil.rmtree(home)


class CodexRPC:
    def __init__(self, binary: str, home: Path, timeout: float = 35):
        self.timeout = timeout
        self.counter = 0
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [binary, "--no-daemon", "app-server", "--listen", "stdio://"],
            cwd=home, env=worker_env(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message: dict):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise AppError("Codex 查询进程已退出，请检查 Codex 版本。") from None

    def call(self, method: str, params: dict | None = None):
        self.counter += 1
        number = self.counter
        self.send({"id": number, "method": method, "params": params or {}})
        end = time.monotonic() + self.timeout
        while True:
            try:
                msg = self.messages.get(timeout=max(0.001, end - time.monotonic()))
            except queue.Empty:
                raise AppError("查询超时，请检查网络后重试。") from None
            if msg is None:
                raise AppError("Codex 查询进程已退出，请检查 Codex 版本。")
            if "method" in msg and "id" in msg:
                # External-token mode must never refresh the active user's tokens.
                self.send({"id": msg["id"], "error": {
                    "code": -32000, "message": "Read-only query; interactive login required"}})
                raise AppError("当前令牌需要更新，请先退出 Codex，再更新登录或重新保存账号。")
            if msg.get("id") != number or "method" in msg:
                if time.monotonic() >= end:
                    raise AppError("查询超时，请稍后重试。")
                continue
            if "error" in msg:
                # Do not print upstream error text: it may include credential material.
                raise AppError(f"Codex 无法完成 {method}（错误码 {msg['error'].get('code', '未知')}）。"
                               "请检查网络、登录状态和 Codex 版本。")
            return msg.get("result", {})

    def __enter__(self):
        try:
            self.call("initialize", {"clientInfo": {"name": "authswitch",
                      "title": "AuthSwitch", "version": "0.1.0"},
                      "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()

    def __exit__(self, *_):
        self.close()
