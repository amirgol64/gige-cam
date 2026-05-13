import json
import os
import hashlib
import secrets
import tempfile
from pathlib import Path

SETTINGS_PATH = Path("/data/config/settings.json")

DEFAULTS = {
    "network": {
        "receiver_ip": "192.168.2.5",
        "receiver_port": 5000
    },
    "camera": {
        "width": 1640,
        "height": 1232,
        "framerate": 30,
        "exposure": "auto",
        "exposure_value": 10000,
        "gain": 1.0,
        "white_balance": "auto",
        "rotation": 0,
        "hflip": False,
        "vflip": False,
        "night_mode": False
    },
    "stream": {
        "codec": "h264",
        "bitrate_kbps": 6000,
        "gop": 15,
        "profile": "baseline"
    },
    "auth": {
        "password_hash": None,
        "password_salt": None,
        "first_login": True
    },
    "ui": {
        "sysinfo_refresh_s": 5
    }
}

HOSTNAME_FILE = "/etc/hostname"


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r") as f:
            on_disk = json.load(f)
        return _deep_merge(DEFAULTS, on_disk)
    except Exception:
        return dict(DEFAULTS)


def save(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SETTINGS_PATH)
        dir_fd = os.open(str(SETTINGS_PATH.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return secrets.compare_digest(h, stored_hash)


def get_hostname() -> str:
    try:
        return Path(HOSTNAME_FILE).read_text().strip()
    except Exception:
        return "GigE-Cam"
