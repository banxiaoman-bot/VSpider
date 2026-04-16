import asyncio
from pathlib import Path
from browser_env import BrowserEnv
from playwright.async_api import async_playwright

async def t():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        await page.goto("https://www.baidu.com")
        som_js = Path("som_inject_v4.js").read_text(encoding="utf-8")
        res = await page.evaluate(som_js, 1)
        print("RES =", type(res), str(res)[:100])

asyncio.run(t())
