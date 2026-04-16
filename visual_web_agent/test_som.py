"""
SoM 注入测试脚本

打开本地测试页面，注入 som_inject.js，截图验证标记效果。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright
from config import SOM_SCRIPT_PATH, VIEWPORT_WIDTH, VIEWPORT_HEIGHT


async def test_som():
    # 加载 SoM 脚本
    som_js = SOM_SCRIPT_PATH.read_text(encoding="utf-8")
    test_page = Path(__file__).parent / "test_page.html"

    print("启动 Playwright...")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
    )
    page = await context.new_page()

    # 打开测试页面
    print(f"打开测试页面: {test_page}")
    await page.goto(f"file:///{test_page.resolve()}")
    await page.wait_for_timeout(500)

    # 注入 SoM 脚本
    print("注入 SoM 标记脚本...")
    element_map = await page.evaluate(som_js)

    print(f"\n{'='*50}")
    print(f"共标记 {len(element_map)} 个可交互元素:")
    print(f"{'='*50}")
    for item in element_map:
        print(f"  [{item['id']:>2}] <{item['tag']}> {item.get('type', '')} | {item['text'][:40]}")
    print(f"{'='*50}")

    # 等待渲染后截图
    await page.wait_for_timeout(500)
    screenshot_path = Path(__file__).parent / "screenshots" / "som_test.png"
    screenshot_path.parent.mkdir(exist_ok=True)
    await page.screenshot(path=str(screenshot_path))
    print(f"\n截图已保存: {screenshot_path}")

    # 验证 data-som-id 属性
    som_elements = await page.query_selector_all("[data-som-id]")
    print(f"含 data-som-id 属性的元素数: {len(som_elements)}")

    # 测试能否通过 data-som-id 定位第一个元素
    if element_map:
        first_id = element_map[0]["id"]
        el = await page.query_selector(f'[data-som-id="{first_id}"]')
        if el:
            print(f"[PASS] data-som-id=\"{first_id}\" locate OK: <{element_map[0]['tag']}>")
        else:
            print(f"[FAIL] data-som-id=\"{first_id}\" locate FAILED!")

    await context.close()
    await browser.close()
    await pw.stop()
    print("\n[PASS] SoM inject test completed!")


if __name__ == "__main__":
    asyncio.run(test_som())
