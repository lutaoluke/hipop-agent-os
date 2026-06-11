"""T27 replenishment evidence smoke: cache-zero must not become a confident answer.

Acceptance covered here:
- A listed SKU whose cached aggregate path is missing/all-zero must use a live
  authoritative source before answering replenishment/pipeline questions.
- The answer may conclude "no replenishment needed", but the reason must cite
  source+time evidence for pending shipment, in-transit, ETA, Noon stock,
  Dongguan stock, and forecast daily sales.
- If the live source fails, the chat path must block numeric/conclusive answers
  instead of turning cached zeros into "no risk / no replenishment".

Run:
  python3 tests/smoke_t27_replenishment_evidence.py
  make test-one F=tests/smoke_t27_replenishment_evidence.py
"""
from __future__ import annotations

import importlib
import os
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SKU = "TBU0010A"
QUESTION = f"case=T27. 请查 {SKU} 本周补货建议、当前 pipeline、目标 pipeline、风险标签和紧急度。"


def _init_cache_zero_db(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE sales_entities (
            id INTEGER PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            country TEXT NOT NULL,
            platform TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    con.execute("""
        CREATE TABLE wf2_sku (
            tenant_id INTEGER,
            entity_alias TEXT,
            partner_sku TEXT,
            title TEXT,
            is_listed INTEGER,
            sales_30d REAL,
            forecast_30d REAL,
            latest_profit_rate REAL,
            imported_at TEXT,
            as_of_date TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        )
    """)
    con.execute("""
        CREATE TABLE wf1_stock (
            tenant_id INTEGER,
            entity_alias TEXT,
            partner_sku TEXT,
            noon_saleable_qty INTEGER,
            pending_inbound_qty INTEGER,
            overseas_total_qty INTEGER,
            yiwu_qty INTEGER,
            dongguan_qty INTEGER,
            total_stock INTEGER,
            imported_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        )
    """)
    con.execute("""
        CREATE TABLE wf3_logistics_hub_v2 (
            tenant_id INTEGER,
            sku TEXT,
            in_transit_total_qty INTEGER,
            total_transit_qty INTEGER,
            transit_batches_json TEXT,
            groups_json TEXT,
            has_stuck_batch INTEGER DEFAULT 0,
            needs_ops_input INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE wf5_sales_cycle (
            tenant_id INTEGER,
            entity_alias TEXT,
            partner_sku TEXT,
            trend TEXT,
            daily_rate REAL,
            forecast_30_days REAL,
            risk_label TEXT,
            current_pipeline INTEGER,
            target_pipeline INTEGER,
            wf5_replenish_qty INTEGER,
            lost_replenish_qty INTEGER,
            weekly_total_replenish INTEGER,
            urgency TEXT,
            ops_advice TEXT,
            updated_at TEXT,
            PRIMARY KEY (tenant_id, entity_alias, partner_sku)
        )
    """)
    con.execute(
        "INSERT INTO sales_entities (id, tenant_id, alias, country, platform, active) "
        "VALUES (1, 1, 'hipop_ksa', 'SA', 'noon', 1)"
    )
    con.execute(
        "INSERT INTO wf2_sku VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "hipop_ksa", SKU, "T27 target SKU", 1, 0, 0, 0.25,
         "2026-06-01 00:00:00", "2026-06-01"),
    )
    # Deliberately all-zero aggregate rows: this used to be misread as truth.
    con.execute(
        "INSERT INTO wf1_stock VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "hipop_ksa", SKU, 0, 0, 0, 0, 0, 0,
         "2026-06-01 00:00:00", "2026-06-01 00:00:00"),
    )
    con.execute(
        "INSERT INTO wf3_logistics_hub_v2 "
        "(tenant_id, sku, in_transit_total_qty, total_transit_qty, transit_batches_json, groups_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (1, SKU, 0, 0, "[]", "[]", "2026-06-01 00:00:00"),
    )
    con.execute(
        "INSERT INTO wf5_sales_cycle VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "hipop_ksa", SKU, "无销量", 0, 0, "无风险", 0, 0, 0, 0, 0,
         "无需采购", "缓存显示无需补货", "2026-06-01 00:00:00"),
    )
    con.commit()
    con.close()


def _reload_modules(db_path: str):
    os.environ["HIPOP_DB"] = db_path
    os.environ.pop("DB_URL", None)
    os.environ["LLM_PROVIDER"] = "smoke"

    from hipop.server import data
    importlib.reload(data)
    from hipop.server import replenishment_evidence
    importlib.reload(replenishment_evidence)
    from hipop.server import agent
    importlib.reload(agent)
    return data, replenishment_evidence, agent


def _with_temp_db(fn):
    orig_db = os.environ.get("HIPOP_DB")
    orig_db_url = os.environ.get("DB_URL")
    orig_provider = os.environ.get("LLM_PROVIDER")
    tmp = tempfile.NamedTemporaryFile(suffix="_t27.db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        _init_cache_zero_db(tmp_path)
        return fn(tmp_path)
    finally:
        if orig_db is None:
            os.environ.pop("HIPOP_DB", None)
        else:
            os.environ["HIPOP_DB"] = orig_db
        if orig_db_url is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = orig_db_url
        if orig_provider is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = orig_provider
        try:
            from hipop.server import replenishment_evidence
            replenishment_evidence.set_replenishment_live_source(None)
        except Exception:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def test_t27_cache_zero_uses_live_authoritative_evidence():
    """All-zero cache + live fixture with values -> answer uses live evidence."""

    def run(db_path: str):
        _data, ev, agent = _reload_modules(db_path)

        def live_fixture(sku: str, store: str, tenant_id: int, entity_alias: str):
            assert sku == SKU
            assert store.upper() == "KSA"
            assert tenant_id == 1
            assert entity_alias == "hipop_ksa"
            return {
                "ok": True,
                "source": "T27 live fixture ERP/noon authority",
                "fetched_at": "2026-06-09T03:45:00Z",
                "pending_shipment_qty": 10,
                "in_transit_qty": 7,
                "eta": "2026-06-14",
                "noon_saleable_qty": 1,
                "dongguan_qty": 5,
                "forecast_daily": 0.105,
                "forecast_30d": 3,
                "weekly_replenish": 0,
                "risk_label": "低风险",
                "urgency": "无需采购",
                "recommendation": "不需要补货",
            }

        ev.set_replenishment_live_source(live_fixture)
        result = agent.chat(
            [{"role": "user", "content": QUESTION}],
            {"store": "KSA", "current_user": "tester", "current_role": "运营", "tenant_id": 1},
        )
        reply = result["reply"]

        assert result.get("judge_method") == "deterministic_replenishment_sku_router", result
        assert result.get("tools_used") == ["query_replenishment_sku"], result
        assert SKU in reply, reply
        assert re.search(r"待发[^0-9]{0,6}10", reply), reply
        assert re.search(r"在途[^0-9]{0,6}7", reply), reply
        assert ("2026-06-14" in reply) or ("6.14" in reply), reply
        assert re.search(r"Noon[^0-9]{0,6}1", reply, re.IGNORECASE), reply
        assert re.search(r"东莞[^0-9]{0,6}5", reply), reply
        assert "0.105" in reply and re.search(r"约\s*3\s*/?\s*月|约\s*3\s*件/月", reply), reply
        assert "T27 live fixture ERP/noon authority" in reply, reply
        assert "2026-06-09T03:45:00Z" in reply, reply
        assert "不需要补货" in reply or "无补货建议" in reply, reply
        assert "pipeline" in reply.lower(), reply
        assert "目标 pipeline 0" not in reply and "当前 pipeline 0" not in reply, reply
        refs = result.get("references") or []
        assert any(r.get("table") == "T27 live fixture ERP/noon authority" for r in refs), refs

    return _with_temp_db(run)


def test_t27_live_unavailable_blocks_cached_zero_answer():
    """All-zero cache + live failure -> no confident zero/no-risk answer."""

    def run(db_path: str):
        _data, ev, agent = _reload_modules(db_path)

        def live_failure(sku: str, store: str, tenant_id: int, entity_alias: str):
            return {
                "ok": False,
                "error": "live_source_unavailable",
                "message": "ERP/noon realtime authority unavailable in this run",
                "source": "T27 live fixture ERP/noon authority",
                "fetched_at": "2026-06-09T03:45:00Z",
            }

        ev.set_replenishment_live_source(live_failure)
        result = agent.chat(
            [{"role": "user", "content": QUESTION}],
            {"store": "KSA", "current_user": "tester", "current_role": "运营", "tenant_id": 1},
        )
        reply = result["reply"]

        assert result.get("tools_used") == ["query_replenishment_sku"], result
        assert any(x in reply for x in ("无法", "不可用", "失败", "不能确认", "需要刷新")), reply
        assert not re.search(r"无需补货|不需要补货|无风险|风险低", reply), reply
        assert not re.search(r"待发[^0-9]{0,6}0|在途[^0-9]{0,6}0|Noon[^0-9]{0,6}0|东莞[^0-9]{0,6}0", reply), reply

    return _with_temp_db(run)


def test_replenishment_safety_blocks_blocked_numeric_claim():
    """LLM fallback cannot turn a blocked replenishment tool result into numbers."""
    from hipop.server import _safety

    reply = f"{SKU} 无需补货，当前在途 0、Noon 仓 0、东莞 0，风险低。"
    _, warns = _safety.sanitize_reply(
        reply,
        ["query_replenishment_sku"],
        tool_log=[{
            "name": "query_replenishment_sku",
            "args": {"sku": SKU, "store": "KSA"},
            "result_replenishment_blocked_skus": [SKU],
        }],
        question=QUESTION,
    )
    assert any("补货" in w and SKU in w for w in warns), warns


def test_t27_prod_path_wires_erp_live_source():
    """Production path: tool_query_replenishment_sku must wire the ERP live
    source itself — without any test calling set_replenishment_live_source().

    Simulates wf1 having run (real Noon/Dongguan stock in wf1_stock) while the
    logistics/replenishment aggregates are still all-zero, so the live source is
    required. The in-transit/pending split and ETA come from the live ERP tool
    (query_sku_live), proving the prod adapter is wired, not a test fixture."""

    def run(db_path: str):
        _data, ev, agent = _reload_modules(db_path)

        # wf1 already ran: real Noon (1) + Dongguan (5) stock present. Logistics
        # and replenishment aggregates remain all-zero -> live source required.
        con = sqlite3.connect(db_path)
        con.execute(
            "UPDATE wf1_stock SET noon_saleable_qty=1, dongguan_qty=5 "
            "WHERE tenant_id=1 AND entity_alias='hipop_ksa' AND partner_sku=?",
            (SKU,),
        )
        con.commit()
        con.close()

        # No global fixture injection — prod path must wire ERP itself.
        assert ev.get_replenishment_live_source() is None

        # Stub ERP token so query_sku_live proceeds past auth.
        orig_token = agent._erp_token_or_error
        agent._erp_token_or_error = lambda tid: ("fake-token", None)

        # Stub the ERP order fetch with a realistic in-transit/pending split:
        # PO-001/PO-002 = 10 pending (no tracking), PO-003 = 7 in-transit (tracking + ETA).
        hipop_dir = REPO / "hipop"
        if str(hipop_dir) not in sys.path:
            sys.path.insert(0, str(hipop_dir))
        from workflows import wf_logistics_status as wls
        orig_collect = wls.collect_sku_orders

        def fake_collect(sku, token):
            in_transit = [
                {"order_no": "PO-001", "qty": 5, "tracking_no": "", "delivery_at": ""},
                {"order_no": "PO-002", "qty": 5, "tracking_no": "", "delivery_at": ""},
                {"order_no": "PO-003", "qty": 7, "tracking_no": "YT123456",
                 "delivery_at": "2026-06-14"},
            ]
            return in_transit, []

        wls.collect_sku_orders = fake_collect
        try:
            result = agent.chat(
                [{"role": "user", "content": QUESTION}],
                {"store": "KSA", "current_user": "tester", "current_role": "运营", "tenant_id": 1},
            )
        finally:
            agent._erp_token_or_error = orig_token
            wls.collect_sku_orders = orig_collect

        reply = result["reply"]
        assert result.get("judge_method") == "deterministic_replenishment_sku_router", result
        assert result.get("tools_used") == ["query_replenishment_sku"], result
        assert re.search(r"待发[^0-9]{0,6}10", reply), reply
        assert re.search(r"在途[^0-9]{0,6}7", reply), reply
        assert ("2026-06-14" in reply) or ("6.14" in reply), reply
        assert re.search(r"Noon[^0-9]{0,6}1", reply, re.IGNORECASE), reply
        assert re.search(r"东莞[^0-9]{0,6}5", reply), reply

    return _with_temp_db(run)


if __name__ == "__main__":
    tests = [
        test_t27_cache_zero_uses_live_authoritative_evidence,
        test_t27_live_unavailable_blocks_cached_zero_answer,
        test_replenishment_safety_blocks_blocked_numeric_claim,
        test_t27_prod_path_wires_erp_live_source,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"✗ {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
