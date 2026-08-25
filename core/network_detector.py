import os
import sys
import socket
import logging
import ipaddress
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger("freecord.network")


def is_public_ip(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_global and not addr.is_private and not addr.is_loopback and not addr.is_link_local
    except ValueError:
        return False


def is_cgnat(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return addr in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def is_residential(ip: str) -> bool:
    if not ip:
        return True
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local or is_cgnat(ip)
    except ValueError:
        return True


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip.startswith("169.254."):
            return "127.0.0.1"
        return ip
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_public_ip() -> Optional[str]:
    services = [
        "https://api.ipify.org?format=json",
        "https://api64.ipify.org?format=json",
        "https://icanhazip.com",
    ]
    for url in services:
        try:
            with httpx.Client(timeout=2.5) as client:
                res = client.get(url)
                if res.status_code == 200:
                    if "json" in res.headers.get("content-type", ""):
                        ip = res.json().get("ip")
                    else:
                        ip = res.text.strip()
                    if ip and is_public_ip(ip):
                        return ip
        except Exception:
            continue
    return None


def is_vps() -> bool:
    lan = get_lan_ip()
    if is_public_ip(lan):
        return True

    markers = [
        "/var/lib/cloud",
        "/etc/cloud",
        "C:\\Program Files\\Amazon\\SSM",
        "C:\\Program Files\\Cloudbase Solutions",
    ]
    for m in markers:
        if os.path.exists(m):
            return True

    return False


def get_ngrok_url() -> Optional[str]:
    try:
        with httpx.Client(timeout=1.5) as client:
            res = client.get("http://127.0.0.1:4040/api/tunnels")
            if res.status_code == 200:
                data = res.json()
                for t in data.get("tunnels", []):
                    u = t.get("public_url", "")
                    if u:
                        return u
    except Exception:
        pass
    return None


get_ngrok_tunnel_url = get_ngrok_url


def get_network_info(port: int = 8000) -> Dict[str, Any]:
    from core.network_bootstrapper import get_active_network_state

    state = get_active_network_state()
    custom_url = os.getenv("BASE_URL") or os.getenv("PUBLIC_URL")
    pub = get_public_ip()
    lan = get_lan_ip()
    on_vps = is_vps()
    cgnat_flag = is_cgnat(lan) if lan else False
    residential = not on_vps

    if custom_url and custom_url.strip():
        host = custom_url.strip().rstrip("/")
        method = state.get("label") or "Custom Link"
    else:
        ng = get_ngrok_url()
        if ng:
            host = ng.rstrip("/")
            method = "Ngrok Tunnel"
        elif on_vps and pub:
            host = f"http://{pub}:{port}"
            method = "Cloud VPS"
        else:
            host = state.get("base_url") or f"http://localhost:{port}"
            method = state.get("label") or "Localhost"

    redirect_url = f"{host}/api/oauth/callback"

    is_dyn = state.get("is_dynamic", False) or "trycloudflare" in host
    is_semi = state.get("is_semi_permanent", False) or "loca.lt" in host
    is_local = state.get("is_localhost", False) or "localhost" in host or "127.0.0.1" in host
    is_perm = state.get("is_permanent", False) or (not is_dyn and not is_semi and not is_local)

    return {
        "port": port,
        "recommended_base_url": host,
        "redirect_uri": redirect_url,
        "redirect_url": redirect_url,
        "method": method,
        "public_ip": pub,
        "lan_ip": lan,
        "localhost_url": f"http://localhost:{port}",
        "is_vps": on_vps,
        "is_cgnat": cgnat_flag,
        "is_residential": residential,
        "is_dynamic": is_dyn,
        "is_semi_permanent": is_semi,
        "is_permanent": is_perm,
        "is_localhost": is_local,
        "warning": state.get("warning"),
        "label": state.get("label", method),
    }

