import os
import socket
import secrets
from pathlib import Path
from typing import Dict, Any, Optional
import httpx
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
BACKUPS_DIR = BASE_DIR / "backups"
BACKUPS_DIR.mkdir(exist_ok=True)
ENV_FILE = BASE_DIR / ".env"


def _ensure_env_file():
    if not ENV_FILE.exists():
        encryption_key = Fernet.generate_key().decode()
        jwt_secret = secrets.token_urlsafe(32)
        admin_pass = "admin123"
        content = f"""HOST=0.0.0.0
PORT=8000
BASE_URL=
DATABASE_URL=sqlite+aiosqlite:///data/freecord.db
ENCRYPTION_KEY={encryption_key}
JWT_SECRET={jwt_secret}
ADMIN_USERNAME=admin
ADMIN_PASSWORD={admin_pass}
ENVIRONMENT=production
DEBUG=false
"""
        ENV_FILE.write_text(content.strip(), encoding="utf-8")


_ensure_env_file()


class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    BASE_URL: Optional[str] = None
    DATABASE_URL: str = "sqlite+aiosqlite:///data/freecord.db"
    ENCRYPTION_KEY: str = ""
    JWT_SECRET: str = ""
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    ENVIRONMENT: str = "production"
    DEBUG: bool = False

    IPQUALITYSCORE_API_KEY: Optional[str] = None
    PROXYCHECK_API_KEY: Optional[str] = None
    TURNSTILE_SITE_KEY: Optional[str] = None
    TURNSTILE_SECRET_KEY: Optional[str] = None
    HCAPTCHA_SITE_KEY: Optional[str] = None
    HCAPTCHA_SECRET_KEY: Optional[str] = None

    DISCORD_OAUTH_ENABLED: bool = False
    DISCORD_CLIENT_ID: Optional[str] = None
    DISCORD_CLIENT_SECRET: Optional[str] = None

    class Config:
        env_file = str(ENV_FILE)
        extra = "ignore"


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
        if not _settings_instance.ENCRYPTION_KEY:
            _settings_instance.ENCRYPTION_KEY = Fernet.generate_key().decode()
        if not _settings_instance.JWT_SECRET:
            _settings_instance.JWT_SECRET = secrets.token_urlsafe(32)
    return _settings_instance


def detect_network_addresses(port: int = 8000) -> Dict[str, Any]:
    from core.network_bootstrapper import get_active_network_state
    from core.network_detector import get_network_info
    
    active_state = get_active_network_state()
    info = get_network_info(port=port)
    settings = get_settings()

    if settings.BASE_URL and settings.BASE_URL.strip():
        recommended_host = settings.BASE_URL.strip().rstrip("/")
    elif active_state.get("base_url") and not active_state.get("is_localhost", False):
        recommended_host = active_state["base_url"].strip().rstrip("/")
    else:
        recommended_host = info["recommended_base_url"]

    redirect_uri = f"{recommended_host}/api/oauth/callback"

    return {
        "port": port,
        "localhost_url": f"http://localhost:{port}",
        "lan_url": f"http://{info['lan_ip']}:{port}" if info.get("lan_ip") and info["lan_ip"] != "127.0.0.1" else None,
        "public_url": f"http://{info['public_ip']}:{port}" if info.get("public_ip") and info.get("is_vps") else None,
        "public_ip": info.get("public_ip"),
        "recommended_base_url": recommended_host,
        "redirect_uri": redirect_uri,
        "redirect_url": redirect_uri,
        "method": info.get("method", "Auto"),
        "is_vps": info.get("is_vps", False),
        "is_cgnat": info.get("is_cgnat", False),
        "is_residential": info.get("is_residential", True),
        "is_dynamic": info.get("is_dynamic", False),
        "is_semi_permanent": info.get("is_semi_permanent", False),
        "is_permanent": info.get("is_permanent", False),
        "is_localhost": info.get("is_localhost", False),
        "warning": info.get("warning"),
        "label": info.get("label", info.get("method", "Connection")),
    }

