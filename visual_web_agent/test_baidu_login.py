import asyncio
from playwright.async_api import async_playwright
import time
from browser_env import BrowserEnv

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('https://www.baidu.com')
        # click login
        await page.click('#s-top-loginbtn')
        await page.wait_for_selector('.tang-pass-login', timeout=5000)
        await asyncio.sleep(1)
        
        # Test finding input and typing simulating our script
        # baidu's input id is typically part of Tangram
        inputs = await page.locator("input[name='userName']").element_handles()
        if inputs:
            el = inputs[0]
            print("Found input")
            # Try to evaluate removing disabled/readonly
            await page.evaluate('(el) => { el.removeAttribute("readonly"); }', el)
            
            await el.click(force=True)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await page.keyboard.type("15709939221", delay=50)
            
            # check what it contains
            val = await el.input_value()
            print("VALUE AFTER TYPE:", val)
        else:
            print("No input found")
            
        await browser.close()

asyncio.run(test())
