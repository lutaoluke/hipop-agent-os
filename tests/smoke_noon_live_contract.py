"""Smoke: WS-32.1 / WS-34 — noon 实时「行契约」唯一来源 + 守门红灯。

承重墙(数据契约 + 确定性守门 smoke)。本 smoke 钉死三件事，作为 WS-35/37
socket、WS-N2/57 抓取器、WS-38 收口的共同前置：

  ① 三类 live producer 未全注册时必须红灯，并指出缺哪类
     —— assert_live_producers_ready() raise LiveSourceUnavailable 且点名缺失类。
     这是 WS-38「迁移完成」判定的硬线：缺来源就 blocked，不回落 CSV 冒充 live。
  ② 三类行 fixture 就位且逐行合契约(REQUIRED + SKU 主键来源)。
  ③ 字段缺失 / 缺 SKU 主键 → 红灯(validate_row raise)，绝不默认编数。

守三种死法：
  · 占位假数据：fixture 不是空壳——逐行过 row_problems，且必填缺失行被拒。
  · 契约漂移：fixture 列头必须 ⊆ 契约 known 字段；ROW_CONTRACT 覆盖且仅覆盖
    KINDS——任何脚本另定字段都会被这里的「列头 ⊆ known」抓到。
  · 接线缺失：producer 注册表是真注册/真清除——注册 orders 后 missing 只剩另两类。

fail-then-pass：本 smoke 先写(断言 noon_live_contract 的契约/红灯行为)，
此前该模块不存在 → import 失败 / 断言 fail；落地模块后 → pass。
跑法：python3 tests/smoke_noon_live_contract.py   或   make test
（纯内存断言，不碰 DB / 不碰 live。）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

import noon_live_contract as C  # noqa: E402


def _expect_raise(fn, exc, what):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"应红灯却没 raise {exc.__name__}: {what}")


def main():
    # 隔离：本 smoke 全程自己掌控注册表状态，结束清空，不污染其它 smoke。
    for k in C.KINDS:
        C.set_live_row_producer(k, None)
    try:
        print("== ① 契约覆盖且仅覆盖三类 ==")
        assert set(C.ROW_CONTRACT) == set(C.KINDS), "ROW_CONTRACT 必须恰好覆盖 KINDS"
        assert set(C.FIXTURES) == set(C.KINDS), "FIXTURES 必须恰好覆盖 KINDS"
        print(f"  ✓ KINDS = {C.KINDS}")

        print("== ② 三类 producer 全未注册 → 红灯并点名缺失 ==")
        assert C.missing_live_producers() == list(C.KINDS), "未注册时应报全部缺失"
        try:
            C.assert_live_producers_ready()
            raise AssertionError("全未注册却没红灯")
        except C.LiveSourceUnavailable as e:
            for k in C.KINDS:
                assert k in str(e), f"红灯消息应点名缺失类 {k}"
        print("  ✓ 全缺时 raise 且点名 orders/my_inventory/asn")

        print("== ③ 注册 orders 后 missing 只剩另两类（真注册/真清除）==")
        C.set_live_row_producer(C.ORDERS, lambda tenant_id: [])
        assert C.missing_live_producers() == [C.MY_INVENTORY, C.ASN], \
            "注册 orders 后应只缺 my_inventory/asn"
        _expect_raise(C.assert_live_producers_ready, C.LiveSourceUnavailable,
                      "仍缺两类应红灯")
        C.set_live_row_producer(C.ORDERS, None)
        assert C.missing_live_producers() == list(C.KINDS), "清除后应回到全缺"
        print("  ✓ 注册/清除生效，missing 随之变化")

        print("== ④ 三类全注册 → 不再红灯 ==")
        for k in C.KINDS:
            C.set_live_row_producer(k, lambda tenant_id: [])
        assert C.missing_live_producers() == [], "全注册后应无缺失"
        C.assert_live_producers_ready()  # 不 raise
        for k in C.KINDS:
            C.set_live_row_producer(k, None)
        print("  ✓ 全注册时放行")

        print("== ⑤ 三类 fixture 就位、逐行合契约、列头 ⊆ known（防漂移）==")
        for kind in C.KINDS:
            path = C.FIXTURES[kind]
            assert os.path.isfile(path), f"{kind} fixture 缺失: {path}"
            rows = C.load_fixture_rows(kind)
            assert rows, f"{kind} fixture 不能为空"
            known = set(C.ROW_CONTRACT[kind]["known"])
            cols = set(rows[0].keys())
            assert cols <= known, \
                f"{kind} fixture 列头越出契约 known: {cols - known}"
            for i, row in enumerate(rows):
                problems = C.row_problems(kind, row)
                assert not problems, f"{kind} fixture 第{i}行不合契约: {problems}"
            print(f"  ✓ {kind}: {len(rows)} 行合契约，列头 ⊆ known")

        print("== ⑥ 字段缺失 / 缺 SKU 主键 → 红灯，不默认编数 ==")
        # 必填缺失
        bad_orders = {"sku": "ZSA001", "item_nr": "IT9"}  # 缺 partner_sku
        assert "缺必填字段 partner_sku" in "; ".join(C.row_problems(C.ORDERS, bad_orders))
        _expect_raise(lambda: C.validate_row(C.ORDERS, bad_orders),
                      C.LiveSourceUnavailable, "orders 缺 partner_sku")
        # 缺 SKU 主键来源（my_inventory 三个候选全空）
        bad_inv = {"country_code": "SA", "qty": "5", "inventory_type": "saleable"}
        probs = C.row_problems(C.MY_INVENTORY, bad_inv)
        assert any("SKU 主键" in p for p in probs), f"应报缺 SKU 主键: {probs}"
        # 空值视同缺失
        assert C.row_problems(C.ASN, {"asn_number": "", "qty": "", "country_code": ""}), \
            "空字符串应视同缺失红灯"
        print("  ✓ 必填/主键缺失逐项点名并 raise")

        print("\n✓ noon 实时行契约 + 守门红灯 smoke 全过")
    finally:
        for k in C.KINDS:
            C.set_live_row_producer(k, None)


if __name__ == "__main__":
    main()
