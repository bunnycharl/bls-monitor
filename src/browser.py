import asyncio
import json
import logging
import os
import socket
import subprocess
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import stealth_async

logger = logging.getLogger(__name__)

CDP_PORT = 9222
CHROME_USER_DATA = "session/chrome_profile"
PROXY_EXT_DIR = "session/proxy_ext"


def _find_chrome() -> str:
    """Find Chrome executable on Windows, macOS, or Linux."""
    candidates = [
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        # Windows
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("Chrome not found. Install Google Chrome.")


def _parse_proxy(proxy_str: str) -> dict:
    """Parse proxy string like 'http://user:pass@host:port' into components."""
    parsed = urlparse(proxy_str)
    return {
        "scheme": parsed.scheme or "http",
        "host": parsed.hostname,
        "port": parsed.port,
        "username": parsed.username,
        "password": parsed.password,
    }


def _create_proxy_auth_extension(username: str, password: str) -> str:
    """Generate a Manifest V3 Chrome extension for proxy authentication."""
    os.makedirs(PROXY_EXT_DIR, exist_ok=True)

    manifest = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Proxy Auth",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {
            "service_worker": "background.js",
        },
    }

    safe_user = json.dumps(username)
    safe_pass = json.dumps(password)

    background_js = f"""chrome.webRequest.onAuthRequired.addListener(
  function(details, callbackFn) {{
    callbackFn({{
      authCredentials: {{
        username: {safe_user},
        password: {safe_pass}
      }}
    }});
  }},
  {{ urls: ["<all_urls>"] }},
  ["asyncBlocking"]
);
"""

    with open(os.path.join(PROXY_EXT_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(PROXY_EXT_DIR, "background.js"), "w") as f:
        f.write(background_js)

    return os.path.abspath(PROXY_EXT_DIR)


def _kill_zombie_chrome(port: int) -> None:
    """Kill any existing Chrome process listening on the CDP port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        result = sock.connect_ex(("127.0.0.1", port))
        if result == 0:
            logger.warning("Port %d already in use, killing zombie Chrome...", port)
            import platform
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "chrome.exe"],
                    capture_output=True,
                )
            else:
                subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True, text=True,
                )
                result_lsof = subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True, text=True,
                )
                for pid in result_lsof.stdout.strip().split("\n"):
                    if pid:
                        subprocess.run(["kill", "-9", pid], capture_output=True)
            logger.info("Zombie Chrome killed, waiting for port to free...")
            import time
            time.sleep(2)
    finally:
        sock.close()


class BrowserManager:
    def __init__(self, config: dict):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._chrome_proc: subprocess.Popen | None = None
        self._cdp_session = None
        self._proxy_creds: dict | None = None

    async def launch(self) -> None:
        _kill_zombie_chrome(CDP_PORT)

        self._playwright = await async_playwright().start()
        bc = self.config["browser"]

        chrome_path = _find_chrome()
        os.makedirs(CHROME_USER_DATA, exist_ok=True)

        # Clean stale lock files from previous runs
        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
            lock_path = os.path.join(CHROME_USER_DATA, lock)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                    logger.info("Removed stale Chrome lock: %s", lock)
                except OSError:
                    pass

        chrome_args = [
            chrome_path,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={os.path.abspath(CHROME_USER_DATA)}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={bc['viewport_width']},{bc['viewport_height']}",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--metrics-recording-only",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
            "about:blank",
        ]

        # Proxy setup
        if bc.get("proxy"):
            proxy = _parse_proxy(bc["proxy"])
            proxy_server = f"{proxy['host']}:{proxy['port']}"
            chrome_args.insert(2, f"--proxy-server={proxy_server}")
            logger.info("Using proxy: %s", proxy_server)

            if proxy.get("username") and proxy.get("password"):
                self._proxy_creds = {
                    "username": proxy["username"],
                    "password": proxy["password"],
                }
                logger.info("Proxy auth will be handled via CDP Fetch API")

        if bc["headless"]:
            chrome_args.insert(2, "--headless=new")

        logger.info("Launching Chrome via CDP: %s", chrome_path)
        self._chrome_proc = subprocess.Popen(
            chrome_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for CDP to become available
        cdp_url = f"http://127.0.0.1:{CDP_PORT}"
        for attempt in range(20):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Could not connect to Chrome CDP after 10 seconds")

        # Use the default context created by Chrome
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
            pages = self._context.pages
            if pages:
                self.page = pages[0]
            else:
                self.page = await self._context.new_page()
        else:
            self._context = await self._browser.new_context()
            self.page = await self._context.new_page()

        # Set up CDP proxy auth handler (extensions don't work in headless)
        if self._proxy_creds:
            await self._setup_proxy_auth()

        # Apply stealth to avoid bot detection
        await stealth_async(self.page)
        await self.page.add_init_script("""() => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        }""")

        logger.info("Connected to Chrome via CDP (headless=%s)", bc["headless"])

    async def _setup_proxy_auth(self) -> None:
        """Set up CDP Fetch domain to handle proxy authentication."""
        self._cdp_session = await self._context.new_cdp_session(self.page)
        creds = self._proxy_creds

        def on_auth_required(params):
            request_id = params["requestId"]
            asyncio.ensure_future(
                self._cdp_session.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": creds["username"],
                            "password": creds["password"],
                        },
                    },
                )
            )

        def on_request_paused(params):
            request_id = params["requestId"]
            asyncio.ensure_future(
                self._cdp_session.send(
                    "Fetch.continueRequest", {"requestId": request_id}
                )
            )

        self._cdp_session.on("Fetch.authRequired", on_auth_required)
        self._cdp_session.on("Fetch.requestPaused", on_request_paused)

        await self._cdp_session.send(
            "Fetch.enable", {"handleAuthRequests": True}
        )
        logger.info("CDP proxy auth handler enabled")

    async def close(self) -> None:
        try:
            if self._context:
                for p in self._context.pages:
                    try:
                        await p.close()
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Error closing pages: %s", e)

        self._browser = None

        if self._playwright:
            await self._playwright.stop()

        if self._chrome_proc:
            self._chrome_proc.terminate()
            try:
                self._chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome_proc.kill()
            self._chrome_proc = None

        logger.info("Browser closed")
