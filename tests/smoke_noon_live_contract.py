"""Smoke: WS-32.1 — noon 实时行契约【唯一来源】+ producer 注册守门。

承重墙(契约门头):noon 三类数据(订单/可售库存/ASN)换源时,live 行必须和
现有 CSV 入口同形、字段契约必须收成一处,否则 WS-35/37/WS-N2 各自定字段 → 漂移。

钉死三种死法:
  · 接线缺失:三类 live producer 未全注册时 `missing_producers()` 必须非空且【指出缺哪类】,
    据此红灯——防「以为接好了实则没接」。
  · 占位假数据:行缺必填字段(含 SKU 三键全缺)必须被 `validate_row` 红灯 raise,
    绝不默认编数。
  · 契约漂移:三类行字段契约只有一个来源 `NOON_LIVE_ROW_SPECS`,且三份 fixture
    必须逐行过契约校验——契约和 fixture 任一漂移即红。

fail-then-pass:
  改动前 `noon_live_contract` 模块不存在 → ImportError(红);实现后 → 全 pass。
  另在用例内做真 fail-then-pass:把合规行删掉一个必填字段后,validate_row 必须由
  「不 raise」变「raise」,证明守门不是空断言。

跑法:
  python3 tests/smoke_noon_live_contract.py    或    make test
(纯内存校验,不碰 DB / 不碰 live。)
"""
import os
import sys
import json
import copy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "hipop"))
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "noon_live")
_passed = 0


def ok(cond, msg):
    global _passed
    if not cond:
        print(f"  ✗ {msg}")
        raise SystemExit(1)
    _passed += 1
    print(f"  ✓ {msg}")


def load(kind):
    with open(os.path.join(FIX, f"{kind}_rows.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    print("\n▶ WS-32.1 noon live 行契约守门")

    # ── 0. 三类契约都在唯一来源里,且键名齐 ────────────────────────────
    ok(set(C.KINDS) == set(C.NOON_LIVE_ROW_SPECS), "三类(orders/inventory/asn)契约都在 NOON_LIVE_ROW_SPECS")
    for kind in C.KINDS:
        spec = C.NOON_LIVE_ROW_SPECS[kind]
        ok({"required", "sku_alt", "recognized", "feeds"} <= set(spec),
           f"{kind} 契约含 required/sku_alt/recognized/feeds")

    # ── 1. 接线缺失死法:全未注册 → missing_producers 指出全部三类 ──────
    C.reset_producers()
    miss = C.missing_producers()
    ok(miss == list(C.KINDS), f"全未注册时 missing_producers 指出全部三类: {miss}")
    ok(C.registered_kinds() == [], "全未注册时 registered_kinds 为空")

    # 注册其中两类 → 仍红灯,且只指剩下的 asn
    C.set_live_row_producer("orders", lambda tid: iter(load("orders")))
    C.set_live_row_producer("inventory", lambda tid: iter(load("inventory")))
    ok(C.missing_producers() == ["asn"], "注册 orders+inventory 后只缺 asn(指出具体缺哪类)")

    # 补齐第三类 → 不再红灯
    C.set_live_row_producer("asn", lambda tid: iter(load("asn")))
    ok(C.missing_producers() == [], "三类全注册后 missing_producers 清空")
    ok(C.registered_kinds() == list(C.KINDS), "三类全注册后 registered_kinds 齐")

    # producer 真能产出行,且产出行同形可校验
    rows = list(C.get_live_row_producer("orders")(1))
    ok(len(rows) == len(load("orders")), "已注册 orders producer 能产出 live 行")

    # ── 2. 三份 fixture 逐行过契约(契约↔fixture 不漂移) ───────────────
    for kind in C.KINDS:
        data = load(kind)
        ok(len(data) >= 1, f"{kind} fixture 非空")
        for i, row in enumerate(data):
            miss = C.missing_fields(kind, row)
            ok(miss == [], f"{kind} fixture 第{i}行过契约(缺: {miss})")
            # 行携带的键都在 recognized 里(不夹带未声明字段 → 防漂移)
            unknown = set(row) - set(C.NOON_LIVE_ROW_SPECS[kind]["recognized"])
            ok(not unknown, f"{kind} fixture 第{i}行无越契约字段(多: {unknown})")

    # ── 3. 占位假数据死法 + 真 fail-then-pass ─────────────────────────
    # 合规行先确认不 raise,再删一个必填字段 → 必须由「过」变「红灯 raise」。
    good = copy.deepcopy(load("inventory")[0])
    C.validate_row("inventory", good)  # 不应 raise
    bad = copy.deepcopy(good)
    del bad["inventory_type"]
    raised = False
    try:
        C.validate_row("inventory", bad)
    except C.LiveContractError as e:
        raised = True
        ok("inventory_type" in str(e), "缺字段 raise 指出具体字段名 inventory_type")
    ok(raised, "删必填字段后 validate_row 由过转红灯(fail-then-pass,非空断言)")

    # SKU 三键全缺也红灯(sku_alt 三选一)
    no_sku = {"item_nr": "ORD-X"}
    ok("partner_sku|sku|noon_sku" in C.missing_fields("orders", no_sku),
       "orders 行 SKU 三键全缺被红灯指出")

    # 未知数据类红灯
    bad_kind = False
    try:
        C.validate_row("nonsense", {})
    except C.LiveContractError:
        bad_kind = True
    ok(bad_kind, "未知数据类 raise LiveContractError")

    C.reset_producers()
    print(f"\n✓ noon live 行契约守门全过（{_passed} assertions）")


if __name__ == "__main__":
    main()
