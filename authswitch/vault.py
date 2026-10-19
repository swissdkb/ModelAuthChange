from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

from .security import AppError, DPAPI, atomic_write, json_bytes, private_directory


class Vault:
    def __init__(self, root: Path, cipher=None):
        self.root = root
        self.path = root / "accounts.dat"
        self.cipher = cipher or DPAPI()

    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "accounts": []}
        try:
            value = json.loads(self.cipher.decrypt(self.path.read_bytes()))
            if value.get("version") != 1 or not isinstance(value.get("accounts"), list):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AppError("凭据库内容已损坏，未执行操作。") from None

    def write(self, value: dict) -> None:
        atomic_write(self.path, self.cipher.encrypt(json_bytes(value)))

    @contextmanager
    def locked(self):
        private_directory(self.root)
        lock = self.root / "vault.lock"
        with lock.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AppError("另一个 AuthSwitch 正在操作，请稍后重试。") from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
