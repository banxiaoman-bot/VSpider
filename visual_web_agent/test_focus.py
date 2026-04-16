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
        
        # In baidu, the wrapper is typically `#TANGRAM__PSP_11__userNameWrapper`
        wrappers = await page.locator("#TANGRAM__PSP_11__userNameWrapper").element_handles()
        if wrappers:
            el = wrappers[0]
            print("Found wrapper:", el)
            
            # Smart focusing via JS
            await page.evaluate('''(_el) => {
                let el = _el;
                if(el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && !el.isContentEditable) {
                    const innerInput = el.querySelector('input:not([type="hidden"]), textarea, [contenteditable]');
                    if(innerInput) el = innerInput;
                }
                el.removeAttribute('readonly');
                el.removeAttribute('disabled');
                el.focus();
            }''', el)
            
            # Not clicking! Just relying on focus
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            await page.keyboard.type("15709939221", delay=50)
            
            val = await page.locator("input[name='userName']").input_value()
            print("VALUE AFTER SMART TYPE:", val)
        else:
            print("No wrapper found")
            
        await browser.close()

asyncio.run(test())
