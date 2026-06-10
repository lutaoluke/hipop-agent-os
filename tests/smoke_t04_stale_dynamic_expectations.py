"""smoke_t04_stale_dynamic_expectations.py

fail-then-pass smoke: T04 dynamic expectations must use _UNAVAILABLE_RE when data_stale=True.

FAIL (改前): _prepare_dynamic_expectations called _num_or_unavailable_re(sales_30d)
  with sales_30d=54 → pattern r"\b54\b" → agent's blanket stale reply never contains
  "54" → T04 chat smoke fails.

PASS (改后): when data_stale=True all metrics are treated as None →
  pattern _UNAVAILABLE_RE → stale reply "当前数值已过期" matches → T04 chat smoke passes.

This test runs without a server or DB.
"""
import re
import sys

_UNAVAILABLE_RE = (
    r"无法|暂无|不可用|未返回|未提供|实时.*(?:失败|拉不到|不可)|"
    r"ERP.*(?:凭据|登录)|同上|过期|已过期"
)


def _num_re(n):
    try:
        s = str(int(n))
    except Exception:
        return r"$^"
    if len(s) > 3:
        return rf"{s[:-3]}[,，]?{s[-3:]}"
    return rf"\b{s}\b"


def _num_or_unavailable_re(n):
    if n is None:
        return _UNAVAILABLE_RE
    return _num_re(n)


def _rate_re(rate, label):
    try:
        pct = float(rate or 0)
    except Exception:
        pct = 0.0
    if abs(pct) <= 1:
        pct *= 100
    if abs(pct) < 0.005:
        return rf"{label}.{{0,20}}0[%.]|0\.00|无{label[:2]}"
    text = f"{pct:.2f}".rstrip("0").rstrip(".")
    return re.escape(text)


def _rate_or_unavailable_re(rate, label):
    if rate is None:
        return _UNAVAILABLE_RE
    return _rate_re(rate, label)


# ── old behavior (改前): ignores data_stale ───────────────────────────────────
def _old_t04_patterns(item):
    return [
        _num_or_unavailable_re(item.get("sales_30d")),
        _num_or_unavailable_re(item.get("total_orders_30d")),
        _rate_or_unavailable_re(item.get("cancel_rate_30d"), "取消率"),
        _rate_or_unavailable_re(item.get("return_rate_30d"), "退货率"),
        _num_or_unavailable_re(item.get("history_total")),
    ]


# ── new behavior (改后): respects data_stale ─────────────────────────────────
def _new_t04_patterns(item):
    data_stale = item.get("data_stale")

    def _d(val):
        return None if data_stale else val

    return [
        _num_or_unavailable_re(_d(item.get("sales_30d"))),
        _num_or_unavailable_re(_d(item.get("total_orders_30d"))),
        _rate_or_unavailable_re(_d(item.get("cancel_rate_30d")), "取消率"),
        _rate_or_unavailable_re(_d(item.get("return_rate_30d")), "退货率"),
        _num_or_unavailable_re(_d(item.get("history_total"))),
    ]


_STALE_REPLY = (
    "TBB0116A 的数据快照截至 2026-06-09（5 天前），"
    "当前数值已过期，不能按新鲜 30 天口径报数。"
)
_FRESH_REPLY = (
    "TBB0116A 30 天口径截至 2026-06-05："
    "30 天销量 54，30 天总单量 43，历史总销量 1967，退货率 0%，取消率 5.88%。"
)


def test_stale_old_fails_new_passes():
    """改前 FAIL → 改后 PASS: data_stale=True with ERP live sales_30d=54."""
    print("== test_stale_old_fails_new_passes ==")
    stale_item = {
        "found": True,
        "data_stale": True,
        "sales_30d": 54,         # ERP live value — non-None even though noon orders stale
        "total_orders_30d": None,
        "cancel_rate_30d": None,
        "return_rate_30d": None,
        "history_total": None,
    }

    old_patterns = _old_t04_patterns(stale_item)
    old_pass = all(re.search(p, _STALE_REPLY, re.IGNORECASE) for p in old_patterns)
    assert not old_pass, "改前应 FAIL（_num_re(54) 找不到 '54' 在陈旧回复里）"
    print("  ✓ 改前 FAIL（符合预期）")

    new_patterns = _new_t04_patterns(stale_item)
    failed = [p for p in new_patterns if not re.search(p, _STALE_REPLY, re.IGNORECASE)]
    assert not failed, f"改后应 PASS，但 {len(failed)} 个 pattern 未匹配: {failed}"
    print("  ✓ 改后 PASS（_UNAVAILABLE_RE 匹配 '已过期'）")
    return []


def test_fresh_item_uses_concrete_numbers():
    """data_stale=False: patterns use concrete numbers, not _UNAVAILABLE_RE."""
    print("== test_fresh_item_uses_concrete_numbers ==")
    fresh_item = {
        "found": True,
        "data_stale": False,
        "sales_30d": 54,
        "total_orders_30d": 43,
        "cancel_rate_30d": 0.0588,
        "return_rate_30d": 0.0,
        "history_total": 1967,
    }

    patterns = _new_t04_patterns(fresh_item)
    failed = [p for p in patterns if not re.search(p, _FRESH_REPLY, re.IGNORECASE)]
    assert not failed, f"新鲜数据时 pattern 未匹配: {failed}"
    # Confirm it's using concrete numbers, not universal unavailability
    assert patterns[0] != _UNAVAILABLE_RE, "新鲜 sales_30d 不应变成 _UNAVAILABLE_RE"
    print("  ✓ 新鲜数据使用具体数字期望")
    return []


def run():
    failures = []
    for fn in [test_stale_old_fails_new_passes, test_fresh_item_uses_concrete_numbers]:
        try:
            failures += fn() or []
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {e}")
        print()
    if failures:
        print(f"✗ {len(failures)} 失败: {failures}")
        return 1
    print("✓ T04 stale dynamic expectations fail-then-pass 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
