import asyncio
import logging
import random
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
import httpx
import aiohttp

logger = logging.getLogger("freecord.proxy_manager")

BASE_DIR = Path(__file__).resolve().parent.parent
PROXIES_FILE = BASE_DIR / "proxies.txt"

PROXYSCRAPE_URL = (
    "https://api.proxyscrape.com/v2/?request=displayproxies"
    "&protocol=http,socks5&timeout=5000&country=all&ssl=all&anonymity=elite,anonymous"
)


class ProxyItem:
    def __init__(self, url: str, is_custom: bool = False):
        self.url = url.strip()
        self.is_custom = is_custom
        self.latency: float = 999.0
        self.is_alive: bool = True
        self.fail_count: int = 0
        self.last_checked: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "is_custom": self.is_custom,
            "latency": round(self.latency, 3),
            "is_alive": self.is_alive,
            "fail_count": self.fail_count,
        }


class HybridProxyManager:
    def __init__(self):
        self.custom_pool: List[ProxyItem] = []
        self.free_pool: List[ProxyItem] = []
        self.active_index: int = 0
        self.last_fetch_time: float = 0.0
        self.fetch_interval_seconds: int = 600
        self.lock = asyncio.Lock()
        self._is_checking = False

    async def initialize(self):
        self.load_local_proxies()
        await self.refresh_free_proxies()
        asyncio.create_task(self._health_check_loop())

    def load_local_proxies(self) -> int:
        self.custom_pool.clear()
        if not PROXIES_FILE.exists():
            PROXIES_FILE.write_text("", encoding="utf-8")
            return 0

        count = 0
        try:
            lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    if not line.startswith(("http://", "https://", "socks5://", "socks4://")):
                        line = f"http://{line}"
                    self.custom_pool.append(ProxyItem(url=line, is_custom=True))
                    count += 1
            logger.debug(f"Loaded {count} custom proxies from proxies.txt")
        except Exception as e:
            logger.debug(f"Could not load proxies.txt: {e}")
        return count

    async def refresh_free_proxies(self) -> int:
        now = time.time()
        if now - self.last_fetch_time < 60:
            return len(self.free_pool)

        fetched = []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(PROXYSCRAPE_URL)
                if resp.status_code == 200:
                    lines = resp.text.strip().splitlines()
                    for line in lines:
                        line = line.strip()
                        if line:
                            formatted = f"http://{line}" if not line.startswith(("http://", "socks5://")) else line
                            fetched.append(ProxyItem(url=formatted, is_custom=False))
                    self.free_pool = fetched
                    self.last_fetch_time = now
                    logger.debug(f"Fetched {len(fetched)} free proxies")
        except Exception as e:
            logger.debug(f"Failed to fetch ProxyScrape proxies: {e}")
        return len(self.free_pool)

    async def check_proxy_health(self, proxy: ProxyItem) -> bool:
        start = time.time()
        test_url = "https://discord.com/api/v10/gateway"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(test_url, proxy=proxy.url, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                    if resp.status in (200, 429):
                        proxy.latency = time.time() - start
                        proxy.is_alive = True
                        proxy.fail_count = 0
                        proxy.last_checked = time.time()
                        return True
        except Exception:
            pass

        proxy.fail_count += 1
        if proxy.fail_count >= 2:
            proxy.is_alive = False
        return False

    async def _health_check_loop(self):
        while True:
            await asyncio.sleep(self.fetch_interval_seconds)
            try:
                await self.refresh_free_proxies()
                sample = self.custom_pool + self.free_pool[:30]
                tasks = [self.check_proxy_health(p) for p in sample]
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                pass

    async def get_next_proxy(self) -> Optional[str]:
        async with self.lock:
            alive_custom = [p for p in self.custom_pool if p.is_alive]
            if alive_custom:
                self.active_index = (self.active_index + 1) % len(alive_custom)
                return alive_custom[self.active_index].url

            alive_free = [p for p in self.free_pool if p.is_alive]
            if alive_free:
                choice = random.choice(alive_free[:40])
                return choice.url

            if self.free_pool:
                choice = random.choice(self.free_pool[:20])
                return choice.url

            return None

    def get_stats(self) -> Dict[str, Any]:
        custom_alive = len([p for p in self.custom_pool if p.is_alive])
        free_alive = len([p for p in self.free_pool if p.is_alive])
        return {
            "custom_total": len(self.custom_pool),
            "custom_alive": custom_alive,
            "free_total": len(self.free_pool),
            "free_alive": free_alive,
            "total_available": custom_alive + free_alive,
        }


proxy_manager = HybridProxyManager()

