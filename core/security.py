import base64
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple
import httpx
from cryptography.fernet import Fernet
import bcrypt
from jose import jwt, JWTError

from core.config import get_settings

logger = logging.getLogger("freecord.security")

ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7


def get_fernet() -> Fernet:
    key = get_settings().ENCRYPTION_KEY
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_secret(plain: Optional[str]) -> Optional[str]:
    if not plain:
        return None
    return get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted: Optional[str]) -> Optional[str]:
    if not encrypted:
        return None
    try:
        return get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return None


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8")[:72], salt).decode("utf-8")


get_password_hash = hash_password


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    cfg = get_settings()
    payload = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES))
    payload["exp"] = expire
    return jwt.encode(payload, cfg.JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    cfg = get_settings()
    try:
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        return None


async def check_ip(ip: str) -> Dict[str, Any]:
    cfg = get_settings()

    if ip in ("127.0.0.1", "::1", "localhost") or ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")):
        return {
            "ip": ip,
            "is_vpn": False,
            "is_proxy": False,
            "is_datacenter": False,
            "is_cellular": False,
            "country": "Local Network",
            "country_code": "LOC",
            "city": "Private LAN",
            "isp": "Localhost / Private Network",
            "asn": "AS0000",
            "fraud_score": 0,
        }

    if cfg.IPQUALITYSCORE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                url = f"https://ipqualityscore.com/api/json/ip/{cfg.IPQUALITYSCORE_API_KEY}/{ip}"
                res = await client.get(url)
                if res.status_code == 200:
                    d = res.json()
                    if d.get("success", False):
                        return {
                            "ip": ip,
                            "is_vpn": d.get("vpn", False),
                            "is_proxy": d.get("proxy", False),
                            "is_datacenter": d.get("is_crawler", False) or d.get("bot_status", False),
                            "is_cellular": d.get("mobile", False),
                            "country": d.get("country_code", "Unknown"),
                            "country_code": d.get("country_code", "XX"),
                            "city": d.get("city", "Unknown"),
                            "isp": d.get("ISP", "Unknown"),
                            "asn": f"AS{d.get('ASN', 0)}",
                            "fraud_score": d.get("fraud_score", 0),
                        }
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            url = f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,mobile,proxy,hosting,query"
            res = await client.get(url)
            if res.status_code == 200:
                d = res.json()
                if d.get("status") == "success":
                    is_proxy = d.get("proxy", False)
                    is_hosting = d.get("hosting", False)
                    is_mobile = d.get("mobile", False)
                    is_vpn = is_proxy or is_hosting
                    return {
                        "ip": ip,
                        "is_vpn": is_vpn,
                        "is_proxy": is_proxy,
                        "is_datacenter": is_hosting,
                        "is_cellular": is_mobile,
                        "country": d.get("country", "Unknown"),
                        "country_code": d.get("countryCode", "XX"),
                        "city": d.get("city", "Unknown"),
                        "region": d.get("regionName", "Unknown"),
                        "zip": d.get("zip", ""),
                        "lat": d.get("lat"),
                        "lon": d.get("lon"),
                        "timezone": d.get("timezone", "UTC"),
                        "isp": d.get("isp", "Unknown"),
                        "org": d.get("org", "Unknown"),
                        "asn": d.get("as", "Unknown"),
                        "fraud_score": 85 if is_vpn else (10 if is_mobile else 0),
                    }
    except Exception:
        pass

    return {
        "ip": ip,
        "is_vpn": False,
        "is_proxy": False,
        "is_datacenter": False,
        "is_cellular": False,
        "country": "Unknown",
        "country_code": "XX",
        "city": "Unknown",
        "region": "Unknown",
        "zip": "",
        "lat": None,
        "lon": None,
        "timezone": "UTC",
        "isp": "Unknown",
        "org": "Unknown",
        "asn": "Unknown",
        "fraud_score": 0,
    }


def parse_user_agent(user_agent: Optional[str]) -> Dict[str, str]:
    if not user_agent:
        return {"os": "Unknown OS", "browser": "Unknown Browser", "device": "Desktop"}
    ua = user_agent.lower()

    if "windows nt 10.0" in ua:
        os_name = "Windows 10/11"
        device_type = "Desktop"
    elif "windows nt 6.3" in ua:
        os_name = "Windows 8.1"
        device_type = "Desktop"
    elif "windows nt 6.1" in ua:
        os_name = "Windows 7"
        device_type = "Desktop"
    elif "windows" in ua:
        os_name = "Windows"
        device_type = "Desktop"
    elif "iphone" in ua:
        os_name = "iOS (iPhone)"
        device_type = "Mobile"
    elif "ipad" in ua:
        os_name = "iPadOS"
        device_type = "Tablet"
    elif "macintosh" in ua or "mac os x" in ua:
        os_name = "macOS"
        device_type = "Desktop"
    elif "android" in ua:
        os_name = "Android"
        device_type = "Mobile"
    elif "linux" in ua:
        os_name = "Linux"
        device_type = "Desktop"
    else:
        os_name = "Unknown OS"
        device_type = "Desktop"

    if "edg/" in ua:
        browser = "Microsoft Edge"
    elif "opr/" in ua or "opera" in ua:
        browser = "Opera"
    elif "chrome/" in ua:
        browser = "Google Chrome"
    elif "firefox/" in ua:
        browser = "Mozilla Firefox"
    elif "safari/" in ua and "chrome" not in ua:
        browser = "Apple Safari"
    else:
        browser = "Web Browser"

    return {
        "os": os_name,
        "browser": browser,
        "device": device_type,
    }


def account_age_days(user_id: str) -> float:
    try:
        snowflake = int(user_id)
        # Discord epoch is 2015-01-01T00:00:00.000Z (1420070400000ms)
        created_ms = (snowflake >> 22) + 1420070400000
        created_dt = datetime.utcfromtimestamp(created_ms / 1000.0)
        return max(0.0, (datetime.utcnow() - created_dt).total_seconds() / 86400.0)
    except Exception:
        return 9999.0


get_accage_days = account_age_days
inspect_ip_address = check_ip


async def verify_turnstile(token: str, remote_ip: Optional[str] = None, secret_key: Optional[str] = None) -> Tuple[bool, str]:
    cfg = get_settings()
    sec = secret_key or cfg.TURNSTILE_SECRET_KEY
    if not sec:
        return True, "Turnstile not configured"

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            data = {"secret": sec, "response": token}
            if remote_ip:
                data["remoteip"] = remote_ip
            res = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
            out = res.json()
            if out.get("success", False):
                return True, "Turnstile verified successfully"
            return False, f"Turnstile failed: {out.get('error-codes', [])}"
    except Exception as e:
        return False, f"Turnstile error: {str(e)}"


async def verify_hcaptcha(token: str, remote_ip: Optional[str] = None, secret_key: Optional[str] = None) -> Tuple[bool, str]:
    cfg = get_settings()
    sec = secret_key or cfg.HCAPTCHA_SECRET_KEY
    if not sec:
        return True, "hCaptcha not configured"

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            data = {"secret": sec, "response": token}
            if remote_ip:
                data["remoteip"] = remote_ip
            res = await client.post("https://hcaptcha.com/siteverify", data=data)
            out = res.json()
            if out.get("success", False):
                return True, "hCaptcha verified successfully"
            return False, f"hCaptcha failed: {out.get('error-codes', [])}"
    except Exception as e:
        return False, f"hCaptcha error: {str(e)}"

