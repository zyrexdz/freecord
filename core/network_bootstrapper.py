import os
import re
import sys
import time
import shutil
import random
import queue
import threading
import urllib.request
import zipfile
import subprocess
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("freecord.network")

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "data" / "bin"
BIN_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = BASE_DIR / ".env"

_active_network_state: Dict[str, Any] = {
    "type": "localhost",
    "base_url": "http://localhost:8000",
    "redirect_uri": "http://localhost:8000/api/oauth/callback",
    "is_permanent": False,
    "is_dynamic": False,
    "is_localhost": True,
    "warning": None,
    "label": "Localhost Only",
}


def get_active_network_state() -> Dict[str, Any]:
    return _active_network_state


def set_active_network_state(
    net_type: str,
    base_url: str,
    is_permanent: bool = False,
    is_dynamic: bool = False,
    is_localhost: bool = False,
    warning: Optional[str] = None,
    label: str = "Custom Link",
):
    clean_url = base_url.strip().rstrip("/")
    if clean_url and not clean_url.startswith("http"):
        clean_url = f"https://{clean_url}"

    _active_network_state.update({
        "type": net_type,
        "base_url": clean_url,
        "redirect_uri": f"{clean_url}/api/oauth/callback",
        "is_permanent": is_permanent,
        "is_dynamic": is_dynamic,
        "is_localhost": is_localhost,
        "warning": warning,
        "label": label,
    })
    os.environ["BASE_URL"] = clean_url


def update_env_variable(key: str, value: str):
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def get_env_variable(key: str) -> Optional[str]:
    if not ENV_FILE.exists():
        return os.getenv(key)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip().startswith(f"{key}="):
            val = line.split("=", 1)[1].strip()
            return val if val else None
    return os.getenv(key)


def get_cloudflared_path() -> str:
    which_path = shutil.which("cloudflared")
    if which_path:
        return which_path

    target_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local_bin = BIN_DIR / target_name
    if local_bin.exists():
        return str(local_bin)

    print("cloudflared not found. Downloading binary from Cloudflare...")
    if sys.platform == "win32":
        download_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    elif sys.platform == "darwin":
        download_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    else:
        download_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    try:
        urllib.request.urlretrieve(download_url, str(local_bin))
        if sys.platform != "win32":
            os.chmod(str(local_bin), 0o755)
        print("Downloaded cloudflared.")
        return str(local_bin)
    except Exception as e:
        print(f"Failed to auto-download cloudflared: {e}")
        return "cloudflared"


def get_ngrok_path() -> str:
    which_path = shutil.which("ngrok")
    if which_path:
        return which_path

    target_name = "ngrok.exe" if sys.platform == "win32" else "ngrok"
    local_bin = BIN_DIR / target_name
    if local_bin.exists():
        return str(local_bin)

    print("ngrok not found. Downloading official binary...")
    zip_path = BIN_DIR / "ngrok.zip"
    if sys.platform == "win32":
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
    elif sys.platform == "darwin":
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-amd64.zip"
    else:
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"

    try:
        urllib.request.urlretrieve(download_url, str(zip_path))
        with zipfile.ZipFile(str(zip_path), "r") as z:
            z.extractall(str(BIN_DIR))
        if zip_path.exists():
            zip_path.unlink()
        if sys.platform != "win32":
            os.chmod(str(local_bin), 0o755)
        print("Downloaded ngrok.")
        return str(local_bin)
    except Exception as e:
        print(f"Failed to auto-download ngrok: {e}")
        return "ngrok"


def enqueue_stream(stream, q):
    for line in iter(stream.readline, ''):
        q.put(line)
    stream.close()


def launch_cloudflare_quick_tunnel(port: int = 8000) -> Optional[str]:
    binary_path = get_cloudflared_path()
    print(f"Starting Cloudflare tunnel on port {port}...")

    try:
        proc = subprocess.Popen(
            [binary_path, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        q = queue.Queue()
        threading.Thread(target=enqueue_stream, args=(proc.stderr, q), daemon=True).start()
        threading.Thread(target=enqueue_stream, args=(proc.stdout, q), daemon=True).start()

        print("Connecting to Cloudflare network", end="", flush=True)
        start_time = time.time()
        url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

        while time.time() - start_time < 30:
            try:
                line = q.get_nowait()
                match = url_regex.search(line)
                if match:
                    tunnel_url = match.group(0)
                    print(f"\nActive Cloudflare tunnel: {tunnel_url}")
                    set_active_network_state(
                        net_type="cloudflare",
                        base_url=tunnel_url,
                        is_permanent=False,
                        is_dynamic=True,
                        is_localhost=False,
                        warning="Temporary Quick Tunnel active. This URL changes every time you restart FreeCord.",
                        label="Cloudflare Quick Tunnel (Dynamic)",
                    )
                    return tunnel_url
            except queue.Empty:
                time.sleep(0.3)
                print(".", end="", flush=True)

        print("\nCloudflare tunnel connection timed out.")
    except Exception as e:
        print(f"\nFailed to launch cloudflared: {e}")
    return None



def launch_ngrok_static_tunnel(port: int = 8000, auth_token: Optional[str] = None, static_domain: Optional[str] = None) -> Optional[str]:
    import httpx

    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get("http://127.0.0.1:4040/api/tunnels")
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for t in tunnels:
                    u = t.get("public_url", "")
                    if u.startswith("https://"):
                        print(f"Ngrok active: {u}")
                        set_active_network_state(
                            net_type="ngrok",
                            base_url=u,
                            is_permanent=True,
                            is_dynamic=False,
                            is_localhost=False,
                            label="Ngrok Static Domain (Permanent)",
                        )
                        return u
    except Exception:
        pass

    binary_path = get_ngrok_path()

    token = auth_token or get_env_variable("NGROK_AUTHTOKEN")
    if not token:
        print("\nNgrok requires a free account token from https://dashboard.ngrok.com/get-started/your-authtoken")
        try:
            token = input("Enter Ngrok Auth Token: ").strip()
            if token:
                update_env_variable("NGROK_AUTHTOKEN", token)
        except Exception:
            token = None

    if token:
        try:
            subprocess.run([binary_path, "config", "add-authtoken", token], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    domain = static_domain or get_env_variable("NGROK_DOMAIN")
    if not domain:
        print("\nEnter Ngrok static domain (optional, press Enter for dynamic, e.g. my-app.ngrok-free.app):")
        try:
            domain_in = input("Ngrok Domain: ").strip()
            if domain_in:
                domain = domain_in
                update_env_variable("NGROK_DOMAIN", domain)
        except Exception:
            domain = None

    cmd = [binary_path, "http"]
    if domain:
        cmd.append(f"--domain={domain}")
    cmd.append(str(port))

    print(f"Starting ngrok on port {port}...")
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        print("Connecting to ngrok network", end="", flush=True)
        for _ in range(30):
            time.sleep(0.8)
            print(".", end="", flush=True)
            try:
                with httpx.Client(timeout=1.5) as client:
                    resp = client.get("http://127.0.0.1:4040/api/tunnels")
                    if resp.status_code == 200:
                        tunnels = resp.json().get("tunnels", [])
                        for t in tunnels:
                            u = t.get("public_url", "")
                            if u.startswith("https://"):
                                print(f"\nActive Ngrok tunnel: {u}")
                                set_active_network_state(
                                    net_type="ngrok",
                                    base_url=u,
                                    is_permanent=bool(domain),
                                    is_dynamic=not bool(domain),
                                    is_localhost=False,
                                    label="Ngrok Static Domain (Permanent)" if domain else "Ngrok Tunnel (Dynamic)",
                                )
                                return u
            except Exception:
                continue
        print("\nNgrok did not respond in time.")
    except Exception as e:
        print(f"\nFailed to start ngrok: {e}")
    return None


def launch_localtunnel(port: int = 8000, custom_subdomain: Optional[str] = None) -> Optional[str]:
    subdomain = custom_subdomain or get_env_variable("LOCALTUNNEL_SUBDOMAIN")
    if not subdomain:
        try:
            subdomain = input("Enter preferred subdomain (e.g. myfreecord): ").strip()
            if subdomain:
                update_env_variable("LOCALTUNNEL_SUBDOMAIN", subdomain)
        except Exception:
            subdomain = "freecord"

    subdomain = subdomain or "freecord"
    clean_sub = re.sub(r'[^a-zA-Z0-9-]', '', subdomain).lower()
    if not clean_sub:
        clean_sub = "freecord"

    print(f"Connecting to Localtunnel on port {port} (subdomain: {clean_sub})...")

    npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
    has_npx = shutil.which(npx_cmd) or shutil.which("npx")

    if has_npx:
        try:
            proc = subprocess.Popen(
                [npx_cmd, "-y", "localtunnel", "--port", str(port), "--subdomain", clean_sub],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=True if sys.platform == "win32" else False,
                bufsize=1,
            )

            q = queue.Queue()
            threading.Thread(target=enqueue_stream, args=(proc.stdout, q), daemon=True).start()
            threading.Thread(target=enqueue_stream, args=(proc.stderr, q), daemon=True).start()

            print("Waiting for assigned URL", end="", flush=True)
            start_time = time.time()
            url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.loca\.lt")

            while time.time() - start_time < 12:
                try:
                    line = q.get_nowait()
                    if "your url is:" in line.lower() or "https://" in line:
                        match = url_regex.search(line)
                        url = match.group(0) if match else line.split("is:")[-1].strip().rstrip("/")
                        if url.startswith("http"):
                            print(f"\nActive Localtunnel: {url}")
                            set_active_network_state(
                                net_type="localtunnel",
                                base_url=url,
                                is_permanent=False,
                                is_dynamic=False,
                                is_localhost=False,
                                label="Localtunnel Subdomain (Semi Permanent)",
                            )
                            return url
                except queue.Empty:
                    time.sleep(0.4)
                    print(".", end="", flush=True)

            print("\nLocaltunnel took too long. Falling back to Cloudflare Quick Tunnel...")
            return launch_cloudflare_quick_tunnel(port)
        except Exception as e:
            print(f"\nLocaltunnel error ({e}). Falling back to Cloudflare...")
            return launch_cloudflare_quick_tunnel(port)

    print("npx is not installed. Using Cloudflare Quick Tunnel...")
    return launch_cloudflare_quick_tunnel(port)


def prompt_custom_domain(prefilled_url: Optional[str] = None) -> Optional[str]:
    url = prefilled_url or get_env_variable("BASE_URL")
    if not url:
        print("\nEnter your domain or reverse proxy URL (e.g. https://verify.mycommunity.com):")
        try:
            url = input("Domain URL: ").strip().rstrip("/")
        except Exception:
            url = None

    if url:
        if not url.startswith("http"):
            url = f"https://{url}"
        print(f"Using custom domain: {url}")
        set_active_network_state(
            net_type="custom",
            base_url=url,
            is_permanent=True,
            is_dynamic=False,
            is_localhost=False,
            label="Custom Domain / Reverse Proxy (Permanent)",
        )
        return url
    return None


def select_network_ingress(
    port: int = 8000,
    cli_choice: Optional[str] = None,
    custom_url: Optional[str] = None,
    no_prompt: bool = False,
) -> str:
    from core.network_detector import get_public_ip, is_vps, get_ngrok_url

    pub_ip = get_public_ip()
    on_vps = is_vps()
    existing_ngrok = get_ngrok_url()

    env_choice = os.getenv("FREECORD_NETWORK_CHOICE") or os.getenv("FREECORD_TUNNEL_CHOICE")
    choice = (cli_choice or env_choice or "").strip().lower()

    if choice:
        if choice in ("1", "cloudflare", "cf"):
            url = launch_cloudflare_quick_tunnel(port)
            if url:
                return url
        elif choice in ("2", "ngrok"):
            url = launch_ngrok_static_tunnel(port)
            if url:
                return url
        elif choice in ("3", "localtunnel", "lt"):
            url = launch_localtunnel(port)
            if url:
                return url
        elif choice in ("4", "custom"):
            url = prompt_custom_domain(custom_url)
            if url:
                return url
        elif choice in ("5", "localhost", "local"):
            set_active_network_state(
                net_type="localhost",
                base_url=f"http://localhost:{port}",
                is_permanent=False,
                is_dynamic=False,
                is_localhost=True,
                warning="Localhost mode active. Discord verification will not work for server members.",
                label="Localhost Only (Testing)",
            )
            return f"http://localhost:{port}"

    if on_vps and pub_ip and no_prompt:
        vps_url = f"http://{pub_ip}:{port}"
        set_active_network_state(
            net_type="vps",
            base_url=vps_url,
            is_permanent=True,
            is_dynamic=False,
            is_localhost=False,
            label="Cloud VPS Public IP (Permanent)",
        )
        return vps_url

    if existing_ngrok:
        set_active_network_state(
            net_type="ngrok",
            base_url=existing_ngrok,
            is_permanent=True,
            is_dynamic=False,
            is_localhost=False,
            label="Active Ngrok Tunnel",
        )
        return existing_ngrok

    if no_prompt:
        if on_vps and pub_ip:
            vps_url = f"http://{pub_ip}:{port}"
            set_active_network_state(net_type="vps", base_url=vps_url, is_permanent=True, label="Cloud VPS")
            return vps_url
        url = launch_cloudflare_quick_tunnel(port)
        if url:
            return url
        set_active_network_state(net_type="localhost", base_url=f"http://localhost:{port}", is_localhost=True, label="Localhost")
        return f"http://localhost:{port}"

    print("=" * 65)
    print("                 FreeCord Connection Setup")
    print("=" * 65)
    print(" [1] Cloudflare Quick Tunnel (Zero config, temporary URL)")
    print(" [2] Ngrok Static Domain (Permanent free tunnel)")
    print(" [3] Localtunnel (Custom subdomain)")
    print(" [4] Custom Domain / Reverse Proxy")
    print(" [5] Localhost Only (Internal testing)")
    print("=" * 65)

    default_choice = "1" if not on_vps else "4"
    try:
        user_input = input(f" Choice [1-5] (default: {default_choice}): ").strip()
    except Exception:
        user_input = default_choice

    user_choice = user_input if user_input else default_choice

    if user_choice == "1":
        url = launch_cloudflare_quick_tunnel(port)
        if url:
            return url
    elif user_choice == "2":
        url = launch_ngrok_static_tunnel(port)
        if url:
            return url
    elif user_choice == "3":
        url = launch_localtunnel(port)
        if url:
            return url
    elif user_choice == "4":
        url = prompt_custom_domain(custom_url)
        if url:
            return url
    elif user_choice == "5":
        set_active_network_state(
            net_type="localhost",
            base_url=f"http://localhost:{port}",
            is_permanent=False,
            is_dynamic=False,
            is_localhost=True,
            warning="Localhost mode active. Discord verification will not work for server members.",
            label="Localhost Only",
        )
        return f"http://localhost:{port}"

    url = launch_cloudflare_quick_tunnel(port)
    if url:
        return url

    set_active_network_state(
        net_type="localhost",
        base_url=f"http://localhost:{port}",
        is_localhost=True,
        label="Localhost Only",
    )
    return f"http://localhost:{port}"

