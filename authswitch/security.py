"""Windows user-bound encryption and small, atomic file operations."""
from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import tempfile


class AppError(Exception):
    """An error safe to show to the user (never include upstream token payloads)."""


class DPAPI:
    MAGIC = b"AUTHSWITCH-DPAPI-1\n"

    def _crypt(self, data: bytes, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise AppError("首版凭据加密需要 Windows（DPAPI），不支持明文降级。")

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        result = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            fn = crypt32.CryptUnprotectData
            fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
        else:
            fn = crypt32.CryptProtectData
            fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(source), "AuthSwitch", None, None, None, 1, ctypes.byref(result))
        fn.restype = wintypes.BOOL
        if not fn(*args):
            raise AppError("无法加密或解密凭据。请使用保存凭据时的 Windows 用户。")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))

    def encrypt(self, data: bytes) -> bytes:
        return self.MAGIC + self._crypt(data, False)

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(self.MAGIC):
            raise AppError("凭据库格式不支持或已损坏。")
        return self._crypt(data[len(self.MAGIC):], True)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                         capture_output=True, creationflags=flags, check=True)
    sid = next(csv.reader([who.stdout.decode("ascii", errors="replace").strip()]))[-1]
    if not sid.startswith("S-1-"):
        raise AppError("无法确定当前 Windows 用户，未写入凭据。")
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                             f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                            capture_output=True, creationflags=flags)
    if result.returncode:
        raise AppError("无法设置凭据目录权限，未写入凭据。")


def atomic_write(path: Path, data: bytes) -> None:
    """Replace on the same volume; existing contents survive a failed replace."""
    fd, name = tempfile.mkstemp(prefix=".authswitch-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
