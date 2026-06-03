"""live_producers — 生产侧 live row producer 注册收口（启动即接线，WS-58 门2 返工）。

为什么要这层：
  WS-35 把 noon 三类 ingest 改成「实时行来源」插座——producer 未注册时 `run_live` 回落 CSV。
  WS-N2.1/WS-58 写了 noon 订单抓取器，但「写了抓取器」≠「生产入口已接上」：fresh process
  只 `import noon_order_fetcher` 时 producer 仍 **未注册**，`get_live_row_producer('orders')`
  仍是 None、`run_live` 默认仍回落 CSV（**接线缺失死法**：只证明手动调 register 能注册，没证明
  生产默认会走真抓取器）。

本模块就是那道接线：**import 即把已就绪的 live producer 注册进** `noon_live_contract` 的统一
注册表。生产入口（worker / api 运行任何 workflow 时加载 `workflow_runners`，连带加载本模块）
起来后，对应 kind 的 live producer 已就位、`run_live` 默认走真抓取器、`missing_live_producers()`
不再含它。

约定（单一收口）：抓取器自身只提供 `register_live_producer()`（可注入替身、可手动调、可被
deterministic smoke 控制）；「生产默认接线」集中在这里，新抓取器就绪后在 `register_all()`
加一行（stock=WS-N2.2 / asn=WS-N2.3），不散落各处。

无外部副作用：producer 是惰性闭包，注册时不连紫鸟、不取数、不开库——仅在 `run_live` 真正
调用时才 `get_platform_session`。故 import 本模块安全（不触发 ziniao/DB）。

复验：`tests/smoke_noon_order_wiring.py` 起 fresh 解释器只 import 生产入口，断言 orders 已自动
就位且 `run_live` 默认走该 producer。`HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1` 跳过自动接线
（运维逃生 + smoke 复刻「未接线 → 红」的 fail 态）。
"""
from __future__ import annotations

import os
import sys


def register_all() -> list:
    """把所有已就绪的 noon live 抓取器注册进生产注册表，返回成功注册的 kind 列表。

    best-effort：单个抓取器 import/注册失败只记 stderr、不阻断其它注册与 `workflow_runners`
    加载（生产入口绝不能因一个 producer 接线问题整体起不来）。真注册成功由 fresh-process
    smoke 复验——这里吞掉异常不会把「没接上」伪装成「接上了」，smoke 会红。
    """
    registered: list = []

    # ── noon 订单（WS-N2.1/WS-58）→ ingest_noon_csv_v2 的 ORDERS 注册表 ──
    try:
        try:
            import noon_order_fetcher as _orders  # scripts 同级（noon_order_fetcher 自插 path）
        except ModuleNotFoundError:
            from hipop.scripts import noon_order_fetcher as _orders  # 包路径回落
        _orders.register_live_producer()
        registered.append("orders")
    except Exception as e:  # noqa: BLE001 — 接线失败不阻断生产入口加载
        print(f"[live_producers] noon 订单 live producer 自动接线失败（生产入口继续，"
              f"run_live 将回落 CSV）: {type(e).__name__}: {e}", file=sys.stderr)

    # ── noon 送仓/ASN（WS-N2.3/WS-60）→ ingest_inbound_staging_v2 的 ASN 注册表 ──
    # 单一收口加 asn 一行。**仅当真实 per-line 送仓源已配置**（asn.asn_url 非空）才接线：
    # 未配置时若强行注册，run_live 调 producer 会因缺配置 raise LiveSourceUnavailable
    # **红灯**（ASN socket 对 LiveSourceUnavailable 不回落 CSV），反而破坏现有 CSV interim
    # 回落路径。故 asn_url 待运营/参谋长确认真实 per-line 源填入后，本接线自动生效。
    try:
        try:
            import noon_asn_scraper as _asn  # scripts 同级（自插 path）
        except ModuleNotFoundError:
            from hipop.scripts import noon_asn_scraper as _asn  # 包路径回落
        if _asn.asn_url_configured():
            _asn.register_live_producer()
            registered.append("asn")
        else:
            print("[live_producers] noon 送仓 asn_url 未配置（per-line 源待确认）→ 暂不自动接线，"
                  "ASN run_live 仍回落 CSV interim（不红灯）。配置 asn.asn_url 后自动生效。",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — 接线失败不阻断生产入口加载
        print(f"[live_producers] noon 送仓 live producer 自动接线失败（生产入口继续，"
              f"run_live 将回落 CSV）: {type(e).__name__}: {e}", file=sys.stderr)

    # 后续抓取器就绪后在此各加一行：
    #   stock（WS-N2.2）→ ingest_noon_stock_csv_v2 的 MY_INVENTORY 注册表
    return registered


# ── import 副作用：生产入口（workflow_runners）加载即接线 ──────────────────────
# 运维/测试逃生：HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1 跳过自动接线（不影响手动
# register_live_producer()）。smoke 用它复刻「未接线 → 红」的 fail-then-pass fail 态。
if os.environ.get("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE") == "1":
    REGISTERED: list = []
else:
    REGISTERED = register_all()
