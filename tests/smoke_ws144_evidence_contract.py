"""smoke_ws144_evidence_contract.py — WS-144/E1.1 统一证据 / 执行记录契约

把"工作台数字"与"执行"统一成可追溯证据，并迁移 1 个查询工具 + 1 个执行工具作样板。

fail-then-pass（改前 FAIL / 改后 PASS）：

  改前（无契约）：
    - 查询样板 _format_total_stock_topn_reply 即使 tool_result 无证据(来源/取数时间/口径)
      也照样把库存数字渲染出来（占位假数据 + 无来源裸数）。
    - 回复里没有"来源"/"取数时间"字样，运营无从追溯。
    - 执行样板 tool_run_workflow legacy 路径返回里没有 execution_record，
      也没同步落 durable event → 只说"已创建/已启动"而无可查的真实步骤。

  改后（本契约）：
    - 证据三要素任一缺失 → 回答层 fail-closed 不出数。
    - 证据齐 → 回复含来源 + 取数时间 + 口径，数字可追溯。
    - 执行工具返回真实 execution_record（真 task_id + ≥1 durable 步骤），
      hint 不再是裸"已启动"，缺真实记录则 fail-closed 标 create_failed。

跑法：
  python3 tests/smoke_ws144_evidence_contract.py
  make test-one F=tests/smoke_ws144_evidence_contract.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

os.environ["HIPOP_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.pop("DB_URL", None)

TENANT_ID = 1

_AGENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id     BIGINT,
  task_id       TEXT NOT NULL,
  step_no       INT NOT NULL,
  step_name     TEXT NOT NULL,
  status        TEXT NOT NULL,
  message       TEXT,
  actor_user_id TEXT,
  actor_email   TEXT,
  actor_role    TEXT,
  actor_source  TEXT,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_results: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    msg = f"  [{status}] {name}"
    if not cond and detail:
        msg += f"\n         ↳ {detail}"
    print(msg)
    _results.append((name, cond))


def _fresh_db():
    import hipop.server.data as _data
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    _data.DB_PATH = path
    _data._engine = None
    conn = _data.conn()
    conn.execute(_AGENT_EVENTS_DDL)
    conn.commit()
    return _data


# ════════════════════════════════════════════════════════════════════════════════
# 1) 契约单元：查询证据
# ════════════════════════════════════════════════════════════════════════════════

def test_query_evidence_unit():
    print("\n── test_query_evidence_unit ─────────────────────────────────")
    from hipop.scripts.evidence_contract import (
        build_query_evidence, assert_query_evidence, render_evidence_suffix,
        ContractViolation, SOURCE_ERP, SOURCE_MERGED, SOURCE_NOON,
    )

    # 三要素缺任一 → raise
    for kw, label in [
        ({"source": None, "fetched_at": "2026-06-09", "coverage": "x"}, "缺来源"),
        ({"source": SOURCE_ERP, "fetched_at": None, "coverage": "x"}, "缺取数时间"),
        ({"source": SOURCE_ERP, "fetched_at": "2026-06-09", "coverage": ""}, "缺口径"),
        ({"source": "bogus", "fetched_at": "t", "coverage": "x"}, "非法来源"),
    ]:
        try:
            build_query_evidence(**kw)
            check(f"{label} 应 raise", False, f"未抛异常: {kw}")
        except ContractViolation:
            check(f"{label} → ContractViolation", True)

    # merged 必须有 sub_sources
    try:
        build_query_evidence(source=SOURCE_MERGED, fetched_at="t", coverage="x")
        check("merged 无 sub_sources 应 raise", False)
    except ContractViolation:
        check("merged 无 sub_sources → ContractViolation", True)

    ev = build_query_evidence(
        source=SOURCE_MERGED, fetched_at="2026-06-09T08:00:00",
        coverage="KSA total_stock 口径", sub_sources=[SOURCE_NOON, SOURCE_ERP],
    )
    suffix = render_evidence_suffix(ev)
    check("render 含来源", "来源" in suffix, suffix)
    check("render 含取数时间", "取数时间" in suffix and "2026-06-09" in suffix, suffix)
    check("render 含口径", "口径" in suffix, suffix)
    # assert_query_evidence 对不完整字典 fail
    try:
        assert_query_evidence({"source": SOURCE_ERP})
        check("不完整证据 assert 应 raise", False)
    except ContractViolation:
        check("不完整证据 assert → ContractViolation", True)


# ════════════════════════════════════════════════════════════════════════════════
# 2) 契约单元：执行记录
# ════════════════════════════════════════════════════════════════════════════════

def test_execution_record_unit():
    print("\n── test_execution_record_unit ───────────────────────────────")
    from hipop.scripts.evidence_contract import (
        build_execution_record, assert_execution_real, render_execution_suffix,
        ContractViolation, EXEC_RUNNING, EXEC_DONE, EXEC_ERROR, EXEC_CREATE_FAILED,
    )

    # running 但无 task_id → raise（只说"已启动"没有真实任务）
    try:
        build_execution_record(status=EXEC_RUNNING, task_id=None, step_count=1)
        check("running 无 task_id 应 raise", False)
    except ContractViolation:
        check("running 无 task_id → ContractViolation", True)

    # running 有 task_id 但 0 步骤 → raise（接线缺失）
    try:
        build_execution_record(status=EXEC_RUNNING, task_id="abc123", step_count=0)
        check("running 0 步骤应 raise", False)
    except ContractViolation:
        check("running 0 步骤 → ContractViolation", True)

    # error 无 reason → raise（失败必须有原因）
    try:
        build_execution_record(status=EXEC_ERROR, task_id="abc123", step_count=2)
        check("error 无 reason 应 raise", False)
    except ContractViolation:
        check("error 无 reason → ContractViolation", True)

    # 合法 running 记录
    rec = build_execution_record(
        status=EXEC_RUNNING, task_id="abc12345", workflow="wf1_stock",
        steps=[{"step_no": 0, "step_name": "任务排队", "status": "queued"}],
    )
    rendered = render_execution_suffix(rec)
    check("render 含真实 task_id", "abc12345" in rendered, rendered)
    check("render 不是裸『已启动』", "已启动" not in rendered, rendered)
    check("render 含步骤数", "步骤数" in rendered, rendered)

    # create_failed：允许无 task_id，但必须带原因
    rec_f = build_execution_record(
        status=EXEC_CREATE_FAILED, workflow="wf1_stock", reason="ERP 登录失败",
    )
    rendered_f = render_execution_suffix(rec_f)
    check("create_failed 渲染含原因", "ERP 登录失败" in rendered_f, rendered_f)

    # assert_execution_real 对裸"已启动"字符串/非记录 → raise
    try:
        assert_execution_real("已启动")
        check("非记录 assert 应 raise", False)
    except ContractViolation:
        check("非记录(已启动字符串) assert → ContractViolation", True)


# ════════════════════════════════════════════════════════════════════════════════
# 3) 查询样板：formatter 接线（无证据 fail-closed / 有证据可追溯）
# ════════════════════════════════════════════════════════════════════════════════

def test_query_formatter_enforces_evidence():
    print("\n── test_query_formatter_enforces_evidence ───────────────────")
    import hipop.server.agent as _agent
    from hipop.scripts.evidence_contract import (
        build_query_evidence, SOURCE_MERGED, SOURCE_NOON, SOURCE_ERP,
    )

    items = [
        {"partner_sku": "SKU-A", "total_stock": 1234,
         "noon_saleable_qty": 800, "pending_inbound_qty": 400},
    ]

    # 无证据 → fail-closed 不出数（改前会照出数字 1,234）
    no_ev = {"fail_closed": False, "store": "KSA", "items": items}
    reply_no_ev = _agent._format_total_stock_topn_reply("KSA", no_ev)
    check("无证据不渲染裸数字 1,234",
          "1,234" not in reply_no_ev,
          reply_no_ev[:200])
    check("无证据回复说明不出数原因",
          ("证据" in reply_no_ev or "不出数" in reply_no_ev),
          reply_no_ev[:200])

    # 有证据 → 出数 + 来源/取数时间/口径可追溯
    ev = build_query_evidence(
        source=SOURCE_MERGED, fetched_at="2026-06-09T08:00:00",
        coverage="KSA total_stock=noon+海外+国内+pending；Top1",
        sub_sources=[SOURCE_NOON, SOURCE_ERP],
    )
    with_ev = {"fail_closed": False, "store": "KSA", "items": items, "evidence": ev}
    reply_ev = _agent._format_total_stock_topn_reply("KSA", with_ev)
    check("有证据渲染数字", "1,234" in reply_ev, reply_ev[:300])
    check("有证据回复含『来源』", "来源" in reply_ev, reply_ev[:300])
    check("有证据回复含『取数时间』并带时间戳",
          "取数时间" in reply_ev and "2026-06-09" in reply_ev, reply_ev[:300])
    check("有证据回复含『口径』", "口径" in reply_ev, reply_ev[:300])


# ════════════════════════════════════════════════════════════════════════════════
# 4) 执行样板：tool_run_workflow 真实执行记录（wire-check，不旁路）
# ════════════════════════════════════════════════════════════════════════════════

def test_run_workflow_attaches_execution_record():
    print("\n── test_run_workflow_attaches_execution_record ──────────────")
    _data = _fresh_db()
    import hipop.server.agent as _agent
    from hipop.runtime import workflow_runners as _runners
    from hipop.server import api as _api

    _agent._chat_tenant.set(TENANT_ID)
    _agent._chat_scope.set({"tenant_id": TENANT_ID, "store": "KSA", "user": "test",
                            "current_user_email": "t@e.com", "current_role": "ops"})

    # 强制走 legacy 路径（避免 spawn_task 起子进程），并让线程目标 no-op（不真跑工作流）
    orig_list = _runners.list_runners
    orig_run = _api._run_workflow
    _runners.list_runners = lambda: []
    _api._run_workflow = lambda *a, **k: None
    try:
        result = _agent.tool_run_workflow("wf1_stock")
    finally:
        _runners.list_runners = orig_list
        _api._run_workflow = orig_run

    check("返回 ok", result.get("ok") is True, str(result)[:200])
    task_id = result.get("task_id")
    check("返回真实 task_id", bool(task_id), str(result)[:200])

    rec = result.get("execution_record")
    check("返回含 execution_record", isinstance(rec, dict), str(result)[:200])
    if isinstance(rec, dict):
        check("execution_record.status=running", rec.get("status") == "running", str(rec))
        check("execution_record.task_id 与返回一致", rec.get("task_id") == task_id, str(rec))
        check("execution_record 步骤数 ≥1", (rec.get("step_count") or 0) >= 1, str(rec))

    # 真实 durable 证据：agent_events 里查得到 ≥1 步
    _data.set_current_tenant(TENANT_ID)
    events = _data.get_events_after(task_id, 0)
    check("durable agent_events ≥1（真实记录非占位）", len(events) >= 1,
          f"events={events}")

    hint = result.get("hint") or ""
    check("hint 含真实 task_id", task_id in hint, hint)
    check("hint 不是裸『已启动』", "已启动" not in hint, hint)

    # 用契约独立复验返回的记录可信（消费端真读）
    from hipop.scripts.evidence_contract import assert_execution_real, ContractViolation
    try:
        assert_execution_real(rec)
        check("execution_record 通过 assert_execution_real", True)
    except ContractViolation as e:
        check("execution_record 通过 assert_execution_real", False, str(e))


def main():
    test_query_evidence_unit()
    test_execution_record_unit()
    test_query_formatter_enforces_evidence()
    test_run_workflow_attaches_execution_record()

    print("\n" + "=" * 60)
    failed = [n for n, ok in _results if not ok]
    total = len(_results)
    if failed:
        print(f"✗ {len(failed)}/{total} 断言 FAIL：")
        for n in failed:
            print(f"   - {n}")
        sys.exit(1)
    print(f"✓ WS-144 证据契约 smoke 全绿（{total} 断言）")


if __name__ == "__main__":
    main()
