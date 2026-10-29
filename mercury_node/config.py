"""Where the node keeps its settings, device token, local policy and audit log.

Windows:            %APPDATA%\\Mercury\\node\\
Android (Termux):   ~/.config/mercury-node/
Linux / macOS:      ~/.config/mercury-node/  (or $XDG_CONFIG_HOME)

The device token is the only secret. On Windows it is encrypted with DPAPI, so only
this Windows user on this machine can read it. Elsewhere the file is chmod 600.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "") or Path("/data/data/com.termux").exists()


def platform_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_TERMUX or hasattr(sys, "getandroidapilevel"):
        return "android"
    return "darwin" if sys.platform == "darwin" else "linux"


def node_dir() -> Path:
    override = os.environ.get("MERCURY_NODE_HOME")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Mercury" / "node"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "mercury-node"


def config_path() -> Path:
    return node_dir() / "node.json"


def policy_path() -> Path:
    return node_dir() / "policy.json"


def log_path() -> Path:
    return node_dir() / "tasks.log"


# -- DPAPI (Windows) ---------------------------------------------------------------

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError("Windows DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _seal(token: str) -> dict:
    if IS_WINDOWS:
        return {"token_dpapi": base64.b64encode(_dpapi(token.encode(), True)).decode()}
    return {"token": token}


def _unseal(cfg: dict) -> str:
    if "token_dpapi" in cfg:
        return _dpapi(base64.b64decode(cfg["token_dpapi"]), False).decode()
    return cfg.get("token", "")


# -- node.json ---------------------------------------------------------------------

def load_config() -> dict | None:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cfg["device_token"] = _unseal(cfg)
    return cfg


def save_config(hub_url: str, device_name: str, device_id: str, token: str) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"hub_url": hub_url.rstrip("/"), "device_name": device_name, "device_id": device_id,
            "hostname": platform.node(), **_seal(token)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if not IS_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def delete_config() -> bool:
    try:
        config_path().unlink()
        return True
    except FileNotFoundError:
        return False
