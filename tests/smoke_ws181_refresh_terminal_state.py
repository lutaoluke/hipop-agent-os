"""smoke_ws181_refresh_terminal_state.py — WS-181 T37/T38 真实完成 + 终态一致

WS-136 最终真实浏览器回归在 isolated 层全绿、却暴露两个生产 bug：
  - T37 ERP 库存刷新 task 终态 error：ingest_erp_stock_v2.py 用整数索引 r[0] 取行，
    sqlite3.Row 支持 r[0] 所以 isolated 绿，PG RealDictCursor 返回 dict → KeyError: 0。
  - T38 销售周期 + 补货重算 task 终态 done_unverified：wf5 runner 丢掉 run_v2 的
    {"ok": False, upstream_not_ready} 信号、save_progress(done) 冒充成功 → verifier
    查到 0 行 → done_unverified；而 chat 受理回执把 done_unverified 当「已完成」，
    boundary 门也把 done_unverified 当完成证据放行「数据已刷新完成」。

本 smoke 钉死四个 fail-then-pass：

  1. T37 dict-row：用模拟 PG RealDictCursor（fetchall 返 dict 行）的连接跑 run_v2，
     改前 r[0] → KeyError: 0（task 会 error）；改后用列名取值 → 正常写库、不崩。

  2. T38a runner fail-closed（上游空）：run_v2 返 {"ok": False} 时，wf5 runner 必须
     raise（让 worker 标 error 带可读原因），不得 save_progress(done) 冒充完成。

  3. T38a runner fail-closed（0 行）：run_v2 返 {alias: 0}（0 行写入）时 runner 必须
     raise，不把空结果当刷新成功；返回正数行时才算成功。

  4. T38b 终态一致：
     a) _workflow_receipt_reply 对 done_unverified 任务**不得**说「已完成」，必须
        明示「未通过校验 / 不代表已成功」；对 verified done 才说「已完成」。
     b) _has_task_done_evidence 对任务级 done_unverified **不算**完成证据，哪怕
        events 里有子步骤 status=done（get_task_with_events 会带「初始化 done」）。

跑法：
  python3 tests/smoke_ws181_refresh_terminal_state.py
  （make test 自动聚合 tests/smoke_*.py，本文件自动并入，无需改 Makefile）
"""
from __future__ import annotations

import os
import re
import sys
import sqlite3
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "hipop", "scripts")
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, SCRIPTS)

# agent.py 顶层 import anthropic；_workflow_reply / _chat_boundary 不依赖它，但为了
# 与其它 smoke 一致、避免任何传递 import 崩，先 stub。
from unittest.mock import MagicMock
for _mod in ("anthropic", "anthropic.types", "anthropic._client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


# ════════════════════════════════════════════════════════════════════════════
# 公共：建临时 sqlite（schema_v2 真表）+ 种 entity / SKU
# ════════════════════════════════════════════════════════════════════════════
def _extract_table(sql_text: str, table: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql_text, re.DOTALL)
    assert m, f"schema_v2.sql 找不到 {table} CREATE TABLE"
    return m.group(0)


def _setup_db(skus_ksa: list) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix="_ws181.db", delete=False)
    db_path = tmp.name
    tmp.close()
    sql_text = open(SCHEMA_V2, encoding="utf-8").read()
    c = sqlite3.connect(db_path)
    for t in ("sales_entities", "wf2_sku", "wf1_stock", "tenant_erp_credentials"):
        c.executescript(_extract_table(sql_text, t))
    c.execute(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active)"
        " VALUES (?,?,?,?,?,?,1)",
        (1, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
    )
    for sku in skus_ksa:
        c.execute(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku) VALUES (?,?,?)",
            (1, "hipop_ksa", sku),
        )
    c.commit()
    c.close()
    return db_path


def _erp_item(sku: str, qty: int, store_name: str = "HIPOP-NOON-KSA") -> dict:
    return {
        "sku_id": sku,
        "stock_total_available_count": qty,
        "platform_sku_ids": [{"platform": {"id": 2},
                              "store": {"name": store_name},
                              "platform_sku_id": "Z" + sku}],
    }


# ── 模拟 PG RealDictCursor：fetchall/fetchone 返回**普通 dict**（r[0] 会 KeyError）──
class _DictCursor:
    def __init__(self, cur):
        self._cur = cur
    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]
    def fetchone(self):
        r = self._cur.fetchone()
        return dict(r) if r is not None else None
    @property
    def description(self):
        return self._cur.description
    def __iter__(self):
        return iter(self.fetchall())


class _DictConn:
    """包一个真 sqlite 连接，但行以 dict 返回 —— 复刻 PG RealDictCursor 行为，
    在 sqlite CI 里也能复现「整数索引 r[0] → KeyError: 0」这条只在 PG 出现的 bug。"""
    def __init__(self, raw):
        raw.row_factory = sqlite3.Row
        self._raw = raw
    def execute(self, sql, params=()):
        return _DictCursor(self._raw.execute(sql, params))
    def commit(self):
        self._raw.commit()
    def close(self):
        self._raw.close()
    def cursor(self):
        return _DictCursor(self._raw.cursor())
    def __enter__(self):
        return self
    def __exit__(self, exc_type, *_):
        if exc_type:
            self._raw.rollback()
        else:
            self._raw.commit()
        self._raw.close()


# ════════════════════════════════════════════════════════════════════════════
# Test 1 — T37: dict-row（PG RealDictCursor）路径不得 KeyError: 0
# ════════════════════════════════════════════════════════════════════════════
def test_t37_pg_dict_rows_no_keyerror():
    """run_v2 内部 SELECT partner_sku 的 fetchall() 返回 dict 行（PG 行为）时，
    改前 {r[0] for r in rows} → KeyError: 0；改后用列名取值 → 正常写库。"""
    import ingest_erp_stock_v2 as erp_stock
    import server.data as sdata
    import sales_entity_v2

    db_path = _setup_db(skus_ksa=["SKU-A", "SKU-B"])
    os.environ.pop("DB_URL", None)
    sdata.DB_PATH = db_path
    sales_entity_v2._data.DB_PATH = db_path

    orig_conn = sdata.conn
    sdata.conn = lambda: _DictConn(sqlite3.connect(sdata.DB_PATH))
    try:
        def fake_fetch(token, wid, **kw):
            # 义乌仓(6) 返回 A/B，其它仓空
            return [_erp_item("SKU-A", 100), _erp_item("SKU-B", 50)] if wid == 6 else []

        # 改前：这里会抛 KeyError: 0（dict 行不支持整数索引）
        result = erp_stock.run_v2(1, token="FAKE", fetch_fn=fake_fetch)
    finally:
        sdata.conn = orig_conn

    # 回读：用普通（非 dict）连接确认真写了行
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = {r["partner_sku"]: dict(r) for r in c.execute("SELECT * FROM wf1_stock").fetchall()}
    c.close()
    os.unlink(db_path)

    assert result.get("hipop_ksa") == 2, f"应写 2 行（A/B），实得 {result}"
    assert "SKU-A" in rows and "SKU-B" in rows, f"A/B 应落库，实得 {list(rows)}"
    assert rows["SKU-A"]["yiwu_qty"] == 100, f"SKU-A 义乌应=100，实得 {rows['SKU-A']}"


def test_t37_known_skus_extraction_uses_column_name():
    """守回归：源码不得再用整数索引 r[0] 取 partner_sku（必须列名访问）。"""
    src = open(os.path.join(SCRIPTS, "ingest_erp_stock_v2.py"), encoding="utf-8").read()
    assert "{r[0] for r in rows}" not in src, (
        "ingest_erp_stock_v2 仍在用 {r[0] for r in rows} —— PG RealDictCursor 会 KeyError: 0"
    )


# ════════════════════════════════════════════════════════════════════════════
# Test 2/3 — T38a: wf5 runner 必须 fail-closed，不把 0 行/未就绪当完成
# ════════════════════════════════════════════════════════════════════════════
def _run_wf5_with(run_v2_return):
    """用打桩的 run_v2 跑 wf5 runner，返回 (raised_exc, summary, saved_progress)。"""
    from hipop.runtime import workflow_runners
    from hipop.workflows import wf_sales_cycle

    runner = workflow_runners.get_runner("wf5_sales_cycle_v2")
    assert runner is not None, "wf5_sales_cycle_v2 必须 @register 在 workflow_runners"

    orig = wf_sales_cycle.run_v2
    wf_sales_cycle.run_v2 = lambda **kw: run_v2_return
    saved = {}
    try:
        try:
            res = runner(
                task_id="t", tenant_id=1, actor={}, spec={},
                progress={}, heartbeat=lambda: None,
                save_progress=lambda p: saved.update(p),
            )
            return None, (res or {}).get("summary"), saved
        except Exception as e:
            return e, None, saved
    finally:
        wf_sales_cycle.run_v2 = orig


def test_t38_runner_failclosed_on_upstream_not_ready():
    """run_v2 返 {"ok": False, upstream_not_ready} → runner 必须 raise，
    不得 save_progress(done) 冒充成功（改前丢弃返回值、伪装 done → verifier 才发现 0 行）。"""
    exc, summary, saved = _run_wf5_with({
        "ok": False, "error": "upstream_not_ready",
        "message": "tenant=1 全部 entity 上游空（wf2_sku 或 wf1_stock 未就绪）",
    })
    assert exc is not None, "上游未就绪时 runner 应 raise（fail-closed），但正常返回了"
    assert not saved.get("done"), "上游未就绪时不得 save_progress(done) 冒充完成"
    assert "upstream_not_ready" in str(exc) or "未就绪" in str(exc), (
        f"fail-closed 错误应说明缺哪路数据，实得：{exc}"
    )


def test_t38_runner_failclosed_on_zero_rows():
    """run_v2 返 {alias: 0}（0 行写入）→ runner 必须 raise，不把空结果当刷新成功。"""
    exc, summary, saved = _run_wf5_with({"hipop_ksa": 0})
    assert exc is not None, "0 行写入时 runner 应 raise（fail-closed），但正常返回了"
    assert not saved.get("done"), "0 行写入时不得 save_progress(done)"


def test_t38_runner_success_on_real_rows():
    """run_v2 返 {alias: 209}（真写了行）→ runner 正常成功，summary 带行数。"""
    exc, summary, saved = _run_wf5_with({"hipop_ksa": 209})
    assert exc is None, f"真写了行时 runner 不该 raise，实得：{exc}"
    assert saved.get("done") is True, "真写了行时应 save_progress(done=True)"
    assert "209" in (summary or ""), f"summary 应反映真实行数 209，实得：{summary}"


# ════════════════════════════════════════════════════════════════════════════
# Test 4a — T38b: 受理回执不得把 done_unverified 当「已完成」
# ════════════════════════════════════════════════════════════════════════════
def _receipt_for(state: str, event_statuses: list) -> str:
    from hipop.server import _workflow_reply
    events = [{"status": s} for s in event_statuses]
    task_data = {"task": {"state": state}, "events": events}
    orig = _workflow_reply._data.get_task_with_events
    _workflow_reply._data.get_task_with_events = lambda tid: task_data
    try:
        return _workflow_reply._workflow_receipt_reply("tid123", "wf5_sales_cycle_v2", "销售周期+补货")
    finally:
        _workflow_reply._data.get_task_with_events = orig


def test_t38_receipt_done_unverified_not_completed():
    """done_unverified 任务（managed runtime：events 含「初始化 done」+ 终态 done_unverified）
    的受理回执**不得**说「已完成」，必须明示未通过校验 / 不代表已成功。"""
    # 真实形状：queued + 初始化(done) + 任务结束(done_unverified)
    reply = _receipt_for("done_unverified", ["queued", "done", "done_unverified"])
    assert "已完成" not in reply, f"done_unverified 不得报『已完成』：\n{reply}"
    assert ("未通过校验" in reply or "不代表已成功" in reply), (
        f"done_unverified 必须明示未通过校验 / 不代表已成功：\n{reply}"
    )


def test_t38_receipt_verified_done_is_completed():
    """verified done（state=done 且终态事件 done）才说「已完成」（正路不回退）。"""
    reply = _receipt_for("done", ["queued", "done", "done"])
    assert "已完成" in reply, f"verified done 应报『已完成』：\n{reply}"


# ════════════════════════════════════════════════════════════════════════════
# Test 4b — T38b: boundary 门不得把 done_unverified 当完成证据
# ════════════════════════════════════════════════════════════════════════════
def test_t38_boundary_done_unverified_not_done_evidence():
    """task 级 done_unverified（即便 events 带子步骤 done）不算完成证据 →
    『数据已刷新完成』仍被拦截。"""
    from hipop.server._chat_boundary import _has_task_done_evidence, check_task_completion_bypass

    # get_task_with_events 真实形状：task.state=done_unverified + events 含 初始化 done
    readback = {
        "name": "get_task_with_events",
        "task": {"state": "done_unverified"},
        "events": [{"status": "queued"}, {"status": "done"}, {"status": "done_unverified"}],
    }
    assert not _has_task_done_evidence([readback]), (
        "done_unverified 任务（含子步骤 done）不应被当完成证据"
    )

    tool_log = [{"name": "run_workflow", "task_id": "x"}, readback]
    warns = check_task_completion_bypass("数据已刷新完成。", tool_log)
    assert warns, "done_unverified 时『数据已刷新完成』应被拦截，但放行了"


def test_t38_boundary_verified_done_allowed():
    """正路不回退：task 级 done + 子步骤 done → 完成证据成立，放行完成声明。"""
    from hipop.server._chat_boundary import _has_task_done_evidence, check_task_completion_bypass

    readback = {
        "name": "get_task_with_events",
        "task": {"state": "done"},
        "events": [{"status": "queued"}, {"status": "done"}, {"status": "done"}],
    }
    assert _has_task_done_evidence([readback]), "verified done 应算完成证据"
    warns = check_task_completion_bypass("数据已刷新完成。", [readback])
    assert not warns, f"verified done 时完成声明应放行，实得 warns={warns}"


if __name__ == "__main__":
    tests = [
        test_t37_pg_dict_rows_no_keyerror,
        test_t37_known_skus_extraction_uses_column_name,
        test_t38_runner_failclosed_on_upstream_not_ready,
        test_t38_runner_failclosed_on_zero_rows,
        test_t38_runner_success_on_real_rows,
        test_t38_receipt_done_unverified_not_completed,
        test_t38_receipt_verified_done_is_completed,
        test_t38_boundary_done_unverified_not_done_evidence,
        test_t38_boundary_verified_done_allowed,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
