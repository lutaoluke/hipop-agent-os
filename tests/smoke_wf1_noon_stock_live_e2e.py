"""Smoke: WS-32.3 / WS-36 — noon 可售库存「真走 live」端到端集成（接 WS-59 producer → runner → source==live）。

承重墙（钉死「假绿 / 静默回落 CSV 冒充 source=live」死法 · 本条核心）：
  WS-59 把 noon 库存抓取器接进生产默认注册表（`live_producers.register_all` 加了
  my_inventory 一行），姊妹 smoke 各自只证明了**一半**：
    · `smoke_noon_stock_live_wiring`：生产默认确实**接到**真 WS-59 producer——但用「打到
      就 raise 的 sentinel」证可达性，真 live 行从没流到 source==live。
    · `smoke_wf1_noon_live_runner` / `smoke_wf_noon_live_verifier`：driver 是 runner，但喂的是
      **合成** producer（`lambda tenant: [...]`），不是生产自动接线的真 WS-59 抓取器。
    · `smoke_noon_stock_fetcher`：真 WS-59 抓取器 → run_live → source==live，但**手动** inject
      page_factory/raw_inventory_fn，且直调 `run_live`，没走「生产自动接线 + noon_live_ingest
      runner」这条真生产路径。
  三者都没把这三段**接成一条**：①生产默认自动接线的真 WS-59 producer（不手动 register/inject）
  ② noon_live_ingest runner ③断言真实 source==live 且 wf1_stock.noon_* 由 live 行写入。本 smoke
  补这条端到端，证明「runner 真跑出 source==live」而非「没报错就算绿」。

  唯一被替身的是**真正外部、非确定**的两点：紫鸟已登录 page（`_get_session`）与 noon 接口配置
  （`_stock_cfg`）；其余 `page.evaluate` 取数 / `_walk_records` / `to_contract_row` / `validate_row`
  / `_assert_live_qty` / run_live / `_aggregate` / `_upsert` / runner / 部分 upsert 边界，全是真
  生产代码。真紫鸟下的 live 跑法见 `noon_stock_fetcher.__main__` 与 PR。

钉死三种死法：
  · 接线缺失：fresh 进程只 import 生产入口（workflow_runners → live_producers 自动接线），**不**手动
    register —— runner 默认就走真 WS-59 producer 落 source==live。跳过自动接线（环境开关）→ 同一
    runner 不再 source==live（回落 csv_fallback），证明 live 结果**取决于**接线、非写死字符串。
  · 死代码短路 / 假绿：live 取数失败 + 无 CSV → runner red（raise），库里无凭空 noon 行；有 CSV →
    显式 csv_fallback + live_error，summary 标 `[csv_fallback]`（「未走 live」可见），绝不静默冒充 live。
  · 占位假数据：source==live 的 wf1_stock.noon_* 是真聚合值（total=saleable+unsaleable，逐 SKU 核），
    且 ERP 列 / pending_inbound_qty 不被部分 upsert 覆盖。

fail-then-pass（不靠改代码，用 WS-59 既有自动接线开关复刻 fail 态）：
  · 生产默认（自动接线）→ 子进程 runner 跑出 source==live + noon_* 由 live 写入（LIVE_OK）。
  · `HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1`（不接线）→ 同一 runner 默认拿不到 producer → 回落
    csv_fallback（NOT live）→ 「source==live」断言在该态必然 fail（fail 态成立）。
  改动前（live_producers.register_all 未加 my_inventory 一行）→ 默认子进程同样回落、非 live。

跑法：
  python3 tests/smoke_wf1_noon_stock_live_e2e.py    # 被 make test 自动聚合
  （fresh 子进程落临时 SQLite，不连紫鸟、不碰 PG / live hipop.db。）
"""
import os
import re
import sys
import csv
import json
import sqlite3
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCHEMA_V2 = os.path.join(REPO, "db", "schema_v2.sql")
TENANT = 1

# 契约键 → noon 接口风格字段名（camelCase 别名，全在 fetcher `_NOON_STOCK_FIELD_MAP` 候选里）。
# 用它把 WS-34 fixture「伪装成」noon 原始接口记录，喂真 WS-59 抓取器映射回契约键。
_RAW_KEYMAP = {
    "country_code": "countryCode", "sku": "skuCode", "warehouse_code": "warehouseCode",
    "qty": "quantity", "inventory_type": "inventoryType", "title": "title",
}
_NOON_COLS = ("noon_total_qty", "noon_saleable_qty",
              "noon_unsaleable_qty", "noon_warehouses_json")
_ERP_COLS = ("yiwu_qty", "dongguan_qty", "overseas_total_qty", "total_stock")
# 预置 ERP 行（SKU-A）：验部分 upsert 不覆盖 ERP 列 / pending_inbound_qty。
_ERP_SEED = (TENANT, "hipop_ksa", "SKU-A", 99, 88, 77, 264, 7)
EXPECT_SKUS = {"SKU-A", "SKU-B", "SKU-C"}  # 经 wf2_sku 映射后的 partner_sku


# ── 替身 page（同 smoke_platform_session / smoke_noon_stock_fetcher 形态；只实现 evaluate）──
class FakePage:
    """可控 page：evaluate(js, url) 返回预置 records JSON（让真 `_fetch_raw_inventory` 走完整路径）。"""
    def __init__(self, payload):
        self.payload = payload
        self.evaluated = []

    def evaluate(self, js, arg=None):
        self.evaluated.append(arg)
        return self.payload


def _extract_create(table):
    sql = open(SCHEMA_V2, encoding="utf-8").read()
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", sql, re.DOTALL)
    assert m, f"找不到 {table} 的 CREATE TABLE（schema_v2.sql 结构变了？）"
    return m.group(0)


def _seed_db(db):
    """建 wf1_stock + sales_entities + wf2_sku（country→entity、平台 SKU→partner_sku 映射）
    + 预置 ERP 种子行（每个子进程一份干净库）。"""
    c = sqlite3.connect(db)
    try:
        for t in ("wf1_stock", "sales_entities", "wf2_sku"):
            c.executescript(_extract_create(t))
        c.executemany(
            "INSERT INTO sales_entities (tenant_id, alias, country, platform, store_name, store_id, active) "
            "VALUES (?,?,?,?,?,?,1)",
            [(TENANT, "hipop_ksa", "SA", "Noon", "HIPOP-NOON-KSA", 85),
             (TENANT, "hipop_uae", "AE", "Noon", "HIPOP-NOON-UAE", 42)],
        )
        c.executemany(
            "INSERT INTO wf2_sku (tenant_id, entity_alias, partner_sku, noon_sku) VALUES (?,?,?,?)",
            [(TENANT, "hipop_ksa", "SKU-A", "ZSA001"),
             (TENANT, "hipop_ksa", "SKU-B", "ZSA002"),
             (TENANT, "hipop_uae", "SKU-C", "ZAE001")],
        )
        c.execute(
            "INSERT INTO wf1_stock (tenant_id, entity_alias, partner_sku, "
            "yiwu_qty, dongguan_qty, overseas_total_qty, total_stock, pending_inbound_qty) "
            "VALUES (?,?,?,?,?,?,?,?)",
            _ERP_SEED,
        )
        c.commit()
    finally:
        c.close()


def _dump(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return {r["partner_sku"]: dict(r)
                for r in c.execute("SELECT * FROM wf1_stock ORDER BY partner_sku")}
    finally:
        c.close()


def _write_fixture_csv(path, rows):
    cols = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ── 子进程：fresh 解释器只 import 生产入口，按 mode 驱动 noon_live_ingest runner ──────
def _child():
    mode = os.environ["HIPOP_E2E_MODE"]  # live / skip / live_fail_nocsv / live_fail_csv
    db = tempfile.NamedTemporaryFile(suffix="_stock_live_e2e.db", delete=False).name
    os.environ.pop("DB_URL", None)
    os.environ["HIPOP_DB"] = db
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "hipop"))
    sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))
    _seed_db(db)

    # 生产入口：worker/api 运行任何 workflow 加载的注册表；连带加载 live_producers 单一收口接线。
    # 刻意**不**手动 register_live_producer()——证明的是「生产默认已接上真 WS-59 producer」。
    from hipop.runtime import workflow_runners
    import noon_live_contract as C
    import noon_stock_fetcher as F          # 真 WS-59 抓取器（autowire 闭包就 close over 它的 globals）
    from hipop.scripts import merge_stock_snapshot_v2 as _merge

    INVENTORY_ROWS = C.load_fixture_rows(C.MY_INVENTORY)
    RAW_RECORDS = [{_RAW_KEYMAP[k]: v for k, v in r.items()} for r in INVENTORY_ROWS]

    skip = os.environ.get("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE") == "1"
    prod = C.get_live_row_producer(C.MY_INVENTORY)
    # 接线前置事实：default 态必须已自动接线；skip 态必须未接线（fail 态前提）。
    if skip and prod is not None:
        print("FAIL: skip 态下 my_inventory 仍被接线（开关失效，fail 态不成立）")
        return 1
    if not skip and prod is None:
        print("FAIL: 生产默认未自动接线真 WS-59 producer（接线缺失死法）")
        return 1

    # 替身**只**打真正外部、非确定的两点：紫鸟已登录 page + noon 接口配置。
    # 其余（page.evaluate 取数 / _walk_records / to_contract_row / validate / qty 校验）全真跑。
    def _fake_session(tenant_id, store_key, account):
        return FakePage(RAW_RECORDS)

    def _boom_session(tenant_id, store_key, account):
        # 模拟登录态失效：真 fetcher 会上抛 blocked（这里直接抛 LiveSourceUnavailable 同形）。
        raise F.LiveSourceUnavailable(
            "平台 noon 未登录：缺会话 cookie —— 请参照 refresh-dbuyerp-token 流程重登该店一次")

    F._get_session = _boom_session if mode.startswith("live_fail") else _fake_session
    F._stock_cfg = lambda store_key=F.DEFAULT_STORE_KEY: {"api_url": "https://noon.test/my-inventory"}
    # merge 归 WS-12，本条 stub 掉（runner 跑完会调它重算合并快照，与本条 source==live 无关）。
    _merge.run_v2 = lambda tenant_id, **kw: {"_stub": True}

    runner = workflow_runners.get_runner("noon_live_ingest")
    if runner is None:
        print("FAIL: noon_live_ingest runner 未注册（接线缺失死法）")
        return 1

    # CSV interim：仅在需要 fallback 输入的两态写到独立 inbox（不污染 live 态）。
    spec = {}
    tmp_inbox = tempfile.mkdtemp(prefix="e2e_inbox_")
    if mode in ("skip", "live_fail_csv"):
        _write_fixture_csv(os.path.join(tmp_inbox, "noon_inventory.csv"), INVENTORY_ROWS)
    spec = {"inbox": tmp_inbox}

    saved = {}
    raised = ""
    try:
        out = runner("tid", TENANT, None, spec, {}, lambda: None,
                     lambda p: saved.update(p))
    except Exception as e:  # noqa: BLE001
        out = None
        raised = f"{type(e).__name__}: {e}"

    rows = _dump(db)
    payload = {
        "mode": mode, "summary": (out or {}).get("summary", "") if out else "",
        "saved_source": saved.get("source"), "raised": raised,
        "skus": sorted(rows.keys()),
    }
    print("E2E_RESULT " + json.dumps(payload, ensure_ascii=False))

    # ── per-mode 断言 ───────────────────────────────────────────────────
    if mode == "live":
        if saved.get("source") != "live":
            print(f"FAIL: 生产默认 runner 未跑出 source==live（疑似静默回落 CSV 假绿）: {payload}")
            return 1
        if "[live]" not in payload["summary"]:
            print(f"FAIL: runner summary 未标 [live]: {payload['summary']!r}")
            return 1
        # noon_* 由 live 行真聚合写入（非写死/非 0）：逐 SKU 核口径。
        if set(rows) != (EXPECT_SKUS | {"SKU-A"}):
            print(f"FAIL: live 落库 partner_sku 集合不对（映射未回 partner_sku？）: {sorted(rows)}")
            return 1
        if any(k.startswith("Z") for k in rows):
            print(f"FAIL: 平台 SKU 被当主键写进去（映射未回 partner_sku）: {sorted(rows)}")
            return 1
        a = rows["SKU-A"]
        if (a["noon_total_qty"], a["noon_saleable_qty"], a["noon_unsaleable_qty"]) != (15, 10, 5):
            print(f"FAIL: SKU-A noon_* 聚合值不对（应 15/10/5）: {a}")
            return 1
        if a["noon_total_qty"] != a["noon_saleable_qty"] + a["noon_unsaleable_qty"]:
            print(f"FAIL: SKU-A total != saleable+unsaleable（疑似写死）: {a}")
            return 1
        if len(json.loads(a["noon_warehouses_json"])) != 2:
            print(f"FAIL: SKU-A 仓库明细应 2 条: {a['noon_warehouses_json']!r}")
            return 1
        # 部分 upsert 边界：ERP 列 / pending_inbound_qty 不被 noon 路径覆盖。
        if tuple(a[c] for c in _ERP_COLS) != (99, 88, 77, 264):
            print(f"FAIL: live 路径覆盖了 ERP 列: {a}")
            return 1
        if a["pending_inbound_qty"] != 7:
            print(f"FAIL: live 路径动了 pending_inbound_qty: {a}")
            return 1
        print("LIVE_OK")
        return 0

    if mode == "skip":
        # 不接线 → runner 默认拿不到 producer → 回落 csv_fallback（NOT live）。这正是
        # 「source==live」断言的 fail 态：live 结果取决于 WS-59 接线、非写死。
        if saved.get("source") == "live":
            print(f"FAIL: 未接线却报 source==live（写死/假绿）: {payload}")
            return 1
        if saved.get("source") != "csv_fallback" or "[csv_fallback]" not in payload["summary"]:
            print(f"FAIL: 未接线 + 有 CSV 应显式回落 csv_fallback 且 summary 标 [csv_fallback]: {payload}")
            return 1
        # 回落落的是真 CSV 数据（不丢运营手工数据、不写假 0）。
        if set(rows) != (EXPECT_SKUS | {"SKU-A"}):
            print(f"FAIL: csv_fallback 未落真实 CSV 库存行: {sorted(rows)}")
            return 1
        print("SKIP_FALLBACK_OK")
        return 0

    if mode == "live_fail_nocsv":
        # live 取数失败（登录失效）+ 无 CSV → runner red（raise LiveSourceUnavailable），
        # 库里无凭空 noon 行（仅剩 ERP 种子 SKU-A，noon_* 仍 NULL，不写假 0）。
        if not raised or "LiveSourceUnavailable" not in raised:
            print(f"FAIL: live 失败且无 CSV 必须 raise LiveSourceUnavailable（不得冒充成功）: {payload}")
            return 1
        if set(rows) != {"SKU-A"} or rows["SKU-A"]["noon_total_qty"] is not None:
            print(f"FAIL: red 路径凭空写了 noon 行 / 把 noon_* 写成假值: {rows.get('SKU-A')}")
            return 1
        print("LIVE_FAIL_NOCSV_OK")
        return 0

    if mode == "live_fail_csv":
        # live 取数失败 + 有 CSV interim → 显式 csv_fallback + live_error（带登录失效信号），
        # summary 标 [csv_fallback]（「未走 live / blocked」可见），绝不静默冒充 live。
        if saved.get("source") != "csv_fallback" or "[csv_fallback]" not in payload["summary"]:
            print(f"FAIL: live 失败 + 有 CSV 应显式回落 csv_fallback: {payload}")
            return 1
        if "refresh-dbuyerp-token" not in payload["summary"] and "未登录" not in payload["summary"]:
            print(f"FAIL: 回落 summary 未带 live 失败信号（未走 live 不可见）: {payload['summary']!r}")
            return 1
        if set(rows) != (EXPECT_SKUS | {"SKU-A"}):
            print(f"FAIL: csv_fallback 未落真实 CSV 库存行: {sorted(rows)}")
            return 1
        print("LIVE_FAIL_CSV_OK")
        return 0

    print(f"FAIL: 未知 mode {mode}")
    return 1


# ── 父进程：每个 mode 起一个 fresh 解释器跑子进程 ──────────────────────────
def _run_child(mode, extra_env=None):
    env = dict(os.environ)
    env["HIPOP_E2E_CHILD"] = "1"
    env["HIPOP_E2E_MODE"] = mode
    env.pop("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE", None)
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    # 1) PASS 态：生产默认自动接线真 WS-59 producer → noon_live_ingest runner 跑出 source==live
    #    + wf1_stock.noon_* 由 live 行真聚合写入 + ERP/pending 不被覆盖。
    rc, out = _run_child("live")
    assert rc == 0 and "LIVE_OK" in out, \
        f"生产默认应自动接线真 WS-59 producer，runner 跑出 source==live 且 noon_* 由 live 写入，子进程未绿:\n{out}"
    print("✓ fresh 进程只 import 生产入口 → 真 WS-59 producer 自动接线，noon_live_ingest runner "
          "跑出真实 source==live，wf1_stock.noon_* 由 live 行写入（15/10/5），ERP 列/pending 不被覆盖")

    # 2) fail 态：跳过自动接线 → 同一 runner 默认拿不到 producer → 回落 csv_fallback（NOT live）。
    #    证明 source==live 取决于 WS-59 接线、非写死字符串（fail-then-pass 的 fail 态成立）。
    rc2, out2 = _run_child("skip", {"HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE": "1"})
    assert rc2 == 0 and "SKIP_FALLBACK_OK" in out2, \
        f"跳过自动接线时同一 runner 应回落 csv_fallback（非 live），子进程未绿:\n{out2}"
    print("✓ 跳过自动接线（HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1）→ 同一 runner 默认回落 "
          "csv_fallback、不报 source==live（「source==live」断言的 fail 态成立，证明非写死）")

    # 3) live 取数失败 + 无 CSV → runner red（raise），库里无凭空 noon 行（占位假数据死法已堵）。
    rc3, out3 = _run_child("live_fail_nocsv")
    assert rc3 == 0 and "LIVE_FAIL_NOCSV_OK" in out3, \
        f"live 取数失败 + 无 CSV 应 raise 且不写假数据，子进程未绿:\n{out3}"
    print("✓ live 取数失败（登录失效）+ 无 CSV → runner 红灯 raise，库里无凭空 noon 行/假 0")

    # 4) live 取数失败 + 有 CSV interim → 显式 csv_fallback + 失败信号，summary 标「未走 live」。
    rc4, out4 = _run_child("live_fail_csv")
    assert rc4 == 0 and "LIVE_FAIL_CSV_OK" in out4, \
        f"live 取数失败 + 有 CSV 应显式回落 csv_fallback 并报失败信号，子进程未绿:\n{out4}"
    print("✓ live 取数失败 + 有 CSV interim → 显式回落 csv_fallback + live_error，"
          "summary 标 [csv_fallback]（「未走 live」可见），绝不静默冒充 live")

    print("\n4/4 passed")
    return 0


if __name__ == "__main__":
    if os.environ.get("HIPOP_E2E_CHILD") == "1":
        sys.exit(_child())
    sys.exit(main())
