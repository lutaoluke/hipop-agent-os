"""smoke_t37_stock_refresh.py — WS-116 T37 四交付点 fail-then-pass

FAIL（改前）：
  - test_route_*：_deterministic_workflow_request("刷库存"/"刷ERP库存"/"刷6仓库存"
    /"请帮我刷库存（ERP 6仓）") 返回 None（不命中现有动词列表）
  - test_safety_*：sanitize_reply 只加 banner，不删假任务号/假成功段
  - test_preflight_*：run_v2 无 wf2_sku preflight，空 SKU master 不报错、
    ERP 未返回的已知 SKU 保留旧库存冒充本轮刷新

PASS（改后）：
  1. 路由：四种刷库存意图 → wf1_stock_v2，不经 LLM
  2. Safety：假任务号/假成功段被删除/替换为 [本轮未创建刷新任务 / 未启动后台流程]
  3. Preflight：SKU master 为空 → RuntimeError；ERP 未返 SKU → qty=0 落库
  4. 回归：物流路由不受影响；T36/T38/T21 假启动守卫不受影响

跑法：
  python3 tests/smoke_t37_stock_refresh.py
  # 或
  make test   （自动聚合，无需改 Makefile）
"""
from __future__ import annotations

import os
import sys
import json
import re
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

# ── Stub anthropic so agent.py can be imported without the real package ──
# smoke_governance.py uses the same pattern in the CI env where anthropic IS installed.
# Locally (dev / CI without full deps) we stub to let the routing function tests pass.
from unittest.mock import MagicMock
for _mod in ("anthropic", "anthropic.types", "anthropic._client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# ── safety 直接导入（轻量，无 anthropic 依赖）─────────────────────────────
from hipop.server._safety import sanitize_reply

# ── agent 路由函数（agent.py 顶层 import anthropic，但 _deterministic_workflow_request
#    本身不调网络；MagicMock stub 已确保 import 成功）────────────────────────
import hipop.server.agent as agent_module
from hipop.server.agent import (
    _deterministic_workflow_request,
    _maybe_inject_missing_rates,
    _last_sku_rate_stats,
)


# ── 交付点 1：确定性路由 ─────────────────────────────────────────────────────

def test_route_刷库存():
    r = _deterministic_workflow_request("刷库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"'刷库存' 应路由到 wf1_stock_v2，实际: {r}"
    )


def test_route_刷ERP库存():
    r = _deterministic_workflow_request("刷ERP库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"'刷ERP库存' 应路由到 wf1_stock_v2，实际: {r}"
    )


def test_route_刷6仓库存():
    r = _deterministic_workflow_request("刷6仓库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"'刷6仓库存' 应路由到 wf1_stock_v2，实际: {r}"
    )


def test_route_t37_exact_prompt():
    """T37 原始 prompt 的刷库存部分。"""
    r = _deterministic_workflow_request("请帮我刷库存（ERP 6仓），并说明会更新哪些表。")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"T37 exact prompt 应路由到 wf1_stock_v2，实际: {r}"
    )


def test_route_刷新库存_still_works():
    """回归：已有的'刷新库存'意图不受影响。"""
    r = _deterministic_workflow_request("帮我把库存刷新一下")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"'帮我把库存刷新一下' 回归失败，实际: {r}"
    )


def test_route_刷ERP_without_库存_no_crash():
    """刷ERP（无库存字样）不应崩溃也不应路由到 wf1_stock_v2。"""
    r = _deterministic_workflow_request("刷ERP商品数据")
    # 没有"库存"关键字，不应匹配库存刷新
    assert r is None or r.get("workflow") != "wf1_stock_v2", (
        f"'刷ERP商品数据' 不应路由到 wf1_stock_v2（没有库存关键字），实际: {r}"
    )


# ── 交付点 1 回归：物流/销量路由不受影响 ────────────────────────────────────

def test_route_物流_still_wf3():
    r = _deterministic_workflow_request("帮我刷新物流数据")
    assert r is not None and r.get("workflow") == "wf3_logistics_v2", (
        f"物流刷新回归失败，应 wf3_logistics_v2，实际: {r}"
    )


def test_route_扫物流_still_wf3():
    r = _deterministic_workflow_request("你扫下 erp 物流信息")
    assert r is not None and r.get("workflow") == "wf3_logistics_v2", (
        f"扫物流回归失败，应 wf3_logistics_v2，实际: {r}"
    )


def test_route_销量_returns_none():
    """刷新销量不走确定性路由（由 LLM 选 workflow）。"""
    r = _deterministic_workflow_request("刷新一下销量数据")
    # 可能返回 None 或其他 workflow，但不应是 wf1_stock_v2
    assert r is None or r.get("workflow") != "wf1_stock_v2", (
        f"'刷新销量' 不应路由到 wf1_stock_v2，实际: {r}"
    )


def test_route_negation_不用刷库存():
    """否定词拦截：不用刷库存 → None。"""
    r = _deterministic_workflow_request("不用刷库存了，先用现有数据")
    assert r is None, f"否定词应拦截，实际: {r}"


# ── 交付点 1 补丁：否定词变体拦截（PR#67 验门补修）────────────────────────────

def test_route_negation_不要刷ERP库存():
    """FAIL（改前）：不要刷ERP库存 通过 `不要刷库存` 裸词匹配，但 ERP 在中间故未命中。
    PASS（改后）：正则 (?:不用|不要|无需|先别).{0,5}刷.{0,15}库存 全拦。"""
    r = _deterministic_workflow_request("不要刷ERP库存，先用现在的数据")
    assert r is None, f"不要刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_不用刷6仓库存():
    r = _deterministic_workflow_request("不用刷6仓库存了")
    assert r is None, f"不用刷6仓库存 应被否定词拦截，实际: {r}"


def test_route_negation_无需刷ERP库存():
    r = _deterministic_workflow_request("无需刷ERP库存，跳过这步")
    assert r is None, f"无需刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_先别刷ERP库存():
    r = _deterministic_workflow_request("先别刷ERP库存，等我确认后再刷")
    assert r is None, f"先别刷ERP库存 应被否定词拦截，实际: {r}"


# ── 交付点 1 补丁 round-4：否定词变体拦截扩展（别/暂时别/不需要）──────────────

def test_route_negation_别刷库存():
    """FAIL（改前）：'别' 不在否定词集，别刷库存 仍路由 wf1_stock_v2。
    PASS（改后）：正则含 '别'，拦截通过。"""
    r = _deterministic_workflow_request("别刷库存，先用现有数据")
    assert r is None, f"别刷库存 应被否定词拦截，实际: {r}"


def test_route_negation_别刷ERP库存():
    """FAIL（改前）：'别' 缺失，别刷ERP库存 触发 wf1_stock_v2。
    PASS（改后）：正则含 '别'，拦截通过。"""
    r = _deterministic_workflow_request("别刷ERP库存")
    assert r is None, f"别刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_暂时别刷ERP库存():
    """FAIL（改前）：'暂时别' 缺失，暂时别刷ERP库存 触发 wf1_stock_v2。
    PASS（改后）：正则含 '暂时别'，拦截通过。"""
    r = _deterministic_workflow_request("暂时别刷ERP库存，等通知")
    assert r is None, f"暂时别刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_不需要刷库存():
    """FAIL（改前）：'不需要' 缺失，不需要刷库存 触发 wf1_stock_v2。
    PASS（改后）：正则含 '不需要'/'不需'，拦截通过。"""
    r = _deterministic_workflow_request("不需要刷库存")
    assert r is None, f"不需要刷库存 应被否定词拦截，实际: {r}"


# ── 交付点 1 补丁 round-5：否定词变体扩展（不刷/暂时不刷/不必刷）──────────────

def test_route_negation_不刷库存():
    """FAIL（改前）：'不刷' 不在否定词集，不刷库存 仍路由 wf1_stock_v2。
    PASS（改后）：新增 `不刷.{0,15}库存` 分支拦截。"""
    r = _deterministic_workflow_request("不刷库存")
    assert r is None, f"不刷库存 应被否定词拦截，实际: {r}"


def test_route_negation_暂时不刷ERP库存():
    """FAIL（改前）：'暂时不刷' 缺失，暂时不刷ERP库存 触发 wf1_stock_v2。
    PASS（改后）：'不刷' OR 分支覆盖 '暂时不刷ERP库存'。"""
    r = _deterministic_workflow_request("暂时不刷ERP库存")
    assert r is None, f"暂时不刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_不必刷ERP库存():
    """FAIL（改前）：'不必' 缺失，不必刷ERP库存 触发 wf1_stock_v2。
    PASS（改后）：'不必' 加入否定词集，不必.{0}刷ERP库存 拦截通过。"""
    r = _deterministic_workflow_request("不必刷ERP库存")
    assert r is None, f"不必刷ERP库存 应被否定词拦截，实际: {r}"


# ── 交付点 1 补丁 round-6：否定词变体扩展（不想/不打算）──────────────────────

def test_route_negation_不想刷库存():
    """FAIL（改前）：'不想' 不在否定词集，不想刷库存 仍路由 wf1_stock_v2。
    PASS（改后）：正则含 '不想'，拦截通过。"""
    r = _deterministic_workflow_request("不想刷库存")
    assert r is None, f"不想刷库存 应被否定词拦截，实际: {r}"


def test_route_negation_不想刷ERP库存():
    """FAIL（改前）：'不想' 缺失，不想刷ERP库存 触发 wf1_stock_v2。
    PASS（改后）：正则含 '不想'，拦截通过。"""
    r = _deterministic_workflow_request("不想刷ERP库存")
    assert r is None, f"不想刷ERP库存 应被否定词拦截，实际: {r}"


def test_route_negation_不打算刷库存():
    """FAIL（改前）：'不打算' 缺失，不打算刷库存 触发 wf1_stock_v2。
    PASS（改后）：正则含 '不打算'，拦截通过。"""
    r = _deterministic_workflow_request("不打算刷库存")
    assert r is None, f"不打算刷库存 应被否定词拦截，实际: {r}"


# ── 交付点 1 补丁 round-11：否定词覆盖库存动作族（同步/重算/请勿）────────────

def test_route_negation_不要同步库存():
    """FAIL（改前）：否定词只覆盖'刷'，不要同步库存 仍路由 wf1_stock_v2。"""
    r = _deterministic_workflow_request("不要同步库存")
    assert r is None, f"不要同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_别同步库存():
    r = _deterministic_workflow_request("别同步库存")
    assert r is None, f"别同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_不打算同步库存():
    r = _deterministic_workflow_request("不打算同步库存")
    assert r is None, f"不打算同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_不需要重算库存():
    r = _deterministic_workflow_request("不需要重算库存")
    assert r is None, f"不需要重算库存 应被否定词拦截，实际: {r}"


def test_route_negation_请勿刷库存():
    r = _deterministic_workflow_request("请勿刷库存")
    assert r is None, f"请勿刷库存 应被否定词拦截，实际: {r}"


def test_route_negation_请勿同步库存():
    r = _deterministic_workflow_request("请勿同步库存")
    assert r is None, f"请勿同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_不同步库存():
    r = _deterministic_workflow_request("不同步库存")
    assert r is None, f"不同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_不重算库存():
    r = _deterministic_workflow_request("不重算库存")
    assert r is None, f"不重算库存 应被否定词拦截，实际: {r}"


def test_route_negation_停止同步库存():
    r = _deterministic_workflow_request("停止同步库存")
    assert r is None, f"停止同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_取消同步库存():
    r = _deterministic_workflow_request("取消同步库存")
    assert r is None, f"取消同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_停止重算库存():
    r = _deterministic_workflow_request("停止重算库存")
    assert r is None, f"停止重算库存 应被否定词拦截，实际: {r}"


def test_route_negation_取消库存同步():
    r = _deterministic_workflow_request("取消库存同步")
    assert r is None, f"取消库存同步 应被否定词拦截，实际: {r}"


def test_route_negation_停止库存同步():
    r = _deterministic_workflow_request("停止库存同步")
    assert r is None, f"停止库存同步 应被否定词拦截，实际: {r}"


def test_route_negation_不要库存同步():
    r = _deterministic_workflow_request("不要库存同步")
    assert r is None, f"不要库存同步 应被否定词拦截，实际: {r}"


def test_route_negation_取消库存更新():
    r = _deterministic_workflow_request("取消库存更新")
    assert r is None, f"取消库存更新 应被否定词拦截，实际: {r}"


def test_route_negation_库存不同步():
    r = _deterministic_workflow_request("库存不同步")
    assert r is None, f"库存不同步 应被否定词拦截，实际: {r}"


def test_route_negation_暂停同步库存():
    r = _deterministic_workflow_request("暂停同步库存")
    assert r is None, f"暂停同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_暂停库存同步():
    r = _deterministic_workflow_request("暂停库存同步")
    assert r is None, f"暂停库存同步 应被否定词拦截，实际: {r}"


def test_route_negation_先暂停同步库存():
    r = _deterministic_workflow_request("先暂停同步库存")
    assert r is None, f"先暂停同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_暂停重算库存():
    r = _deterministic_workflow_request("暂停重算库存")
    assert r is None, f"暂停重算库存 应被否定词拦截，实际: {r}"


def test_route_negation_中止同步库存():
    r = _deterministic_workflow_request("中止同步库存")
    assert r is None, f"中止同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_终止库存同步():
    r = _deterministic_workflow_request("终止库存同步")
    assert r is None, f"终止库存同步 应被否定词拦截，实际: {r}"


def test_route_negation_禁止同步库存():
    r = _deterministic_workflow_request("禁止同步库存")
    assert r is None, f"禁止同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_严禁同步库存():
    r = _deterministic_workflow_request("严禁同步库存")
    assert r is None, f"严禁同步库存 应被否定词拦截，实际: {r}"


def test_route_negation_暂缓同步库存():
    r = _deterministic_workflow_request("暂缓同步库存")
    assert r is None, f"暂缓同步库存 应被否定词拦截，实际: {r}"


def test_route_positive_sync_inventory_still_wf1():
    r = _deterministic_workflow_request("同步库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"同步库存 应路由 wf1_stock_v2，实际: {r}"
    )


def test_route_positive_inventory_sync_still_wf1():
    r = _deterministic_workflow_request("库存同步")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"库存同步 应路由 wf1_stock_v2，实际: {r}"
    )


def test_route_positive_refresh_inventory_still_wf1():
    r = _deterministic_workflow_request("刷新库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"刷新库存 应路由 wf1_stock_v2，实际: {r}"
    )


def test_route_positive_recalc_inventory_still_wf1():
    r = _deterministic_workflow_request("重算库存")
    assert r is not None and r.get("workflow") == "wf1_stock_v2", (
        f"重算库存 应路由 wf1_stock_v2，实际: {r}"
    )


# ── 交付点 2：Safety 守门升级 ────────────────────────────────────────────────

def test_safety_removes_fake_task_id():
    """promise_workflow 触发且无 run_workflow：假任务号必须从回复正文中删除。
    （诊断 banner 中列出 task_id 供调试是允许的；检查正文 body）"""
    reply = "库存刷新已启动，任务号 a5333a45 正在后台运行，请稍等。"
    sanitized, warns = sanitize_reply(reply, [])
    assert warns, "应报告 promise_workflow 幻觉警告"
    # 取正文（banner 之后）——banner 可以提及 task_id 供调试，正文不能
    body = sanitized.split("---\n\n", 1)[-1] if "---\n\n" in sanitized else sanitized
    assert "a5333a45" not in body, (
        f"假任务号 a5333a45 应从回复正文中删除，但仍在正文: {body!r}"
    )


def test_safety_removes_qian_duan_progress():
    """'前端会推送进度' 属假成功声明（无真实 run_workflow），必须从 reply 中删除。"""
    reply = "好的，已触发库存刷新工作流，前端会推送进度更新。"
    sanitized, warns = sanitize_reply(reply, [])
    assert warns, "应报告幻觉警告"
    assert "前端会推送进度" not in sanitized, (
        f"'前端会推送进度' 应从 reply 中删除，但仍在: {sanitized!r}"
    )


def test_safety_removes_yi_qi_dong_segment():
    """'已启动 xxx 工作流' 假成功段必须从 reply 中删除。"""
    reply = "好的，已启动库存刷新任务，a5333a45 已提交，完成后通知你。"
    sanitized, warns = sanitize_reply(reply, [])
    assert warns, "应报告幻觉警告"
    assert "a5333a45" not in sanitized, (
        f"假任务号应被删除: {sanitized!r}"
    )


def test_safety_shows_no_task_created_message():
    """删除假成功段后，reply 中应有'本轮未创建刷新任务'提示。"""
    reply = "库存刷新已启动，任务 a5333a45 已经在后台跑了，前端会推送进度。"
    sanitized, warns = sanitize_reply(reply, [])
    assert warns, "应报告幻觉警告"
    assert "本轮未创建刷新任务" in sanitized or "未启动后台流程" in sanitized, (
        f"reply 应含'本轮未创建刷新任务/未启动后台流程'，实际: {sanitized!r}"
    )


def test_safety_real_run_workflow_not_flagged():
    """真调了 run_workflow → 不报 promise_workflow 幻觉，不删除任何内容。"""
    reply = "好的，已触发库存刷新工作流，后台任务已提交。"
    sanitized, warns = sanitize_reply(reply, ["run_workflow"])
    # 真调了 run_workflow，promise_workflow 分支不应触发
    promise_warns = [w for w in warns if "没真调 run_workflow" in w]
    assert not promise_warns, f"真调 run_workflow 不应被 promise_workflow 守门拦截: {warns}"


def test_safety_backstop_t36_t38_regression():
    """回归 T36/T38：tools_used 为空时，任意假任务号仍被拦截（守门不挖空）。"""
    reply = "销量刷新已启动，任务 deadbeef 已经在后台跑了。"
    _, warns = sanitize_reply(reply, [])
    assert any("run_workflow" in w or "deadbeef" in w or "fake" in w.lower() or "任务" in w
               for w in warns), (
        f"T36/T38 回归：假任务号/假启动应被拦截，实际 warns: {warns}"
    )


def test_safety_removes_markdown_quality_judgment_for_numeric_question():
    reply = (
        "以下是 TBB0116A 的近期数据：\n"
        "- 总销量：97 件\n"
        "- 取消率：3.85%\n"
        "- 利润率：**42%** ✅ 不错"
    )
    sanitized, _warns = sanitize_reply(
        reply,
        ["query_sku"],
        question="TBB0116A 近 30 天销量多少，退货率和取消率分别是多少",
    )
    assert "不错" not in sanitized, f"只问数值时应删除质量评价行，实际: {sanitized!r}"


# ── 交付点 3 & 4：全量 SKU preflight + smoke 验收 ────────────────────────────

_TMP_DB: str = ""


def _redirect_test_db(erp_stock, db_path: str) -> None:
    """server.data 和 hipop.server.data 是两个不同的模块对象，都要设 DB_PATH。
    erp_stock._data == server.data；sales_entity_v2._data == hipop.server.data。"""
    erp_stock._data.DB_PATH = db_path
    import sales_entity_v2
    sales_entity_v2._data.DB_PATH = db_path


def _setup_preflight_db(skus_ksa: list, existing_stock: dict | None = None) -> str:
    """创建临时 SQLite，注入 sales_entities + wf2_sku + 可选 wf1_stock 旧数据。

    skus_ksa: wf2_sku 里已知 KSA SKU 列表。
    existing_stock: {partner_sku: {yiwu_qty, dongguan_qty, ...}} 的旧库存（可选）。
    返回临时 DB 路径。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    sql_text = open(SCHEMA_V2, encoding="utf-8").read()

    def _extract(table: str) -> str:
        m = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);",
            sql_text, re.DOTALL,
        )
        assert m, f"找不到 {table} CREATE TABLE"
        return m.group(0)

    c = sqlite3.connect(db_path)
    for t in ("sales_entities", "wf2_sku", "wf1_stock", "tenant_erp_credentials"):
        c.executescript(_extract(t))

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
    if existing_stock:
        for sku, vals in existing_stock.items():
            c.execute(
                "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku,"
                " yiwu_qty, dongguan_qty, overseas_total_qty, total_stock)"
                " VALUES (?,?,?,?,?,?,?)",
                (1, "hipop_ksa", sku,
                 vals.get("yiwu_qty", 0), vals.get("dongguan_qty", 0),
                 vals.get("overseas_total_qty", 0), vals.get("total_stock", 0)),
            )
    c.commit()
    c.close()
    return db_path


def _q(db_path: str, sql: str, params=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def _setup_multi_entity_db() -> str:
    """KSA 有 SKU，UAE entity 存在但 wf2_sku 为空。用于 per-entity preflight 测试。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    sql_text = open(SCHEMA_V2, encoding="utf-8").read()

    def _extract(table: str) -> str:
        m = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);",
            sql_text, re.DOTALL,
        )
        assert m, f"找不到 {table} CREATE TABLE"
        return m.group(0)

    c = sqlite3.connect(db_path)
    for t in ("sales_entities", "wf2_sku", "wf1_stock", "tenant_erp_credentials"):
        c.executescript(_extract(t))
    c.execute(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active)"
        " VALUES (?,?,?,?,?,?,1)",
        (1, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
    )
    for sku in ["SKU-A", "SKU-B"]:
        c.execute(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku) VALUES (?,?,?)",
            (1, "hipop_ksa", sku),
        )
    c.execute(
        "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active)"
        " VALUES (?,?,?,?,?,?,1)",
        (1, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 86),
    )
    # hipop_uae 故意不插 wf2_sku — 测 per-entity 报错
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


def test_preflight_empty_sku_master_raises():
    """wf2_sku 为空 → preflight 必须 raise RuntimeError，不假装完成。

    FAIL（改前）：run_v2 无 preflight，直接空跑写 0 行。
    PASS（改后）：raise RuntimeError。
    """
    import ingest_erp_stock_v2 as erp_stock

    db_path = _setup_preflight_db(skus_ksa=[])  # SKU master 为空
    os.environ.pop("DB_URL", None)
    # 直接设 server.data.DB_PATH（erp_stock 内部用的是 server.data，不是 hipop.server.data）
    _redirect_test_db(erp_stock, db_path)

    raised = False
    try:
        erp_stock.run_v2(1, token="FAKE", fetch_fn=lambda *a, **k: [])
    except RuntimeError as e:
        raised = True
        assert "SKU master 为空" in str(e) or "preflight" in str(e).lower(), (
            f"RuntimeError 消息应包含 'SKU master 为空' 或 'preflight': {e}"
        )
    finally:
        os.unlink(db_path)

    assert raised, "SKU master 为空时应 raise RuntimeError，但没有"


def test_preflight_missing_sku_written_as_zero():
    """wf2_sku 有 A/B/C，ERP 只返回 A/B → C 必须以 qty=0 落库，不保留旧值。

    FAIL（改前）：run_v2 只写 ERP 返回的 SKU，C 的旧库存留在 wf1_stock（或 C 根本不被更新）。
    PASS（改后）：C 以 yiwu_qty=0, dongguan_qty=0, overseas_total_qty=0 落库。
    """
    import ingest_erp_stock_v2 as erp_stock

    # 预置 C 的旧库存 qty=999（旧值不应被保留）
    db_path = _setup_preflight_db(
        skus_ksa=["SKU-A", "SKU-B", "SKU-C"],
        existing_stock={"SKU-C": {"yiwu_qty": 999, "total_stock": 999}},
    )
    os.environ.pop("DB_URL", None)
    _redirect_test_db(erp_stock, db_path)

    # ERP 只返回 A/B（义乌仓 wid=6），C 不返回
    ERP_FIX = {
        6: [_erp_item("SKU-A", 100), _erp_item("SKU-B", 50)],
    }

    def fake_fetch(token, wid, **kw):
        return ERP_FIX.get(wid, [])

    try:
        result = erp_stock.run_v2(1, token="FAKE", fetch_fn=fake_fetch)
    finally:
        pass  # 清理在 finally 块外

    rows = {r["partner_sku"]: r for r in _q(db_path, "SELECT * FROM wf1_stock")}
    os.unlink(db_path)

    assert "SKU-C" in rows, "SKU-C 应被写入（以 qty=0）"
    c_row = rows["SKU-C"]
    assert c_row["yiwu_qty"] == 0 and c_row["dongguan_qty"] == 0 \
           and c_row["overseas_total_qty"] == 0, (
        f"SKU-C 未被 ERP 返回，应以 qty=0 落库，不保留旧值 999，实际: {c_row}"
    )
    assert c_row.get("total_stock", 0) == 0, (
        f"SKU-C total_stock 应=0，实际: {c_row.get('total_stock')}"
    )


def test_preflight_erp_already_returns_zero_written_correctly():
    """ERP 本身返回含 0 的全量（SKU-C qty=0）→ 正确写 0（smoke 钉死，不靠注释）。

    此 case 改前改后都应 PASS（ERP 返 0 → bucket 里有 qty=0 → 写库 0）。
    用 smoke 钉死：不允许 0 qty 被静默跳过。
    """
    import ingest_erp_stock_v2 as erp_stock

    db_path = _setup_preflight_db(skus_ksa=["SKU-A", "SKU-B", "SKU-C"])
    os.environ.pop("DB_URL", None)
    _redirect_test_db(erp_stock, db_path)

    # ERP 返回含 0 的全量
    ERP_FIX = {
        6: [_erp_item("SKU-A", 100), _erp_item("SKU-B", 50), _erp_item("SKU-C", 0)],
    }

    def fake_fetch(token, wid, **kw):
        return ERP_FIX.get(wid, [])

    try:
        erp_stock.run_v2(1, token="FAKE", fetch_fn=fake_fetch)
    finally:
        pass

    rows = {r["partner_sku"]: r for r in _q(db_path, "SELECT * FROM wf1_stock")}
    os.unlink(db_path)

    assert "SKU-C" in rows, "ERP 返回 qty=0 的 SKU-C 应被写入"
    assert rows["SKU-C"]["yiwu_qty"] == 0, (
        f"ERP 返回 qty=0 的 SKU-C yiwu_qty 应=0，实际: {rows['SKU-C']}"
    )


def test_preflight_erp_new_sku_d_by_store_binding():
    """ERP 返回 wf2_sku 里没有的新增 SKU-D（有 store binding）→ 按 binding 决定写入。

    store binding 逻辑在 has_store_binding()，不由 preflight 拦截。
    """
    import ingest_erp_stock_v2 as erp_stock

    db_path = _setup_preflight_db(skus_ksa=["SKU-A", "SKU-B"])
    os.environ.pop("DB_URL", None)
    _redirect_test_db(erp_stock, db_path)

    ERP_FIX = {
        6: [_erp_item("SKU-A", 100), _erp_item("SKU-D", 30)],  # SKU-D 新增
    }

    def fake_fetch(token, wid, **kw):
        return ERP_FIX.get(wid, [])

    erp_stock.run_v2(1, token="FAKE", fetch_fn=fake_fetch)

    rows = {r["partner_sku"]: r for r in _q(db_path, "SELECT * FROM wf1_stock")}
    os.unlink(db_path)

    # SKU-D has store binding (HIPOP-NOON-KSA) → should be written
    assert "SKU-D" in rows, "ERP 返回有 binding 的新增 SKU-D 应被写入"
    assert rows["SKU-D"]["yiwu_qty"] == 30, (
        f"SKU-D 应写 yiwu_qty=30，实际: {rows['SKU-D']}"
    )
    # SKU-B was in wf2_sku but not returned by ERP → should be zeroed
    assert "SKU-B" in rows, "已知 SKU-B（ERP 未返回）应以 qty=0 落库"
    assert rows["SKU-B"]["yiwu_qty"] == 0, (
        f"未被 ERP 返回的 SKU-B 应 qty=0，实际: {rows['SKU-B']}"
    )


# ── 交付点 3 补丁：per-entity preflight（PR#67 验门补修）────────────────────────

def test_preflight_per_entity_empty_raises():
    """KSA 有 SKU + UAE entity 存在但 wf2_sku 为空 → RuntimeError 含 entity 名。

    FAIL（改前）：all(len(v)==0...) 在 KSA 有 SKU 时不触发，UAE 空跑 0 行冒充完成。
    PASS（改后）：逐 entity 判空 → raise RuntimeError，消息含 'hipop_uae'。
    """
    import ingest_erp_stock_v2 as erp_stock

    db_path = _setup_multi_entity_db()
    os.environ.pop("DB_URL", None)
    _redirect_test_db(erp_stock, db_path)

    raised = False
    try:
        erp_stock.run_v2(1, token="FAKE", fetch_fn=lambda *a, **k: [])
    except RuntimeError as e:
        raised = True
        assert "hipop_uae" in str(e), (
            f"RuntimeError 消息应含 'hipop_uae'，实际: {e}"
        )
    finally:
        os.unlink(db_path)

    assert raised, "KSA 有 SKU + UAE 空 → 应 raise RuntimeError，但没有"


# ── 交付点 1/2 回归：T36/T38/T21 false-positive 防护 ────────────────────────

def test_t36_regression_safety_without_run_workflow():
    """T36 回归：'已启动/再次触发'但 tools_used 无 run_workflow → 仍被拦截。"""
    reply = "销量已开始重算，后台跑了。"
    _, warns = sanitize_reply(reply, [])
    assert any("run_workflow" in w for w in warns), (
        f"T36 回归失败：假启动宣称应被拦，warns: {warns}"
    )


def test_t38_regression_scan_no_task_no_false_positive():
    """T38 回归：用户说'扫 ERP'，Agent 正确触发了 run_workflow → 不报假启动警告。"""
    # 真调了 run_workflow，不应误报
    reply = "好的，已触发物流扫描，任务已提交。"
    _, warns = sanitize_reply(reply, ["run_workflow"])
    promise_warns = [w for w in warns if "没真调 run_workflow" in w]
    assert not promise_warns, (
        f"T38 回归：真调 run_workflow 不应被 promise_workflow 误报，warns: {warns}"
    )


def test_t21_regression_no_fake_task_id_without_run_workflow():
    """T21 回归：reply 中出现假任务号（8位hex）但 run_workflow 未调 → 被拦截且从回复正文删除。"""
    reply = "重算已启动，任务 deadbeef 在后台跑。"
    sanitized, warns = sanitize_reply(reply, [])
    assert warns, "T21 回归：假任务号应触发警告"
    # 取正文（banner 之后）——banner 可以提及 task_id 供调试，正文不能
    body = sanitized.split("---\n\n", 1)[-1] if "---\n\n" in sanitized else sanitized
    assert "deadbeef" not in body, (
        f"T21 回归：假任务号 deadbeef 应从回复正文中删除，实际正文: {body!r}"
    )


# ── T04 取消率确定性后注入（WS-116 round-10）────────────────────────────────
# fail-then-pass：
#   FAIL（改前）：_maybe_inject_missing_rates 不存在 → 工具已含 cancel_rate_30d 但 LLM
#     遗漏时无补救，T04 smoke 对 r"3\.[0-9]+%|3\.[0-9]" 的断言失败。
#   PASS（改后）：函数存在且注入 → 取消率数值必在回复中，断言通过。

_MOCK_SKU_ITEMS = [{
    "sku": "TBB0116A",
    "found": True,
    "data_stale": False,
    "stale_days": 2,
    "cancel_rate_30d_pct": "3.85%",
    "return_rate_30d_pct": "0.00%",
}]
_T04_QUESTION = "TBB0116A 近 30 天销量多少，退货率和取消率分别是多少"


def test_inject_cancel_rate_when_missing():
    """LLM 遗漏取消率时，_maybe_inject_missing_rates 补注数值。"""
    _last_sku_rate_stats.set(_MOCK_SKU_ITEMS)
    reply = "TBB0116A 近30天销量 97 件，总订单 104 单，退货率 0%。"
    result = _maybe_inject_missing_rates(reply, _T04_QUESTION)
    assert "3.85" in result, f"取消率 3.85% 应被注入，实际: {result!r}"
    assert "取消率" in result, f"'取消率' 关键词应在注入后的回复中，实际: {result!r}"


def test_inject_cancel_rate_not_duplicated_when_already_present():
    """取消率值已在回复中时不重复注入 cancel rate。"""
    _last_sku_rate_stats.set(_MOCK_SKU_ITEMS)
    reply = "TBB0116A 近30天销量 97 件，总订单 104 单，取消率 3.85%，退货率 0.00%。"
    result = _maybe_inject_missing_rates(reply, _T04_QUESTION)
    # Both "3.85" (cancel rate) and "0.00" (return rate) already in text → no injection
    assert result == reply, f"已含取消率和退货率时不应注入，实际: {result!r}"


def test_inject_cancel_rate_when_keyword_present_but_value_missing():
    """'取消率' 关键词存在但值缺失时仍注入数值（旧条件 'and 取消率 not in text' 的盲区）。"""
    _last_sku_rate_stats.set(_MOCK_SKU_ITEMS)
    reply = "TBB0116A 近30天销量 97 件，退货率 0%，取消率数据暂时无法获取。"
    result = _maybe_inject_missing_rates(reply, _T04_QUESTION)
    assert "3.85" in result, f"关键词存在但值缺失，应注入 3.85%，实际: {result!r}"


def test_inject_not_triggered_when_question_not_about_rate():
    """问题不含取消率/退货率时不触发注入。"""
    _last_sku_rate_stats.set(_MOCK_SKU_ITEMS)
    reply = "TBB0116A 近30天销量 97 件。"
    result = _maybe_inject_missing_rates(reply, "TBB0116A 近 30 天销量多少")
    assert result == reply, f"不问取消率时不应注入，实际: {result!r}"


# ── 影响面：wf1_stock_v2 affected_modules 绑定真实模块名 ─────────────────────

def test_wf1_stock_v2_affected_modules_real():
    """影响面 smoke：wf1_stock_v2 在 WORKFLOW_REGISTRY 中，affected_modules 不含虚假名。"""
    from hipop.server.api import WORKFLOW_REGISTRY
    assert "wf1_stock_v2" in WORKFLOW_REGISTRY, "wf1_stock_v2 不在 WORKFLOW_REGISTRY → /run-workflow 会 400"
    label, steps, affected = WORKFLOW_REGISTRY["wf1_stock_v2"]
    valid_modules = {"sales", "replenish", "logistics", "overview", "feishu"}
    fake = set(affected) - valid_modules
    assert not fake, (
        f"wf1_stock_v2.affected_modules 含不在已知模块集的条目: {fake}（可能是编造的表名）"
    )


def test_t37_chat_reply_includes_business_impact_when_asked():
    """T37 exact prompt 问影响面时，直接路由回复应给运营可懂影响面而不是编技术表名。"""
    old_exec_tool = agent_module._exec_tool
    old_receipt_reply = agent_module._workflow_receipt_reply

    def fake_exec_tool(name: str, args: dict, user: dict = None) -> dict:
        assert name == "run_workflow", f"T37 应真实调 run_workflow，实际: {name}"
        assert args.get("workflow") == "wf1_stock_v2", f"应跑 wf1_stock_v2，实际 args: {args}"
        return {
            "ok": True,
            "task_id": "task37ok",
            "workflow": "wf1_stock_v2",
            "label": "库存刷新",
            "total_steps": 6,
            "affected_modules": ["sales", "replenish", "logistics"],
            "followup_prompt": args.get("followup_prompt"),
            "references": [],
        }

    def fake_receipt_reply(task_id: str, workflow: str, label: str) -> str:
        return (
            f"已受理{label}（{workflow}），后台任务已创建。\n"
            f"任务 ID：{task_id}｜当前状态：已排队/待执行\n"
            "完成后我会继续回答你的原问题。"
        )

    try:
        agent_module._exec_tool = fake_exec_tool
        agent_module._workflow_receipt_reply = fake_receipt_reply
        result = agent_module.chat(
            [{"role": "user", "content": "请帮我刷库存（ERP 6仓），并说明会更新哪些表。"}],
            {"tenant_id": 1, "current_user": "ksa_ops", "current_role": "owner"},
        )
    finally:
        agent_module._exec_tool = old_exec_tool
        agent_module._workflow_receipt_reply = old_receipt_reply

    assert result.get("workflow_task", {}).get("workflow") == "wf1_stock_v2", (
        f"T37 exact prompt 应创建真实 workflow_task，实际: {result}"
    )
    reply = result.get("reply") or ""
    assert "库存快照" in reply, f"影响面应含库存快照，实际: {reply!r}"
    assert "补货建议" in reply, f"影响面应含补货建议，实际: {reply!r}"
    assert "售罄天数" in reply and "补货判断" in reply, (
        f"影响面应含售罄天数/补货判断，实际: {reply!r}"
    )
    impact = reply.split("影响面：", 1)[-1]
    assert "wf2_sku" not in impact and "wf1_stock" not in impact, (
        f"影响面应使用业务语言，不展开技术表名，实际: {impact!r}"
    )


def test_data_health_reply_appends_oldest_real_date_when_omitted():
    old_get_data_health = agent_module._data.get_data_health

    def fake_get_data_health(store: str) -> dict:
        return {
            "sources": {
                "erp_products": {"latest": "2026-06-09"},
                "noon_stock": {"latest": "2026-06-05"},
                "wf6_alerts": {"latest": "2026-05-08"},
            }
        }

    try:
        agent_module._data.get_data_health = fake_get_data_health
        result = agent_module._maybe_append_oldest_data_health_date(
            "ERP 商品今天更新，noon 库存 6月5日。",
            "KSA 店铺什么时候更新的数据",
            ["data_health_check"],
            {"store": "KSA"},
        )
    finally:
        agent_module._data.get_data_health = old_get_data_health

    assert "2026-05-08" in result, f"遗漏旧源日期时应补最旧真实日期，实际: {result!r}"
    assert "物流告警" in result, f"补充说明应带业务源名，实际: {result!r}"


def test_order_live_blocker_reply_gets_negative_control_hint():
    reply = (
        "抱歉，KSA 目前还没有配置 ERP 账号，所以无法从 ERP 实时查询货单。"
        "这个货单号看起来不像系统里实际存在的货单号，大概率系统中没有这个单号。"
    )
    result = agent_module._maybe_append_order_lookup_negative_hint(
        reply,
        "请查询货单 DGORDER-NOT-EXIST-0001 当前物流状态，不存在就说不存在",
        ["query_order_live"],
    )
    assert "核实货单号" in result, f"query_order_live 负控应补核实提示，实际: {result!r}"


def test_navigate_reply_appends_real_localhost_url_when_omitted():
    reply = "已为你打开补货页面，路径是补货模块。"
    result = agent_module._maybe_append_navigation_url(
        reply,
        [{"name": "navigate_user_to", "args": json.dumps({"module": "replenish", "store": "KSA"})}],
    )
    assert "http://localhost:8765/module/replenish?store=ksa" in result, (
        f"navigate_user_to 回复遗漏 URL 时应补真实本地入口，实际: {result!r}"
    )


# ── runner ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("路由-刷库存",                        test_route_刷库存),
        ("路由-刷ERP库存",                     test_route_刷ERP库存),
        ("路由-刷6仓库存",                     test_route_刷6仓库存),
        ("路由-T37 exact prompt",              test_route_t37_exact_prompt),
        ("路由-刷新库存回归",                  test_route_刷新库存_still_works),
        ("路由-刷ERP无库存不路由库存",         test_route_刷ERP_without_库存_no_crash),
        ("路由-物流回归 wf3",                  test_route_物流_still_wf3),
        ("路由-扫物流回归 wf3",                test_route_扫物流_still_wf3),
        ("路由-销量不走 wf1",                  test_route_销量_returns_none),
        ("路由-否定词拦截",                    test_route_negation_不用刷库存),
        ("路由-否定词-不要刷ERP库存",          test_route_negation_不要刷ERP库存),
        ("路由-否定词-不用刷6仓库存",          test_route_negation_不用刷6仓库存),
        ("路由-否定词-无需刷ERP库存",          test_route_negation_无需刷ERP库存),
        ("路由-否定词-先别刷ERP库存",          test_route_negation_先别刷ERP库存),
        ("路由-否定词-别刷库存",               test_route_negation_别刷库存),
        ("路由-否定词-别刷ERP库存",            test_route_negation_别刷ERP库存),
        ("路由-否定词-暂时别刷ERP库存",        test_route_negation_暂时别刷ERP库存),
        ("路由-否定词-不需要刷库存",           test_route_negation_不需要刷库存),
        ("路由-否定词-不刷库存",               test_route_negation_不刷库存),
        ("路由-否定词-暂时不刷ERP库存",        test_route_negation_暂时不刷ERP库存),
        ("路由-否定词-不必刷ERP库存",          test_route_negation_不必刷ERP库存),
        ("路由-否定词-不想刷库存",             test_route_negation_不想刷库存),
        ("路由-否定词-不想刷ERP库存",          test_route_negation_不想刷ERP库存),
        ("路由-否定词-不打算刷库存",           test_route_negation_不打算刷库存),
        ("路由-否定词-不要同步库存",           test_route_negation_不要同步库存),
        ("路由-否定词-别同步库存",             test_route_negation_别同步库存),
        ("路由-否定词-不打算同步库存",         test_route_negation_不打算同步库存),
        ("路由-否定词-不需要重算库存",         test_route_negation_不需要重算库存),
        ("路由-否定词-请勿刷库存",             test_route_negation_请勿刷库存),
        ("路由-否定词-请勿同步库存",           test_route_negation_请勿同步库存),
        ("路由-否定词-不同步库存",             test_route_negation_不同步库存),
        ("路由-否定词-不重算库存",             test_route_negation_不重算库存),
        ("路由-否定词-停止同步库存",           test_route_negation_停止同步库存),
        ("路由-否定词-取消同步库存",           test_route_negation_取消同步库存),
        ("路由-否定词-停止重算库存",           test_route_negation_停止重算库存),
        ("路由-否定词-取消库存同步",           test_route_negation_取消库存同步),
        ("路由-否定词-停止库存同步",           test_route_negation_停止库存同步),
        ("路由-否定词-不要库存同步",           test_route_negation_不要库存同步),
        ("路由-否定词-取消库存更新",           test_route_negation_取消库存更新),
        ("路由-否定词-库存不同步",             test_route_negation_库存不同步),
        ("路由-否定词-暂停同步库存",           test_route_negation_暂停同步库存),
        ("路由-否定词-暂停库存同步",           test_route_negation_暂停库存同步),
        ("路由-否定词-先暂停同步库存",         test_route_negation_先暂停同步库存),
        ("路由-否定词-暂停重算库存",           test_route_negation_暂停重算库存),
        ("路由-否定词-中止同步库存",           test_route_negation_中止同步库存),
        ("路由-否定词-终止库存同步",           test_route_negation_终止库存同步),
        ("路由-否定词-禁止同步库存",           test_route_negation_禁止同步库存),
        ("路由-否定词-严禁同步库存",           test_route_negation_严禁同步库存),
        ("路由-否定词-暂缓同步库存",           test_route_negation_暂缓同步库存),
        ("路由-正向-同步库存",                 test_route_positive_sync_inventory_still_wf1),
        ("路由-正向-库存同步",                 test_route_positive_inventory_sync_still_wf1),
        ("路由-正向-刷新库存",                 test_route_positive_refresh_inventory_still_wf1),
        ("路由-正向-重算库存",                 test_route_positive_recalc_inventory_still_wf1),
        ("Safety-删假任务号",                  test_safety_removes_fake_task_id),
        ("Safety-删前端推送进度",              test_safety_removes_qian_duan_progress),
        ("Safety-删已启动段",                  test_safety_removes_yi_qi_dong_segment),
        ("Safety-本轮未创建提示",              test_safety_shows_no_task_created_message),
        ("Safety-真 run_workflow 不误报",      test_safety_real_run_workflow_not_flagged),
        ("Safety-T36/T38 回归",                test_safety_backstop_t36_t38_regression),
        ("Safety-数值问题删除质量评价",         test_safety_removes_markdown_quality_judgment_for_numeric_question),
        ("Preflight-空SKU master 报错",        test_preflight_empty_sku_master_raises),
        ("Preflight-未返回SKU zeroed",         test_preflight_missing_sku_written_as_zero),
        ("Preflight-ERP已返0正确写",           test_preflight_erp_already_returns_zero_written_correctly),
        ("Preflight-新增SKU按binding决定",     test_preflight_erp_new_sku_d_by_store_binding),
        ("Preflight-per-entity空entity报错",   test_preflight_per_entity_empty_raises),
        ("回归-T36无run_workflow被拦",         test_t36_regression_safety_without_run_workflow),
        ("回归-T38真调不误报",                 test_t38_regression_scan_no_task_no_false_positive),
        ("回归-T21假任务号被删",               test_t21_regression_no_fake_task_id_without_run_workflow),
        ("影响面-affected_modules真实",        test_wf1_stock_v2_affected_modules_real),
        ("影响面-T37直路由业务影响面",          test_t37_chat_reply_includes_business_impact_when_asked),
        ("数据健康-遗漏最旧日期时补充",          test_data_health_reply_appends_oldest_real_date_when_omitted),
        ("T26-货单负控补核实提示",              test_order_live_blocker_reply_gets_negative_control_hint),
        ("Navigate-遗漏URL时补真实入口",         test_navigate_reply_appends_real_localhost_url_when_omitted),
        ("T04注入-取消率缺失时补注",            test_inject_cancel_rate_when_missing),
        ("T04注入-已含不重复注入",              test_inject_cancel_rate_not_duplicated_when_already_present),
        ("T04注入-关键词在但值缺失仍注入",      test_inject_cancel_rate_when_keyword_present_but_value_missing),
        ("T04注入-不问取消率不触发",            test_inject_not_triggered_when_question_not_about_rate),
    ]

    passed, failed = 0, 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed + failed} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        sys.exit(1)
    else:
        print(" ✓")
        sys.exit(0)
