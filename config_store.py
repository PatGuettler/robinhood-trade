"""
Encrypted configuration storage for TradeBot.
All sensitive data (API keys, passwords, tokens) is encrypted at rest using
AES-256 via Fernet with PBKDF2HMAC key derivation (480,000 iterations).

Storage layout in ~/.tradebot/:
  salt.bin    — random 16-byte salt (created once, never changes)
  config.enc  — Fernet-encrypted JSON blob of all config values
  meta.json   — non-sensitive flags only (has_claude, has_gmail, etc.)
                readable without the master password for dashboard display
"""

import json
import os
import base64
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

CONFIG_DIR  = Path.home() / ".tradebot"
CONFIG_FILE = CONFIG_DIR / "config.enc"
SALT_FILE   = CONFIG_DIR / "salt.bin"
META_FILE   = CONFIG_DIR / "meta.json"


def _ensure_dir():
    CONFIG_DIR.mkdir(exist_ok=True)


def _get_or_create_salt() -> bytes:
    _ensure_dir()
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = os.urandom(16)
    SALT_FILE.write_bytes(salt)
    return salt


def _derive_key(password: str) -> bytes:
    salt = _get_or_create_salt()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def save_config(data: dict, password: str):
    """Encrypt and save config dict to disk. Also updates meta.json flags."""
    _ensure_dir()
    key = _derive_key(password)
    f = Fernet(key)
    encrypted = f.encrypt(json.dumps(data).encode())
    CONFIG_FILE.write_bytes(encrypted)

    meta = {
        "configured":          True,
        "has_claude":          bool(data.get("claude_api_key")),
        "has_gmail":           bool(data.get("gmail_user") and data.get("gmail_password")),
        "has_alert_email":     bool(data.get("alert_email")),
        "has_robinhood":       bool(data.get("robinhood_configured")),
        "has_trading_rules":   bool(data.get("watchlist")),
        "has_sell_strategy":   bool(data.get("sell_mode")),
    }
    META_FILE.write_text(json.dumps(meta))


def load_config(password: str) -> dict | None:
    """Decrypt and return config. Returns None if password wrong or no config."""
    if not CONFIG_FILE.exists():
        return None
    try:
        key = _derive_key(password)
        f = Fernet(key)
        decrypted = f.decrypt(CONFIG_FILE.read_bytes())
        return json.loads(decrypted.decode())
    except Exception:
        return None


def get_meta() -> dict:
    """Return non-sensitive flags without needing the master password."""
    if not META_FILE.exists():
        return {"configured": False}
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return {"configured": False}


def is_configured() -> bool:
    return CONFIG_FILE.exists()


def delete_config():
    """Wipe all config files (reset)."""
    for f in [CONFIG_FILE, SALT_FILE, META_FILE]:
        if f.exists():
            f.unlink()
