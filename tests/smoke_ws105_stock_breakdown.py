"""
smoke_ws105_stock_breakdown.py — T11/T12 单 SKU 库存拆分 smoke（WS-104/WS-105）

T11: TBP0169A KSA → total=10702, noon_saleable=22, overseas=10680
T12: TBB0116A KSA → total=148, noon_saleable=81, overseas=66
边界: 缺行 → found=False + 标准口径; NULL 字段 → 无数据/未刷新; OpenAI str args verifier
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hipop.server.agent import tool_query_stock_breakdown, _fix_stock_breakdown_reply
import re

passed, failed = 0, []


def ok(name):
    global passed
    passed += 1
    print(f"  ✓ {name}")


def fail(name, msg):
    failed.append((name, msg))
    print(f"  ✗ {name}: {msg}")


# ── T11: TBP0169A KSA ──────────────────────────────────────────────────────────
name = "T11 TBP0169A KSA total=10702/noon=22/overseas=10680"
try:
    r = tool_query_stock_breakdown("TBP0169A", "KSA")
    assert r["found"] is True, f"found={r['found']}"
    assert r["total_stock"] == 10702, f"total_stock={r['total_stock']}"
    assert r["noon_saleable"] == 22, f"noon_saleable={r['noon_saleable']}"
    assert r["overseas"] == 10680, f"overseas={r['overseas']}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── T12: TBB0116A KSA ──────────────────────────────────────────────────────────
name = "T12 TBB0116A KSA total=148/noon=81/overseas=66"
try:
    r = tool_query_stock_breakdown("TBB0116A", "KSA")
    assert r["found"] is True, f"found={r['found']}"
    assert r["total_stock"] == 148, f"total_stock={r['total_stock']}"
    assert r["noon_saleable"] == 81, f"noon_saleable={r['noon_saleable']}"
    assert r["overseas"] == 66, f"overseas={r['overseas']}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── 缺行 SKU → found=False, error 含标准口径，不报 0 不报断货 ─────────────────
name = "缺行 SKU found=False 标准口径"
try:
    r = tool_query_stock_breakdown("NONEXISTENT_SMOKE_SKU", "KSA")
    assert r["found"] is False, f"found={r['found']}"
    err = r.get("error", "")
    assert re.search(r"无|未刷新|未接入", err), f"error 未含标准口径: {err}"
    assert "断货" not in str(r), f"不应含断货: {r}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── NULL 字段 → 无数据/未刷新，不默认 0 ────────────────────────────────────────
name = "NULL 字段返回 无数据/未刷新"
try:
    import sqlite3
    from hipop.server import data as _data
    db_path = str(_data.DB_PATH)
    sku = "T11_SMOKE_NULL_SKU"
    con = sqlite3.connect(db_path)
    con.execute("""
        INSERT OR REPLACE INTO wf1_hipop_ksa_stock
        (partner_sku, noon_saleable_qty, noon_total_qty, noon_unsaleable_qty,
         overseas_total_qty, overseas_breakdown_json, yiwu_qty, dongguan_qty, total_stock)
        VALUES (?, 5, 5, 0, NULL, NULL, NULL, NULL, 5)
    """, (sku,))
    con.commit()
    con.close()
    try:
        r = tool_query_stock_breakdown(sku, "KSA")
        assert r["found"] is True, f"found={r['found']}"
        assert r["overseas"] == "无数据/未刷新", f"overseas NULL 应返回无数据: {r['overseas']}"
        assert r["yiwu"] == "无数据/未刷新", f"yiwu NULL 应返回无数据: {r['yiwu']}"
        assert r["dongguan"] == "无数据/未刷新", f"dongguan NULL 应返回无数据: {r['dongguan']}"
        assert "断货" not in str(r), f"不应推断断货: {r}"
        ok(name)
    finally:
        con2 = sqlite3.connect(db_path)
        con2.execute("DELETE FROM wf1_hipop_ksa_stock WHERE partner_sku=?", (sku,))
        con2.commit()
        con2.close()
except Exception as e:
    fail(name, str(e))

# ── verifier: Anthropic dict args → injects standard phrase ────────────────────
name = "verifier dict-args found=False 注入标准口径"
try:
    tl = [{"name": "query_stock_breakdown", "args": {"sku": "MISS"}, "result_found": False}]
    out = _fix_stock_breakdown_reply("查不到记录。", tl)
    assert re.search(r"无数据|未刷新|未接入|无行|没有记录|找不到|无库存记录", out), f"未注入: {out[:200]}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── verifier: OpenAI JSON-string args → should NOT crash, should inject phrase ──
name = "verifier str-args found=False 不崩且注入标准口径 (OpenAI shape)"
try:
    import json
    tl = [{"name": "query_stock_breakdown", "args": json.dumps({"sku": "MISS_OAI"}), "result_found": False}]
    out = _fix_stock_breakdown_reply("查不到记录，要不帮你确认。", tl)
    assert re.search(r"无数据|未刷新|未接入|无行|没有记录|找不到|无库存记录", out), f"未注入: {out[:200]}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── verifier: already has standard phrase → no-op ──────────────────────────────
name = "verifier 已含标准口径 no-op"
try:
    tl = [{"name": "query_stock_breakdown", "args": {"sku": "X"}, "result_found": False}]
    msg = "该SKU未刷新，请联系运营确认。"
    out = _fix_stock_breakdown_reply(msg, tl)
    assert out == msg, f"不应修改: {out}"
    ok(name)
except Exception as e:
    fail(name, str(e))

# ── report ──────────────────────────────────────────────────────────────────────
print()
if failed:
    print(f"FAIL: {len(failed)} / {passed + len(failed)}")
    for n, m in failed:
        print(f"  ✗ {n}: {m}")
    sys.exit(1)
else:
    print(f"PASS: {passed} / {passed} smoke_ws105_stock_breakdown 全绿")
