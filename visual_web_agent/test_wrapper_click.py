import asyncio
from playwright.async_api import async_playwright
import time

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('https://www.baidu.com')
        # click login
        await page.click('#s-top-loginbtn')
        await page.wait_for_selector('.tang-pass-login', timeout=5000)
        await asyncio.sleep(1)
        
        # Click the wrapper directly
        wrappers = await page.locator("#TANGRAM__PSP_11__userNameWrapper").element_handles()
        if wrappers:
            el = wrappers[0]
            print("Found wrapper:", el)
            
            await el.click(force=True)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await page.keyboard.type("15709939221", delay=50)
            
            val = await page.locator("input[name='userName']").input_value()
            print("VALUE AFTER WRAPPER CLICK TYPE:", val)
        else:
            print("No wrapper found")
            
        await browser.close()

asyncio.run(test())
