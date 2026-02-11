"""Debug script: open the login page, wait for CF challenge, inspect the page."""
import asyncio
import os
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async


async def main():
    os.makedirs("screenshots", exist_ok=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="ru-RU",
        timezone_id="Europe/Moscow",
    )
    page = await context.new_page()
    await stealth_async(page)
    await page.add_init_script("""() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
        window.chrome = { runtime: {} };
    }""")

    url = "https://russia.blsportugal.com/Global/account/login"
    print(f"[1] Navigating to {url}")

    try:
        resp = await page.goto(url, timeout=60000)
        status = resp.status if resp else "None"
        print(f"[2] Response status: {status}")
    except Exception as e:
        print(f"[2] Navigation error: {e}")

    # Take immediate screenshot of whatever loaded
    try:
        await page.screenshot(path="screenshots/debug_01_initial.png", full_page=True)
        print("[3] Initial screenshot saved")
    except Exception as e:
        print(f"[3] Screenshot error: {e}")

    # Wait for Cloudflare challenge - up to 30 seconds
    print("[4] Waiting up to 30s for Cloudflare challenge to resolve...")
    for i in range(30):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            current_url = page.url
            if i % 5 == 0:
                print(f"  [{i}s] title='{title}' url={current_url}")
            # If we're past the challenge, the title should change
            if "just a moment" not in title.lower() and "attention" not in title.lower():
                if "login" in current_url.lower() or "blsportugal" in current_url.lower():
                    print(f"[5] Challenge resolved at {i}s! title='{title}'")
                    break
        except Exception as e:
            print(f"  [{i}s] Error reading page: {e}")
            break
    else:
        print("[5] Timeout waiting for challenge to resolve")

    # Now inspect the page
    try:
        await page.screenshot(path="screenshots/debug_02_after_wait.png", full_page=True)
        print("[6] Post-wait screenshot saved")

        title = await page.title()
        current_url = page.url
        print(f"[7] Title: {title}")
        print(f"[7] URL: {current_url}")

        text = await page.inner_text("body")
        print(f"\n--- Page text (first 3000 chars) ---\n{text[:3000]}")

        # Form elements
        inputs = await page.query_selector_all("input, select, button, textarea")
        print(f"\n--- Found {len(inputs)} form elements ---")
        for inp in inputs:
            tag = await inp.evaluate("el => el.tagName")
            inp_type = await inp.get_attribute("type") or ""
            name = await inp.get_attribute("name") or ""
            inp_id = await inp.get_attribute("id") or ""
            placeholder = await inp.get_attribute("placeholder") or ""
            cls = await inp.get_attribute("class") or ""
            print(f"  <{tag}> type={inp_type} name={name} id={inp_id} placeholder={placeholder} class={cls[:50]}")

        # Captcha elements
        captcha_els = await page.query_selector_all("[data-sitekey], .h-captcha, .g-recaptcha, .cf-turnstile")
        print(f"\n--- Captcha elements: {len(captcha_els)} ---")
        for el in captcha_els:
            cls = await el.get_attribute("class") or ""
            sitekey = await el.get_attribute("data-sitekey") or ""
            print(f"  class={cls} data-sitekey={sitekey}")

        # Check iframes (captcha might be in an iframe)
        frames = page.frames
        print(f"\n--- Frames: {len(frames)} ---")
        for frame in frames:
            print(f"  name={frame.name} url={frame.url}")

    except Exception as e:
        print(f"[!] Error inspecting page: {e}")

    print("\nKeeping browser open for 60s — inspect it manually...")
    await asyncio.sleep(60)

    await browser.close()
    await pw.stop()
    print("Done.")


asyncio.run(main())
