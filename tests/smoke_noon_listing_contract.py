"""Smoke: WS-183/S1 — noon listing 行契约 + 配置占位。

fail-then-pass 证明（与 smoke_noon_live_contract 独立，互不依赖）：
  · 改前：C.LISTINGS 不存在 → AttributeError，smoke 直接红。
  · 改后：
    ① LISTINGS 常量就位，在 ROW_CONTRACT / FIXTURES 中有定义。
    ② listing fixture 逐行合契约（列头 ⊆ known，必填非空，SKU 主键非空）。
    ③ 缺 SKU 来源 → LiveSourceUnavailable。
    ④ 缺在售状态(listing_status) → LiveSourceUnavailable。
    ⑤ 未知字段（自造）→ LiveSourceUnavailable。
    ⑥ 合法行无误伤。
    ⑦ KINDS(refresh 集) 仍只含 orders/my_inventory/asn（listings 不接 refresh_all_v2）。
    ⑧ platform_browser.platforms.noon.listings 占位键存在且无真实凭据。

跑法：python3 tests/smoke_noon_listing_contract.py   或   make test
（纯内存/文件读，不碰 DB / 不碰 live。）
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402

# ── fail-then-pass 守门：改前 LISTINGS 不存在，下一行 AttributeError 即红 ──
assert hasattr(C, "LISTINGS"), "LISTINGS 常量未定义（fail-then-pass: 改前预期红灯）"


def _expect_raise(fn, exc, what):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"应红灯却没 raise {exc.__name__}: {what}")


def main():
    print("== ① LISTINGS kind 就位 ==")
    assert C.LISTINGS == "listings", f"LISTINGS 应为 'listings'，got {C.LISTINGS!r}"
    assert C.LISTINGS in C.ROW_CONTRACT, "LISTINGS 应在 ROW_CONTRACT"
    assert C.LISTINGS in C.FIXTURES, "LISTINGS 应在 FIXTURES"
    spec = C.ROW_CONTRACT[C.LISTINGS]
    assert "required" in spec and "sku_key_one_of" in spec and "known" in spec, \
        "listing ROW_CONTRACT 缺 required/sku_key_one_of/known"
    # listing_status 和 country_code 是对账必填
    assert "listing_status" in spec["required"], "listing_status 应为 required"
    assert "country_code" in spec["required"], "country_code 应为 required"
    # SKU 主键候选
    for f in ("noon_sku", "partner_sku", "sku"):
        assert f in spec["sku_key_one_of"], f"{f} 应在 sku_key_one_of"
    print(f"  ✓ LISTINGS = {C.LISTINGS!r}，required={spec['required']}，"
          f"sku_key_one_of={spec['sku_key_one_of']}")

    print("== ② listing fixture 就位、逐行合契约、列头 ⊆ known ==")
    path = C.FIXTURES[C.LISTINGS]
    assert os.path.isfile(path), f"listing fixture 缺失: {path}"
    rows = C.load_fixture_rows(C.LISTINGS)
    assert rows, "listing fixture 不能为空"
    known = set(C.ROW_CONTRACT[C.LISTINGS]["known"])
    cols = set(rows[0].keys())
    assert cols <= known, f"listing fixture 列头越出契约 known: {cols - known}"
    for i, row in enumerate(rows):
        problems = C.row_problems(C.LISTINGS, row)
        assert not problems, f"listing fixture 第{i}行不合契约: {problems}"
    print(f"  ✓ {len(rows)} 行合契约，列头 ⊆ known")

    print("== ③ 缺 SKU 来源 → 红灯 ==")
    no_sku = {"country_code": "SA", "listing_status": "active"}
    probs = C.row_problems(C.LISTINGS, no_sku)
    assert any("SKU 主键" in p for p in probs), f"缺 SKU 来源应红灯: {probs}"
    _expect_raise(lambda: C.validate_row(C.LISTINGS, no_sku),
                  C.LiveSourceUnavailable, "listing 缺 SKU 主键")
    print("  ✓ 缺 noon_sku/partner_sku/sku 全空 → red")

    print("== ④ 缺在售状态(listing_status) → 红灯 ==")
    no_status = {"country_code": "SA", "noon_sku": "NOON001"}
    probs = C.row_problems(C.LISTINGS, no_status)
    assert any("listing_status" in p for p in probs), \
        f"缺 listing_status 应红灯: {probs}"
    _expect_raise(lambda: C.validate_row(C.LISTINGS, no_status),
                  C.LiveSourceUnavailable, "listing 缺 listing_status")
    # 空字符串视同缺失
    empty_status = {"country_code": "SA", "noon_sku": "NOON001", "listing_status": ""}
    assert any("listing_status" in p for p in C.row_problems(C.LISTINGS, empty_status)), \
        "空 listing_status 应视同缺失红灯"
    print("  ✓ 缺/空 listing_status → red")

    print("== ⑤ 未知字段（自造）→ 红灯（防漂移）==")
    bad_row = {
        "country_code": "SA", "noon_sku": "NOON001",
        "listing_status": "active", "invented_field": "x",
    }
    probs = C.row_problems(C.LISTINGS, bad_row)
    assert any("未知字段 invented_field" in p for p in probs), \
        f"自造字段应红灯: {probs}"
    _expect_raise(lambda: C.validate_row(C.LISTINGS, bad_row),
                  C.LiveSourceUnavailable, "listing 带契约外字段")
    print("  ✓ invented_field → red")

    print("== ⑥ 合法行无误伤 ==")
    ok_row = {"country_code": "SA", "noon_sku": "NOON001", "listing_status": "active"}
    assert C.row_problems(C.LISTINGS, ok_row) == [], \
        f"合法 listing 行被误判: {C.row_problems(C.LISTINGS, ok_row)}"
    ok_full = {
        "country_code": "AE", "store_name": "HIPOP-NOON-AE",
        "noon_sku": "NOON002", "partner_sku": "PAE001", "sku": "ZAE001",
        "listing_status": "active", "is_listed": "1", "title": "Product B",
    }
    assert C.row_problems(C.LISTINGS, ok_full) == [], \
        f"合法全字段 listing 行被误判: {C.row_problems(C.LISTINGS, ok_full)}"
    print("  ✓ 合法行零问题")

    print("== ⑦ KINDS(refresh 集)不含 LISTINGS（不接 refresh_all_v2）==")
    assert C.LISTINGS not in C.KINDS, \
        "LISTINGS 不应在 KINDS(refresh 集)，以免 assert_live_producers_ready 要求 listing producer"
    assert set(C.KINDS) == {C.ORDERS, C.MY_INVENTORY, C.ASN}, \
        f"KINDS 应恰为 orders/my_inventory/asn，got {C.KINDS}"
    # listing kind 不出现在 missing_live_producers
    missing = C.missing_live_producers()
    assert C.LISTINGS not in missing, \
        "listings 不应出现在 missing_live_producers（不走 refresh 注册表）"
    print(f"  ✓ KINDS = {C.KINDS}，missing 不含 listings")

    print("== ⑧ platform_browser.platforms.noon.listings 占位就位，无真实凭据 ==")
    config_path = os.path.join(REPO, "hipop", "config", "hipop.json")
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    noon_cfg = cfg.get("platform_browser", {}).get("platforms", {}).get("noon", {})
    assert "listings" in noon_cfg, \
        "hipop.json platform_browser.platforms.noon 缺 listings 占位"
    listing_cfg = noon_cfg["listings"]
    # api_url 必须是 env 占位符（${ 开头），绝不写死真实 URL
    api_url = listing_cfg.get("api_url", "")
    assert api_url.startswith("${"), \
        f"listings api_url 应为 env 占位符，got {api_url!r}（禁写真实 URL/凭据）"
    print(f"  ✓ listings 占位存在，api_url={api_url!r}")

    print("\n✓ noon listing 行契约 + 配置占位 smoke 全过")


if __name__ == "__main__":
    main()
