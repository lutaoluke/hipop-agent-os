"""
Playwright 截图：覆盖 7 张 demo 图
"""
import asyncio, os, sys
from playwright.async_api import async_playwright

OUT = "/Users/luke/code/hipop/hipop/logs/screenshots"
os.makedirs(OUT, exist_ok=True)
BASE = "http://127.0.0.1:8765"

PAGES = [
    ("01_overview_ksa.png", "/", 3500),
    ("02_module_sales.png", "/module/sales?store=ksa", 2500),
    ("03_module_logistics.png", "/module/logistics?store=ksa", 2500),
    ("04_module_replenish.png", "/module/replenish?store=ksa", 2500),
    ("05_module_selection.png", "/module/selection?store=ksa", 2000),
    ("06_role_liuhe.png", "/role/liuhe", 2500),
    ("07_overview_uae.png", "/?store=uae", 3000),
    ("08_module_feishu.png", "/module/feishu?store=ksa", 1500),
]


async def take_chat_screenshot(context):
    """额外: 进入 overview 后, 在右侧 chat 框里发一条然后等回复, 截全屏"""
    page = await context.new_page()
    await page.set_viewport_size({"width": 1440, "height": 900})
    await page.goto(f"{BASE}/")
    await page.wait_for_timeout(2500)
    # 发一条到 chat
    try:
        await page.fill("textarea", "看 KSA 卡单的几个 SKU")
        # 模拟发送
        await page.click("button:has-text('发送')")
        # 等 LLM 回复
        await page.wait_for_timeout(15000)
    except Exception as e:
        print(f"chat send failed: {e}")
    out = os.path.join(OUT, "09_chat_with_llm.png")
    await page.screenshot(path=out, full_page=True)
    print(f"  ✓ {out}")
    await page.close()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        for name, path, wait_ms in PAGES:
            page = await context.new_page()
            try:
                await page.goto(f"{BASE}{path}", wait_until="domcontentloaded")
                await page.wait_for_timeout(wait_ms)
                out = os.path.join(OUT, name)
                await page.screenshot(path=out, full_page=True)
                print(f"  ✓ {out}  ({path})")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
            await page.close()

        # chat 截图额外
        try:
            await take_chat_screenshot(context)
        except Exception as e:
            print(f"chat screenshot fail: {e}")

        await browser.close()
    print(f"\n截图完成 → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
