"""【DEPRECATED】紫鸟 web_driver 模式手动 probe —— 已被协议层 + 运维入口取代。

⚠️ 不要再用本脚本手动传 `browserOauth` / `debuggPort` 跑 startBrowser。
   原因：紫鸟 web_driver 的 `startBrowser` **绝不能**带 debuggPort/debugPort
   （传了真实紫鸟返回 -10000），端口由紫鸟自动分配、只能从响应 `debuggingPort` 读。
   本脚本旧的 `start <browserOauth>` 路径正好踩这个坑，且绕过了多租户凭据/登录态检测。

正确入口（二选一）
------------------
1. 取"已登录的平台浏览器会话"（业务取数）：
       from hipop.server._platform_browser import get_platform_session
       page = get_platform_session(tenant_id, store_key)   # 三级回落 + 登录态 blocked
   协议骨架/选店/startBrowser/CDP 接管全在 `hipop/server/_platform_browser.py`，
   不要在脚本里手拼 applyAuth/getBrowserList/startBrowser。

2. 进程层"紫鸟 web_driver 是否常驻 / 健康"（运维）：
       python3 -m hipop.scripts.ziniao_webdriver healthcheck
       python3 -m hipop.scripts.ziniao_webdriver start
   常驻守护见 `hipop/launchd/com.hipop.ziniao.plist`
   （`bash hipop/launchd/install.sh install`）。

本文件保留只为不破坏外部引用；运行它会打印本提示并转交健康检查。
"""
import sys


_BANNER = __doc__


def main() -> int:
    print(_BANNER)
    print("→ 转交：python3 -m hipop.scripts.ziniao_webdriver healthcheck\n")
    try:
        from hipop.scripts.ziniao_webdriver import healthcheck
    except Exception as e:  # noqa: BLE001
        print(f"（无法加载新入口：{e}；请直接跑 "
              f"`python3 -m hipop.scripts.ziniao_webdriver healthcheck`）")
        return 2
    return healthcheck()


if __name__ == "__main__":
    sys.exit(main())
