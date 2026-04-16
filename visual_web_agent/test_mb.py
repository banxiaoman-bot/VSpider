import asyncio
import logging
from browser_env import BrowserEnv

logging.basicConfig(level=logging.INFO)

async def t():
    b = BrowserEnv()
    await b.start('https://www.baidu.com')
    res = await b.mark_and_screenshot(1)
    print('OK')

asyncio.run(t())
