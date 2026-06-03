"""LIVE smoke（手动跑，需本机活紫鸟 + 已登录 noon）—— WS-N2.1 / WS-58 真 page→真 rows。

刻意不叫 `smoke_*.py`：`make test` 只自动发现 `tests/smoke_*.py`，本文件**不**进 CI 全量
（CI 无紫鸟会硬失败）。它是「真 page→真 rows」的 fail-then-pass 端到端证明，由 Coder 在
本机活紫鸟下手动跑、把命令与输出回贴 issue。

证明（acceptance #1/#2/#3）：
  · 改前（未注册 producer）：订单 ingest `run_live` 无 producer + 无 CSV → 红灯回落，
    `source != "live"`。
  · 改后（`register_live_producer()` 注册真抓取器）：`get_platform_session(tenant,"noon")`
    拿真实已登录 page → `_fetch_raw_orders` 分页 POST noon Sales Dashboard → 映射 WS-34 行
    → 同一 `_aggregate`/`_upsert` 落临时库，`source == "live"` 且 orders > 0。
  · 登录失效 / 字段缺 / 接口改版 → 抓取器红灯 blocked（见 deterministic smoke），本 live 跑
    只验「活紫鸟 + 已登录」这条 happy path 真能跑出真行。

跑法：
  python3 tests/live_noon_order_fetcher.py
  （落临时 SQLite，不碰 PG / 不碰 live hipop.db；无紫鸟时打印 blocked 并 exit 0 跳过。）
"""
import os
import re
import sys
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

_DB = tempfile.NamedTemporaryFile(suffix="_order_live_real.db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402
import noon_order_fetcher as F  # noqa: E402
import ingest_noon_csv_v2 as noon  # noqa: E402
from hipop.server import _platform_browser as pb  # noqa: E402
from server import data as _data  # noqa: E402

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"   # KSA 单主体：强制路由，避免依赖临时库的 sales_entities


def _extract_create(table):
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE"
    return m.group(0)


def _reset_db():
    c = sqlite3.connect(_DB)
    try:
        for t in ("wf2_orders", "wf2_sku"):
            c.executescript(f"DROP TABLE IF EXISTS {t};")
            c.executescript(_extract_create(t))
        c.commit()
    finally:
        c.close()


def _count(table):
    c = sqlite3.connect(_DB)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def main():
    _data.set_current_tenant(TENANT)
    # 前置：紫鸟 webdriver 在听吗？不在 → 这是 live-only 用例，打印 blocked 跳过（不算失败）。
    try:
        pb._assert_webdriver_up()
    except pb.PlatformBrowserError as e:
        print(f"[skip] 无本机活紫鸟（{e}）—— 这是 live-only 用例，请在本机起紫鸟 web_driver "
              f"后重跑；CI 全量走 deterministic smoke_noon_order_fetcher.py。")
        return 0

    C.set_live_row_producer(C.ORDERS, None)
    try:
        # ── 改前：未注册 → run_live 无 producer + 无 CSV → 红灯，source != live ──
        F.unregister_live_producer()
        _reset_db()
        before_live = False
        with tempfile.TemporaryDirectory() as empty_dir:
            try:
                res = noon.run_live(TENANT, inbox=empty_dir, entity_alias=ALIAS)
                before_live = (res.get("source") == "live")
            except noon.LiveSourceUnavailable:
                before_live = False
        assert not before_live, "改前不应有 live 源"
        assert _count("wf2_orders") == 0, "改前红灯路径不得凭空写订单行"
        print("✓ 改前：订单 ingest 无 producer → 无 live（source != 'live'），库里无凭空行")

        # ── 改后：注册真抓取器 → run_live 真 page→真 rows → source == live ──
        F.register_live_producer()  # 真 get_platform_session + 真 _fetch_raw_orders
        assert C.get_live_row_producer(C.ORDERS) is not None, "注册后 producer 应就位"
        _reset_db()
        res = noon.run_live(TENANT, entity_alias=ALIAS)
        print(f"  run_live result: {res}")
        assert res.get("source") == "live", f"注册后应走真 live 源: {res}"
        assert res.get("orders", 0) > 0, f"真 page 应抓出 >0 订单行: {res}"
        n_db = _count("wf2_orders")
        assert n_db == res["orders"], f"落库行数应 == live 行数: {n_db} vs {res['orders']}"

        # 抽样几条真行回看（证明绑定真实 page、真字段、非编造）。
        c = sqlite3.connect(_DB)
        c.row_factory = sqlite3.Row
        sample = [dict(r) for r in c.execute(
            "SELECT partner_sku, noon_sku, item_nr, order_date, status, "
            "is_cancelled, is_return, seller_price, currency, destination "
            "FROM wf2_orders LIMIT 5")]
        c.close()
        print(f"✓ 改后：真 page → {res['orders']} 真 live 订单行（source=live），落库 {n_db} 行。样例：")
        for r in sample:
            print("   ", dict(r))
        print(f"\n✓ LIVE 通过：register 前 source!='live'，register 后真 page 跑出 source=='live' "
              f"({res['orders']} 行 / {res.get('skus')} SKU)")
        return 0
    finally:
        F.unregister_live_producer()
        C.set_live_row_producer(C.ORDERS, None)


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        try:
            os.unlink(_DB)
        except OSError:
            pass
    sys.exit(rc)
