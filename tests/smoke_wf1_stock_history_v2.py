"""Smoke: WS-22 — wf1_stock 历史 as_of_date 维度（dated 快照层）。

fail-then-pass 承重墙（钉死三种死法）：
  · 接线缺失：断言 wf1_stock_snapshot_v2 进了 WORKFLOW_REGISTRY + runner 注册表 +
    verifier 注册表，且 callable 可解析 → operator 能按业务日真触发；read_snapshot /
    data.stock_as_of 给 WS-12 历史抽检查询入口。
  · 死代码短路：latest 层 wf1_stock 照常 upsert（只留当前快照），历史靠 dated 层
    wf1_stock_history 多业务日并存 —— 断言两天写完后 wf1_stock 只剩 1 行（最新），
    而 history 有 2 行；latest 读路径（wf5 用的那条 SELECT）仍返回最新日。
  · 占位假数据：as_of_date 必填运行参数，缺失/非法 raise（不回落 today）；冻结时
    把 latest.imported_at 另存 source_imported_at，断言 as_of_date == 传入业务日而
    **不等于** imported_at（这里故意把 imported_at 设成 2099，证明没从它反推）。

改动前（base commit）：db/schema_v2.sql 无 wf1_stock_history 表 + 无
scripts/stock_history.py → _extract_create / import 失败 → smoke FAIL。改动后 → PASS。

跑法：
  python3 tests/smoke_wf1_stock_history_v2.py
  或 make test-wf1-history
（纯 SQLite 临时库，不依赖 PG / 不碰 live hipop.db。）
"""
import os
import sys
import re
import time
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 必须在 import server.data 之前固定到临时 SQLite 库、并清掉 PG。
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)
os.environ["HIPOP_DB"] = _TMP_DB

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1
ALIAS = "hipop_ksa"
SKU = "SKU-A"
D1 = "2026-05-30"
D2 = "2026-05-31"


def _extract_create(table: str) -> str:
    """从 db/schema_v2.sql 抠出指定表的 CREATE TABLE（保证测试 schema 与真 schema 一致）。"""
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 没加 dated 层？）"
    return m.group(0)


def _setup_db():
    c = sqlite3.connect(_TMP_DB)
    # latest 层 + dated 层 + wf2_sku（latest 读路径需要）
    for t in ("sales_entities", "wf2_sku", "wf1_stock", "wf1_stock_history"):
        c.executescript(_extract_create(t))
    c.execute(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
        "VALUES (?,?,?,?,?,?,1)",
        (TENANT, ALIAS, "SA", "Noon", "HIPOP-NOON-KSA", 85),
    )
    c.execute(
        "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, sales_10d) VALUES (?,?,?,?)",
        (TENANT, ALIAS, SKU, 0),
    )
    c.commit()
    c.close()


def _set_latest(noon_saleable, pending, overseas, yiwu, dongguan, imported_at):
    """模拟某业务日 ingest 跑完后的 latest wf1_stock（同一 PK，覆盖式 upsert）。"""
    total = (noon_saleable or 0) + (overseas or 0) + (yiwu or 0) + (dongguan or 0)
    c = sqlite3.connect(_TMP_DB)
    c.execute(
        "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
        "noon_saleable_qty, noon_total_qty, pending_inbound_qty, overseas_total_qty, "
        "yiwu_qty, dongguan_qty, total_stock, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (tenant_id, entity_alias, partner_sku) DO UPDATE SET "
        "noon_saleable_qty=excluded.noon_saleable_qty, noon_total_qty=excluded.noon_total_qty, "
        "pending_inbound_qty=excluded.pending_inbound_qty, overseas_total_qty=excluded.overseas_total_qty, "
        "yiwu_qty=excluded.yiwu_qty, dongguan_qty=excluded.dongguan_qty, "
        "total_stock=excluded.total_stock, imported_at=excluded.imported_at",
        (TENANT, ALIAS, SKU, noon_saleable, noon_saleable, pending, overseas,
         yiwu, dongguan, total, imported_at),
    )
    c.commit()
    c.close()


def _q(sql, params=()):
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def main():
    _setup_db()
    import stock_history

    # 业务日 D1 的 latest（imported_at 故意写成 2099，验 as_of_date 不从它反推）─────
    _set_latest(noon_saleable=10, pending=5, overseas=77, yiwu=99, dongguan=88,
                imported_at="2099-01-01 00:00:00")
    c = sqlite3.connect(_TMP_DB)
    c.row_factory = sqlite3.Row
    r1 = stock_history.record_snapshot(c, TENANT, D1, entity_alias=ALIAS)
    c.close()
    assert r1["rows"] == 1 and r1["as_of_date"] == D1, r1

    # 业务日 D2 的 latest（不同库存）→ 走 run_v2（registry/runner 同一入口，用 data.conn）─
    _set_latest(noon_saleable=2, pending=0, overseas=20, yiwu=0, dongguan=3,
                imported_at="2099-01-02 00:00:00")
    r2 = stock_history.run_v2(TENANT, as_of_date=D2, entity_alias=ALIAS)
    assert r2["rows"] == 1 and r2["as_of_date"] == D2, r2

    # ── 死法#2 死代码短路：latest 只剩最新一行；历史靠 dated 层并存 ──────────
    latest = _q("SELECT * FROM wf1_stock WHERE partner_sku=?", (SKU,))
    assert len(latest) == 1, f"latest wf1_stock 应只有 1 行（当前快照），实有 {len(latest)}"
    assert (latest[0]["noon_saleable_qty"], latest[0]["overseas_total_qty"],
            latest[0]["yiwu_qty"], latest[0]["dongguan_qty"]) == (2, 20, 0, 3), \
        f"latest 应是 D2 的值（被 D1 覆盖），实为 {latest[0]}"

    hist = _q("SELECT * FROM wf1_stock_history WHERE partner_sku=? ORDER BY as_of_date", (SKU,))
    assert len(hist) == 2, f"dated 层应同时保留 D1/D2 两天，实有 {len(hist)} 行"
    assert [h["as_of_date"] for h in hist] == [D1, D2], [h["as_of_date"] for h in hist]

    # ── 按历史日期回溯：两天分别返回不同且正确的各仓数量 ───────────────────
    c = sqlite3.connect(_TMP_DB); c.row_factory = sqlite3.Row
    s1 = stock_history.read_snapshot(c, TENANT, ALIAS, SKU, D1)
    s2 = stock_history.read_snapshot(c, TENANT, ALIAS, SKU, D2)
    c.close()
    assert (s1["noon_saleable_qty"], s1["pending_inbound_qty"], s1["overseas_total_qty"],
            s1["yiwu_qty"], s1["dongguan_qty"]) == (10, 5, 77, 99, 88), s1
    assert (s2["noon_saleable_qty"], s2["pending_inbound_qty"], s2["overseas_total_qty"],
            s2["yiwu_qty"], s2["dongguan_qty"]) == (2, 0, 20, 0, 3), s2
    assert s1 != s2, "两天快照不应相同"

    # ── 死法#3 占位假数据：as_of_date 来自运行参数，不取 today、不从 imported_at 反推 ──
    assert s1["as_of_date"] == D1 and s2["as_of_date"] == D2
    assert s1["source_imported_at"].startswith("2099") and s1["as_of_date"] != s1["source_imported_at"], \
        f"as_of_date 疑似从 imported_at 反推: {s1}"
    # snapshot_at 是"现在"（>=2026-06），与业务日 D1 解耦
    assert s1["snapshot_at"][:10] != D1, f"snapshot_at 不该等于业务日 D1: {s1['snapshot_at']}"

    # 缺失 / 非法 as_of_date → 红灯 raise（不静默回落 today）。
    # 含两类形状对、但**日历上不存在**的占位假业务日（'2026-99-99'、'2026-02-30'）——
    # 验门人红队挖到的洞：旧实现只用正则看形状，会把这种假日写进历史抽检层。
    for bad in (None, "", "today", "2026/05/30", "20260530", "2026-99-99", "2026-02-30"):
        raised = False
        try:
            stock_history.run_v2(TENANT, as_of_date=bad)
        except ValueError:
            raised = True
        assert raised, f"as_of_date={bad!r} 应 raise，而不是回落/写入假业务日"
    # 非法业务日（含日历不存在的）绝不落库 —— history 仍只有 D1/D2 两行
    assert _q("SELECT COUNT(*) c FROM wf1_stock_history")[0]["c"] == 2, "非法 as_of_date 竟写了库"

    # ── latest 读路径不破坏：wf5 用的那条 SELECT 仍读到 D2（最新快照）──────────
    wf5_row = _q(
        "SELECT noon_saleable_qty, pending_inbound_qty, overseas_total_qty, yiwu_qty, dongguan_qty "
        "FROM wf1_stock WHERE tenant_id=? AND entity_alias=? AND partner_sku=?",
        (TENANT, ALIAS, SKU),
    )[0]
    immediate = (wf5_row["noon_saleable_qty"] or 0) + (wf5_row["pending_inbound_qty"] or 0)
    assert immediate == 2, f"wf5 latest 读路径应拿到 D2 即时可用量=2，实为 {immediate}"

    # ── 死法#1 接线缺失：registry + runner + verifier + WS-12 查询入口都接上 ──────
    from hipop.server import api
    assert "wf1_stock_snapshot_v2" in api.WORKFLOW_REGISTRY, "未进 WORKFLOW_REGISTRY → /run-workflow 会 400"
    _, steps, _ = api.WORKFLOW_REGISTRY["wf1_stock_snapshot_v2"]
    fn = api._resolve_callable(steps[0][2])
    assert callable(fn) and fn.__name__ == "run_v2", f"callable 解析失败: {steps}"
    from hipop.runtime import workflow_runners as wr, verifiers as vr
    assert "wf1_stock_snapshot_v2" in wr.list_runners(), "runner 注册表缺 → 后台 worker 跑不到"
    assert "wf1_stock_snapshot_v2" in vr._VERIFIERS, "verifier 注册表缺 → 交付门没确定性校验"

    # WS-12 历史抽检查询入口（data 层）真能按日期取回
    from hipop.server import data
    d_s1 = data.stock_as_of(TENANT, ALIAS, SKU, D1)
    assert d_s1 and d_s1["yiwu_qty"] == 99 and d_s1["as_of_date"] == D1, d_s1
    assert data.stock_history_dates(TENANT, ALIAS) == [D2, D1], data.stock_history_dates(TENANT, ALIAS)

    # ── 确定性 verifier 判真实日历日（不只看 SQL LIKE 形状）─────────────────────
    # 正例：window 内只有 D1/D2 合法业务日 → ok=True、bad_as_of_date=0。
    res_ok = vr.run_verifier("wf1_stock_snapshot_v2", "smoke", TENANT, time.time() - 3600)
    assert res_ok and res_ok["ok"] is True, f"合法业务日 verifier 应过: {res_ok}"
    assert res_ok["evidence"]["bad_as_of_date"] == 0, res_ok

    # 反例（验门人红队洞）：直接往 history 注入一条形状对、但日历上不存在的占位假业务日，
    # 模拟旧 normalize 漏过的行。verifier 必须靠真实日期解析判掉它 → ok=False。
    # 改动前 verifier 只用 SQL LIKE '____-__-__' → 把 '2026-99-99' 当合法 → ok=True，此处会 FAIL；
    # 改动后用 strptime 真解析 → ok=False，PASS。
    bad_started = time.time() - 5
    cbad = sqlite3.connect(_TMP_DB)
    cbad.execute(
        "INSERT INTO wf1_stock_history "
        "(tenant_id, entity_alias, partner_sku, as_of_date, total_stock, snapshot_at) "
        "VALUES (?,?,?,?,?, datetime('now','localtime'))",
        (TENANT, ALIAS, "SKU-BAD", "2026-99-99", 1),
    )
    cbad.commit(); cbad.close()
    res_bad = vr.run_verifier("wf1_stock_snapshot_v2", "smoke", TENANT, bad_started)
    assert res_bad and res_bad["ok"] is False, f"非法日历日 '2026-99-99' verifier 应红灯: {res_bad}"
    assert res_bad["evidence"]["bad_as_of_date"] >= 1, res_bad
    assert "2026-99-99" in res_bad["evidence"]["bad_samples"], res_bad

    print("✓ dated 层 wf1_stock_history 按 (tenant,entity,sku,as_of_date) 多日并存")
    print("✓ latest wf1_stock 仍只留当前快照（D2），wf5 读路径不破坏")
    print("✓ 按 D1/D2 回溯分别返回不同且正确的官方仓/海外仓/义乌/东莞/pending 数量")
    print("✓ as_of_date 来自运行参数：缺失/非法/日历不存在(2026-99-99/2026-02-30)红灯 raise，不取 today、不反推 imported_at")
    print("✓ verifier 用真实日期解析判业务日：合法 ok=True，注入 '2026-99-99' 假日 ok=False（不只看 SQL LIKE 形状）")
    print("✓ wf1_stock_snapshot_v2 进 registry + runner + verifier；WS-12 查询入口可用")
    print("\n6/6 passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass
