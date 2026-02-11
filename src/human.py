import asyncio
import random

from playwright.async_api import Locator, Page


class HumanBehavior:
    @staticmethod
    async def random_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
        delay = random.uniform(min_ms, max_ms) / 1000.0
        await asyncio.sleep(delay)

    @staticmethod
    async def type_like_human(page: Page, selector: str, text: str) -> None:
        element = page.locator(selector).first
        await element.click()
        await HumanBehavior.random_delay(300, 800)
        # Clear any existing value
        await element.fill("")
        for char in text:
            await element.press(char)
            delay = random.uniform(50, 180) / 1000.0
            if random.random() < 0.05:
                delay += random.uniform(200, 500) / 1000.0
            await asyncio.sleep(delay)

    @staticmethod
    async def click_with_delay(locator: Locator) -> None:
        await HumanBehavior.random_delay(200, 600)
        box = await locator.bounding_box()
        if box:
            offset_x = random.uniform(box["width"] * 0.25, box["width"] * 0.75)
            offset_y = random.uniform(box["height"] * 0.25, box["height"] * 0.75)
            await locator.click(position={"x": offset_x, "y": offset_y})
        else:
            await locator.click()

    @staticmethod
    async def scroll_to(page: Page, selector: str) -> None:
        await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }""",
            selector,
        )
        await HumanBehavior.random_delay(300, 700)
