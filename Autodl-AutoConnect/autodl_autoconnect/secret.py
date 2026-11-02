"""Token 的本地保存。

要求：
- 绝不明文落盘（Windows 用 DPAPI 加密；其它平台用「用户主目录 + 0600 权限 + 警告」）；
- 任何读出来的 token 都不进日志、不进异常消息；
- 支持环境变量 ``AUTODL_TOKEN`` 覆盖，方便 CI / Agent 临时注入。
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from .config import state_dir
from .errors import ConfigError

__all__ = ["save_token", "load_token", "token_path", "describe_storage", "TOKEN_ENV"]

TOKEN_ENV = "AUTODL_TOKEN"


def token_path() -> Path:
    return state_dir() / "token"


def _dpapi_protect(data: bytes) -> bytes | None:
    """Windows DPAPI 加密（当前用户作用域）。非 Windows 或失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.c_char_p))
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        buffer = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return buffer
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.c_char_p))
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_token(token: str, path: str | os.PathLike | None = None) -> Path:
    """保存 token。优先使用 Windows DPAPI。"""
    token = (token or "").strip()
    if not token:
        raise ConfigError("token 不能为空")
    target = Path(path) if path else token_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    protected = _dpapi_protect(token.encode("utf-8"))
    if protected is not None:
        target.write_bytes(b"dpapi:" + base64.b64encode(protected))
        _restrict_permissions(target)
        return target

    # 非 Windows：退化为 base64 + 0600，并留下醒目的说明文件。
    target.write_text(
        base64.b64encode(token.encode("utf-8")).decode("ascii"), encoding="ascii"
    )
    _restrict_permissions(target)
    (target.parent / "README-安全提示.txt").write_text(
        "本目录保存着 AutoDL 开发者 Token。\n"
        "当前平台没有可用的系统级加密接口，Token 以 base64 存放（仅是编码，不是加密）。\n"
        "请确认本目录权限仅本人可读；更安全的做法是改用环境变量 AUTODL_TOKEN，\n"
        "或直接在每次运行时通过 --token 传入。\n",
        encoding="utf-8",
    )
    return target


def load_token(path: str | os.PathLike | None = None) -> str:
    """读取 token：环境变量优先，其次本地文件。"""
    env_value = os.environ.get(TOKEN_ENV, "").strip()
    if env_value:
        return env_value
    target = Path(path) if path else token_path()
    if not target.exists():
        return ""
    raw = target.read_bytes()
    if raw.startswith(b"dpapi:"):
        decrypted = _dpapi_unprotect(base64.b64decode(raw[6:]))
        if decrypted is None:
            raise ConfigError(
                "无法用当前 Windows 用户解密已保存的 Token（可能换了账号或机器）。"
                f"请重新执行登录：{target}"
            )
        return decrypted.decode("utf-8").strip()
    try:
        return base64.b64decode(raw.decode("ascii").strip()).decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"本地 Token 文件已损坏：{target}（{exc}）") from exc


def describe_storage(path: str | os.PathLike | None = None) -> str:
    """描述当前 Token 存储方式（给 ``doctor`` 命令用）。"""
    target = Path(path) if path else token_path()
    if os.environ.get(TOKEN_ENV, "").strip():
        return f"环境变量 {TOKEN_ENV}"
    if not target.exists():
        return "未配置"
    if target.read_bytes().startswith(b"dpapi:"):
        return f"Windows DPAPI 加密文件 {target}"
    return f"base64 文件（未加密）{target}"


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
