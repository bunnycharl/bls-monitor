import asyncio
import json
import logging
import os
import shutil
import subprocess
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

CDP_PORT = 9222
CHROME_USER_DATA = "session/chrome_profile"
PROXY_EXT_DIR = "session/proxy_ext"


def _find_chrome() -> str:
    """Find Chrome executable on Windows or Linux."""
    candidates = [
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
    """Generate a Manifest V2 Chrome extension for proxy authentication."""
    os.makedirs(PROXY_EXT_DIR, exist_ok=True)

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": [
            "webRequest",
            "webRequestBlocking",
            "<all_urls>",
        ],
        "background": {
            "scripts": ["background.js"],
        },
    }

    background_js = f"""chrome.webRequest.onAuthRequired.addListener(
  function(details) {{
    return {{
      authCredentials: {{
        username: "{username}",
        password: "{password}"
      }}
    }};
  }},
  {{ urls: ["<all_urls>"] }},
  ["blocking"]
);
"""

    with open(os.path.join(PROXY_EXT_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(PROXY_EXT_DIR, "background.js"), "w") as f:
        f.write(background_js)

    return os.path.abspath(PROXY_EXT_DIR)


class BrowserManager:
    def __init__(self, config: dict):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._chrome_proc: subprocess.Popen | None = None

    async def launch(self) -> None:
        self._playwright = await async_playwright().start()
        bc = self.config["browser"]

        chrome_path = _find_chrome()
        os.makedirs(CHROME_USER_DATA, exist_ok=True)

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
            "about:blank",
        ]

        # Proxy setup
        if bc.get("proxy"):
            proxy = _parse_proxy(bc["proxy"])
            proxy_server = f"{proxy['host']}:{proxy['port']}"
            chrome_args.insert(2, f"--proxy-server={proxy_server}")
            logger.info("Using proxy: %s", proxy_server)

            # Auth extension (Chrome --proxy-server doesn't support user:pass)
            if proxy.get("username") and proxy.get("password"):
                ext_path = _create_proxy_auth_extension(
                    proxy["username"], proxy["password"]
                )
                chrome_args.insert(2, f"--load-extension={ext_path}")
                chrome_args.insert(2, "--disable-extensions-except=" + ext_path)
                logger.info("Proxy auth extension loaded from %s", ext_path)

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

        logger.info("Connected to Chrome via CDP (headless=%s)", bc["headless"])

    async def save_session(self) -> None:
        pass

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
