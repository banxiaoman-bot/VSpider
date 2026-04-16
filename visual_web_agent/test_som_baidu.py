import asyncio
from playwright.async_api import async_playwright
import time
from pathlib import Path

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('https://www.baidu.com')
        # click login
        await page.click('#s-top-loginbtn')
        await page.wait_for_selector('.tang-pass-login', timeout=5000)
        await asyncio.sleep(2)
        
        # Inject SoM
        som_js = Path("som_inject_v4.js").read_text(encoding="utf-8")
        res = await page.evaluate(som_js, 1)
        
        # Print all marked elements inside the login dialog
        for item in res.get("resultMap", []):
            if "登录" in item["text"] or "手机号" in item["text"]:
                print(item)
            
        await browser.close()

asyncio.run(test())
