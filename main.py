import os
import sys
import argparse
import logging
import uvicorn
from core.config import get_settings, detect_network_addresses
from core.network_bootstrapper import select_network_ingress, get_active_network_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.ERROR)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.protocol").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("freecord.main")

BANNER = r"""
  _____ ____  _____ _____ ____ ___  ____  ____  
 |  ___|  _ \| ____| ____/ ___/ _ \|  _ \|  _ \ 
 | |_  | |_) |  _| |  _|| |  | | | | |_) | | | |
 |  _| |  _ <| |___| |__| |__| |_| |  _ <| |_| |
 |_|   |_| \_\_____|_____\____\___/|_| \_\____/ 
"""


def print_startup_banner():
    try:
        if sys.platform == "win32":
            os.system("color")
    except Exception:
        pass
    print("\033[96m" + BANNER + "\033[0m")


def print_access_info(net_info: dict):
    print("=" * 65)
    print("  FreeCord Server is Ready & Running!")
    print("=" * 65)
    print(f"  * Dashboard URL:     \033[92m{net_info['recommended_base_url']}\033[0m")
    print(f"  * Localhost:         {net_info['localhost_url']}")
    if net_info.get("lan_url"):
        print(f"  * Local Network:     {net_info['lan_url']}")
    if net_info.get("is_vps") and net_info.get("public_url"):
        print(f"  * VPS Public IP:     {net_info['public_url']}")
    if net_info.get("is_vps"):
        print(f"  * Environment:       \033[92mCloud VPS (Direct Public Access)\033[0m")
    else:
        print(f"  * Environment:       \033[93mHome Network (Tunnel active for Discord joins)\033[0m")
    print("-" * 65)
    print(f"  OAuth2 Redirect URL: \033[94m{net_info['redirect_uri']}\033[0m")
    print(f"  Connection Mode:     {net_info.get('label', net_info.get('method', 'Custom'))}")
    if net_info.get("is_dynamic"):
        print(f"  Link Status:         \033[93mTemporary (changes on restart)\033[0m")
    elif net_info.get("is_semi_permanent") or "loca.lt" in net_info.get("recommended_base_url", ""):
        print(f"  Link Status:         \033[93mSemi Permanent (keeps URL if free)\033[0m")
    elif net_info.get("is_permanent"):
        print(f"  Link Status:         \033[92mFixed URL (never changes)\033[0m")
    elif net_info.get("is_localhost"):
        print(f"  Link Status:         Testing Only (Internal)")
    print("=" * 65)
    print("  Login: Username: admin | Password: admin123")
    print("  \033[93m* Security Note: Please change default admin password in Settings!\033[0m")
    print("=" * 65 + "\n")


def parse_cli_args():
    parser = argparse.ArgumentParser(description="FreeCord Discord Backup & Migration Platform")
    parser.add_argument(
        "--tunnel",
        "-t",
        choices=["auto", "ngrok", "cloudflare", "cf", "localtunnel", "lt", "custom", "localhost", "local", "1", "2", "3", "4", "5"],
        default=None,
        help="Select public connection mode",
    )
    parser.add_argument(
        "--url",
        "-u",
        type=str,
        default=None,
        help="Custom public URL (e.g. https://auth.mydomain.com)",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip interactive network selection prompt",
    )
    return parser.parse_args()


def main():
    args = parse_cli_args()
    settings = get_settings()

    print_startup_banner()

    if args.url:
        os.environ["BASE_URL"] = args.url.strip().rstrip("/")
    elif not settings.BASE_URL or not settings.BASE_URL.strip():
        public_url = select_network_ingress(
            port=settings.PORT,
            cli_choice=args.tunnel,
            custom_url=args.url,
            no_prompt=args.no_prompt,
        )
        if public_url:
            os.environ["BASE_URL"] = public_url
    else:
        print(f"\n  Using configured BASE_URL from environment: {settings.BASE_URL}\n")

    net_info = detect_network_addresses(port=settings.PORT)
    print_access_info(net_info)

    uvicorn.run(
        "web.app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
