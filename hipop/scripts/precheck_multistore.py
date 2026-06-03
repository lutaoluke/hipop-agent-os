"""多店枚举 live 预检（WS-46 / WS-33.0）——如实回报，绝不冒充第二店。

跑真实本机紫鸟 web_driver：枚举当前 account 下**全部** store（不硬编 browserId），逐店
按 sales_entities 解析 tenant/entity，打印**真实 store count + 脱敏 store 标识 + 映射结果**。

acceptance #3/#5：若真实账号仍只返回 1 店，必须在输出里写明
  `multi-store live BLOCKED: account exposes 1 store`
绝不报「多店已实测通过」。真实多店 live 终验收 deferred（等真多店 fixture）。

退出码：
  0  枚举到 >=2 店且全部能映射（真实多店通路打通）
  3  multi-store live BLOCKED（只 1 店 / 缺映射 / 紫鸟不可达 / 缺凭据）——如实 blocked
  2  其它意外错误

用法（本机紫鸟 web_driver 起在 18080）：
  python3 -m hipop.scripts.precheck_multistore
  python3 -m hipop.scripts.precheck_multistore --account <登录用户名>
  python3 -m hipop.scripts.precheck_multistore --tenant 1
"""
from __future__ import annotations

import argparse
import sys

from hipop.server import _platform_browser as pb
from hipop.server import data as _data


def _redact(s: str) -> str:
    """脱敏：保留首尾各 2 字符，中间打码（不泄露完整 store 标识/账号）。"""
    s = str(s or "")
    if len(s) <= 4:
        return (s[:1] + "*" * max(0, len(s) - 1)) if s else "(空)"
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多店枚举 live 预检（如实回报）")
    ap.add_argument("--account", default=None, help="覆盖枚举的登录用户名")
    ap.add_argument("--port", type=int, default=None, help="紫鸟 web_driver 端口")
    ap.add_argument("--tenant", type=int, default=1,
                    help="读 sales_entities 真相源用的 tenant context（默认 1）")
    args = ap.parse_args(argv)

    # 读 sales_entities 真相源需要 tenant context（RLS/WHERE 按当前 context）。
    _data.set_current_tenant(args.tenant)

    print("== 多店枚举 live 预检（WS-46）==")
    try:
        stores = pb.list_stores(account=args.account, port=args.port)
    except pb.PlatformBrowserError as e:
        print(f"  紫鸟/凭据不可用：{e}")
        print("\nmulti-store live BLOCKED: 紫鸟 web_driver 不可达或缺凭据，无法枚举 store")
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"  意外错误：{type(e).__name__}: {e}")
        return 2

    n = len(stores)
    print(f"  真实 store count = {n}")
    for s in stores:
        print(f"    - browser_id={_redact(s.browser_id)} "
              f"store_username={_redact(s.store_username)} name={_redact(s.name)}")

    # 逐店映射（如实暴露缺映射），即使只有 1 店也跑，证明映射通路。
    try:
        resolved, blocked = pb.map_stores(account=args.account, port=args.port)
    except pb.PlatformBrowserError as e:
        print(f"  映射阶段 blocked：{e}")
        resolved, blocked = [], [(s, "map_stores blocked") for s in stores]

    print(f"  已映射 {len(resolved)} / 缺映射 {len(blocked)}")
    for se in resolved:
        print(f"    ✓ {_redact(se.store.name)} → tenant={se.tenant_id} "
              f"entity={se.entity_alias} ({se.matched_on})")
    for s, reason in blocked:
        print(f"    ✗ {_redact(s.name)} 缺映射: {reason[:90]}")

    print()
    if n < 2:
        print(f"multi-store live BLOCKED: account exposes {n} store"
              f"{'' if n == 1 else 's'} —— 真实多店验收 deferred（需真多店 fixture），"
              f"绝不冒充第二店")
        return 3
    if blocked:
        print(f"multi-store live BLOCKED: {len(blocked)} 个 store 缺 tenant/entity 映射 —— "
              f"补 sales_entities 行后重试，绝不默认塞 tenant=1")
        return 3
    print(f"multi-store live OK: 枚举到 {n} 店且全部映射成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
