"""
数据巡检 agent — 10 个 invariant 检查
针对今天踩过的 8 个 P0 总结出来的不变量, 任何漂移立刻告警.

调用:
  from server.audit import run_audit
  result = run_audit("ksa")  # 返回 list of {check, status, severity, message, fix}

集成:
  - /api/audit/{store} endpoint
  - 总览页"系统健康"模块卡
  - 异常进工作日志区
"""
import os, sys, sqlite3, subprocess, datetime, re
from typing import List, Dict, Optional

HIPOP_ROOT = os.path.dirname(os.path.dirname(__file__))
PROJECT_ROOT = os.path.dirname(HIPOP_ROOT)
DB_PATH = os.environ.get("HIPOP_DB", os.path.join(PROJECT_ROOT, "hipop.db"))


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _scalar(sql, params=()):
    with _conn() as c:
        r = c.execute(sql, params).fetchone()
        return r[0] if r else None


def _check(name, ok, msg, severity="warn", fix=""):
    return {
        "check": name,
        "status": "ok" if ok else severity,
        "severity": severity,
        "message": msg,
        "fix": fix,
    }


# ── 1. 明细 vs 聚合一致性 (P0#1 抓住 wf2 主表漏 140 SKU) ──
def check_aggregation_consistency(store: str) -> Dict:
    s = store.lower()
    n_orders = _scalar(f"SELECT COUNT(DISTINCT partner_sku) FROM wf2_hipop_{s}_orders") or 0
    if n_orders == 0:
        return _check(
            "wf2 聚合一致性",
            True,
            f"wf2_hipop_{s}_orders 为空, 跳过对账",
            severity="info",
        )
    # 在 orders 但主表 as_of_date NULL 的 SKU 数
    missing = _scalar(f"""
        SELECT COUNT(DISTINCT s.partner_sku)
        FROM wf2_hipop_{s}_sku s
        JOIN wf2_hipop_{s}_orders o ON s.partner_sku = o.partner_sku
        WHERE s.as_of_date IS NULL
    """) or 0
    return _check(
        "wf2 聚合一致性",
        missing == 0,
        f"orders 有 {n_orders} 个 SKU, 主表 as_of_date NULL 的有 {missing} 个",
        severity="danger",
        fix="跑 wf_sales_static --entities hipop_" + s,
    )


# ── 2. 跨表 SKU 池漂移 (P0#5 sa_main 偏离 wf2 289 个能立刻发现) ──
def check_sku_pool_drift(store: str) -> Dict:
    s = store.lower()
    sku_main = set()
    pool_drift = []
    with _conn() as c:
        try:
            sku_main = {r[0] for r in c.execute(f"SELECT partner_sku FROM wf2_hipop_{s}_sku WHERE partner_sku IS NOT NULL")}
        except sqlite3.OperationalError:
            pass
        for table in [f"wf1_hipop_{s}_stock", f"wf5_hipop_{s}_sales_cycle"]:
            try:
                rows = {r[0] for r in c.execute(f"SELECT partner_sku FROM {table} WHERE partner_sku IS NOT NULL")}
                missing = len(sku_main - rows)
                extra = len(rows - sku_main)
                pct = (missing / max(1, len(sku_main))) * 100
                if pct > 10 or extra > 50:
                    pool_drift.append(f"{table}: 缺 {missing} 多 {extra}")
            except sqlite3.OperationalError:
                pool_drift.append(f"{table}: 不存在")
    if pool_drift:
        return _check("SKU 池跨表漂移", False,
                      f"主表 {len(sku_main)} SKU, 漂移: {'; '.join(pool_drift)}",
                      severity="warn",
                      fix="检查各 ingest 是否覆盖全 entity")
    return _check("SKU 池跨表漂移", True, f"主表 {len(sku_main)}, wf1/wf5 对齐 (漂移 < 10%)")


# ── 3. 数据新鲜度 (P0#1 noon CSV 缺失会被这条抓) ──
def check_data_freshness(store: str) -> Dict:
    s = store.lower()
    today = datetime.date.today()
    issues = []
    checks = [
        (f"wf2_hipop_{s}_sku", "imported_at", 7, "wf2 商品库"),
        (f"wf2_hipop_{s}_sku", "as_of_date", 8, "wf2 销量"),
        ("wf3_logistics_hub", "updated_at", 7, "wf3 物流 hub"),  # wf3 全量跑不每天, 7 天
        (f"wf5_hipop_{s}_sales_cycle", "updated_at", 8, "wf5 决策"),
    ]
    for table, col, max_days, label in checks:
        try:
            latest = _scalar(f"SELECT MAX({col}) FROM {table}")
            if not latest:
                issues.append(f"{label} 无数据")
                continue
            d = datetime.datetime.fromisoformat(latest[:19]).date() if "T" in latest else datetime.date.fromisoformat(latest[:10])
            lag = (today - d).days
            if lag > max_days:
                issues.append(f"{label} 滞后 {lag} 天 (阈值 {max_days})")
        except Exception as e:
            issues.append(f"{label}: {e}")
    if issues:
        return _check("数据新鲜度", False, "; ".join(issues), severity="warn",
                      fix="跑 weekly_run.py 全量")
    return _check("数据新鲜度", True, "所有源表在阈值内")


# ── 4. 数据流断裂 (P0#6 sync 读空表能抓) ──
def check_data_flow_integrity(store: str) -> Dict:
    s = store.lower()
    issues = []
    # 已确认丢货 alerts 必须在 per-entity replenishment_queue 有对应记录
    with _conn() as c:
        try:
            lost_alerts = c.execute("""
                SELECT order_no, sku_list_json, forwarder
                FROM wf6_logistics_alerts WHERE ops_status='已确认丢货'
            """).fetchall()
            for a in lost_alerts:
                fw = a["forwarder"] or ""
                # 路由到对应 entity
                target_country = "SA" if "KSA" in fw else ("AE" if "UAE" in fw else None)
                if not target_country: continue
                target_alias = f"hipop_{target_country.lower()}" if target_country == "SA" else "hipop_uae"
                t = f"wf6_{target_alias}_replenishment_queue"
                cnt = c.execute(f"SELECT COUNT(*) FROM {t} WHERE order_no=?", (a["order_no"],)).fetchone()[0]
                if cnt == 0:
                    issues.append(f"alert {a['order_no']} ({fw}) 已确认丢货, 但 {t} 没记录")
        except sqlite3.OperationalError as e:
            issues.append(str(e))

    # 老表 wf6_replenishment_queue 不应被新写入 (P0#6 保护)
    try:
        cnt = _scalar("SELECT COUNT(*) FROM wf6_replenishment_queue") or 0
        if cnt > 0:
            issues.append(f"老表 wf6_replenishment_queue 有 {cnt} 行 (应为 0, 已下线)")
    except sqlite3.OperationalError:
        pass

    if issues:
        return _check("数据流断裂", False, "; ".join(issues[:3]), severity="danger",
                      fix="跑 wf_logistics_alerts 重新触发同步")
    return _check("数据流断裂", True, "alerts → replenishment_queue 流通正常")


# ── 5. 死引用扫描 (P0#5 / P0#7 防 sa_main 回归) ──
def check_dead_references() -> Dict:
    bad_patterns = [
        ("FROM sa_main", "活的 SQL 读 sa_main"),
        ("UPDATE sa_main", "活的 SQL 写 sa_main"),
        ("INSERT INTO sa_main", "活的 SQL 写 sa_main"),
    ]
    findings = []
    scan_paths = [
        os.path.join(HIPOP_ROOT, "workflows"),
        os.path.join(HIPOP_ROOT, "scripts"),
        os.path.join(HIPOP_ROOT, "server"),
    ]
    for path in scan_paths:
        for root, _, files in os.walk(path):
            for f in files:
                if not f.endswith(".py"): continue
                # 跳过巡检脚本自己 (避免 bad_patterns 字符串字面值误匹配)
                if f == "audit.py": continue
                fp = os.path.join(root, f)
                # 跳过 deprecated 老文件
                with open(fp, encoding="utf-8") as fh:
                    content = fh.read()
                if "DEPRECATED" in content[:500]:
                    continue
                for line_no, line in enumerate(content.splitlines(), 1):
                    # 只看代码行, 不看注释 (#)
                    code_part = line.split("#")[0]
                    # 跳过 docstring 内的字面值 (粗略: 行开头是引号)
                    stripped = code_part.strip()
                    if stripped.startswith('"') or stripped.startswith("'"): continue
                    for pattern, desc in bad_patterns:
                        if pattern in code_part:
                            findings.append(f"{os.path.relpath(fp, PROJECT_ROOT)}:{line_no}")
                            break
    if findings:
        return _check("死引用扫描", False,
                      f"发现 {len(findings)} 处活的 sa_main 引用: {', '.join(findings[:3])}",
                      severity="danger",
                      fix="改用 wf1_<alias>_stock / wf2_<alias>_sku")
    return _check("死引用扫描", True, "全链路 0 处活的 sa_main 引用")


# ── 6. 算法基线 (KSA 业务指标突变检测) ──
def check_algorithm_baseline(store: str) -> Dict:
    s = store.lower()
    issues = []
    total = _scalar(f"SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle") or 0
    if total == 0:
        return _check("算法基线", True, "wf5 表为空, 跳过", severity="info")

    have_sales = _scalar(f"SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle WHERE trend != '无销量'") or 0
    weekly = _scalar(f"SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle WHERE COALESCE(weekly_total_replenish,0) > 0") or 0
    target_pos = _scalar(f"SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle WHERE COALESCE(target_pipeline,0) > 0") or 0
    slow_in_sales = _scalar(f"""
        SELECT COUNT(*) FROM wf5_hipop_{s}_sales_cycle
        WHERE trigger_reasons LIKE '%慢销%' AND trend != '无销量'
    """) or 0

    # 阈值 (KSA 标杆)
    if store.upper() == "KSA":
        if have_sales > 0:
            slow_pct = slow_in_sales * 100 / have_sales
            if slow_pct > 30:
                issues.append(f"有销量内慢销卷入率 {slow_pct:.0f}% > 30% (慢销 v2 退化警告!)")
            if weekly < 30:
                issues.append(f"本周补货 SKU 仅 {weekly} 个 (基线 ≥ 50)")
        target_pct = target_pos * 100 / total
        if target_pct < 20:
            issues.append(f"target>0 比例 {target_pct:.0f}% < 20% (hub 数据稀疏?)")
    if issues:
        return _check("算法基线 (KSA)", False, "; ".join(issues), severity="warn",
                      fix="对照 §十五 慢销 v2 三条件 AND")
    return _check("算法基线", True,
                  f"target>0 {target_pos}/{total}, 慢销内卷入 {slow_in_sales}/{have_sales}, 周补货 {weekly}")


# ── 7. 慢销规则代码漂移 (P0#8 防退回单变量) ──
def check_slow_rule_code() -> Dict:
    fp = os.path.join(HIPOP_ROOT, "workflows", "wf_sales_cycle.py")
    if not os.path.exists(fp):
        return _check("慢销规则", False, "wf_sales_cycle.py 不存在!", severity="danger")
    with open(fp, encoding="utf-8") as f:
        content = f.read()

    # 必须存在 v2 三条件标记
    required_markers = ["is_growth_trend", "is_pipeline_safe", "slow_mover"]
    missing = [m for m in required_markers if m not in content]
    if missing:
        return _check("慢销规则", False,
                      f"wf_sales_cycle.py 缺关键变量 {missing}, 可能退回单变量阈值!",
                      severity="danger",
                      fix="对照 §十五 慢销 v2, 重新加 trend + pipeline 保护")
    # 必须能找到 AND 连接的 slow_mover 表达式
    if not re.search(r"slow_mover\s*=.*and.*and", content):
        return _check("慢销规则", False,
                      "wf_sales_cycle.py 的 slow_mover 不是三条件 AND, 可能被简化",
                      severity="danger",
                      fix="对照 §十五 慢销 v2 三条件 AND")
    return _check("慢销规则", True, "wf_sales_cycle 慢销 v2 三条件 AND 完整")


# ── 8. Canary SKU (TBJ0059A 必须给补货建议) ──
def check_canary_skus(store: str) -> Dict:
    if store.upper() != "KSA":
        return _check("Canary SKU", True, "非 KSA, 跳过", severity="info")
    issues = []
    canaries = [
        ("TBJ0059A", "加速增长", True),  # (sku, expected_trend, expect_replenish>0)
        ("TBA0210A", None, None),       # 只检查存在
    ]
    for sku, exp_trend, exp_replenish in canaries:
        with _conn() as c:
            row = c.execute(f"""
                SELECT trend, weekly_total_replenish FROM wf5_hipop_ksa_sales_cycle
                WHERE partner_sku = ?
            """, (sku,)).fetchone()
        if not row:
            issues.append(f"{sku} 不在 wf5 中")
            continue
        if exp_trend and row["trend"] != exp_trend:
            issues.append(f"{sku} trend={row['trend']}, 期望 {exp_trend}")
        if exp_replenish and not (row["weekly_total_replenish"] or 0) > 0:
            issues.append(f"{sku} weekly_total_replenish=0, 应给补货建议")
    if issues:
        return _check("Canary SKU", False, "; ".join(issues), severity="warn")
    return _check("Canary SKU", True, "TBJ0059A 等基准 SKU 状态正常")


# ── 9. 孤儿表 / 必需表存在 ──
def check_required_tables() -> Dict:
    required = [
        "wf1_hipop_ksa_stock", "wf2_hipop_ksa_sku", "wf2_hipop_ksa_orders",
        "wf3_logistics_hub", "wf5_hipop_ksa_sales_cycle",
        "wf6_logistics_alerts", "wf6_hipop_ksa_replenishment_queue",
        "agent_actions", "agent_events", "feishu_digest",
    ]
    with _conn() as c:
        existing = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in required if t not in existing]
    if missing:
        return _check("必需表存在", False, f"缺表: {missing}", severity="danger",
                      fix="跑 sales_entity.ensure_tables")
    return _check("必需表存在", True, f"{len(required)} 个必需表都在")


# ── 10. P0 回归测试 (作为 invariant) ──
def check_p0_regression() -> Dict:
    test_file = os.path.join(PROJECT_ROOT, "tests", "test_phase1.py")
    if not os.path.exists(test_file):
        return _check("P0 回归", False, "tests/test_phase1.py 不存在", severity="warn")

    # 只跑 P0 子集 (快, 5s)
    try:
        result = subprocess.run(
            ["python3", "-c", """
import sys, os
sys.path.insert(0, 'tests')
sys.path.insert(0, '.')
from test_phase1 import test_p0_wf2_aggregation, test_p0_wf5_target_pipeline, test_strategies_files_exist
fails = []
for fn in (test_p0_wf2_aggregation, test_p0_wf5_target_pipeline, test_strategies_files_exist):
    try: fn()
    except Exception as e: fails.append(f'{fn.__name__}: {e}')
print('|'.join(fails) if fails else 'OK')
"""],
            capture_output=True, text=True, timeout=15, cwd=PROJECT_ROOT,
        )
        out = (result.stdout or "").strip()
        if out == "OK":
            return _check("P0 回归测试", True, "3 个核心 P0 测试通过")
        return _check("P0 回归测试", False, out[:200], severity="danger",
                      fix="跑 python3 tests/test_phase1.py 看完整输出")
    except subprocess.TimeoutExpired:
        return _check("P0 回归测试", False, "测试超时 (>15s)", severity="warn")
    except Exception as e:
        return _check("P0 回归测试", False, f"运行错误: {e}", severity="warn")


# ── 主入口 ──
ALL_CHECKS = [
    ("agg",        check_aggregation_consistency),
    ("pool",       check_sku_pool_drift),
    ("freshness",  check_data_freshness),
    ("flow",       check_data_flow_integrity),
    ("deadref",    check_dead_references),
    ("baseline",   check_algorithm_baseline),
    ("slow_rule",  check_slow_rule_code),
    ("canary",     check_canary_skus),
    ("tables",     check_required_tables),
    ("regression", check_p0_regression),
]


def run_audit(store: str = "ksa") -> List[Dict]:
    """跑所有 invariant 检查, 返回结果列表."""
    results = []
    for key, fn in ALL_CHECKS:
        try:
            sig = fn.__code__.co_varnames[:fn.__code__.co_argcount]
            r = fn(store) if "store" in sig else fn()
            r["key"] = key
            results.append(r)
        except Exception as e:
            results.append({
                "key": key, "check": fn.__name__, "status": "danger",
                "severity": "danger", "message": f"巡检脚本崩了: {e}",
                "fix": "看 audit.py 代码",
            })
    return results


def get_summary(store: str = "ksa") -> Dict:
    """聚合: ok/warn/danger 计数 + 整体状态"""
    results = run_audit(store)
    counts = {"ok": 0, "warn": 0, "danger": 0, "info": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if counts["danger"] > 0:
        overall = "danger"
    elif counts["warn"] > 0:
        overall = "warn"
    else:
        overall = "ok"
    return {
        "store": store.upper(),
        "overall": overall,
        "counts": counts,
        "checks": results,
        "as_of": datetime.datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import json
    out = get_summary("ksa")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
