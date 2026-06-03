"""Smoke: WS-58 门2 返工 — noon 订单 live producer **生产侧自动接线**（fresh-process）。

承重墙（接线缺失死法 · 门2 打回点）：
  「写了抓取器 + 能手动 register」≠「生产入口已接上」。本 smoke 起一个**全新解释器进程**，
  只 import 生产入口（`hipop.runtime.workflow_runners`，worker/api 运行任何 workflow 都会加载它），
  **不手动调** `register_live_producer()`，断言：
    ① `get_live_row_producer('orders')` 非 None；
    ② `missing_live_producers()` 不含 'orders'；
    ③ `run_live` **默认**（不传 live_producer）就走这个已接线的订单 producer
       —— 用 sentinel 替身 `_get_session` 证明默认路径真的调到了它。

fail-then-pass（不靠改代码，用接线开关复刻 fail 态）：
  · 生产默认（自动接线）→ 子进程断言全绿（WIRING_OK）。
  · `HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1`（跳过自动接线）→ 子进程发现 orders 未就位
    （producer is None / missing 仍含 orders）→ 断言失败（复刻「未接线 → 红」）。
  改动前（live_producers 不存在 / workflow_runners 不 import 它）→ 默认子进程同样红。

跑法：
  python3 tests/smoke_noon_order_wiring.py        # 被 make test 自动聚合
  （fresh 子进程落临时 SQLite，不连紫鸟、不碰 PG / live hipop.db。）
"""
import os
import sys
import json
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


# ── 子进程：只 import 生产入口，断言 orders 已自动接线 ───────────────────────
def _child() -> int:
    db = tempfile.NamedTemporaryFile(suffix="_wiring.db", delete=False).name
    os.environ.pop("DB_URL", None)
    os.environ["HIPOP_DB"] = db
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "hipop"))
    sys.path.insert(0, os.path.join(REPO, "hipop", "scripts"))

    # 生产入口：worker/api 运行任何 workflow 加载的注册表；连带加载 live_producers 接线。
    # 刻意**不**手动 register_live_producer()——证明的是「生产默认已接上」，不是「手动能接」。
    from hipop.runtime import workflow_runners  # noqa: F401
    import noon_live_contract as C
    import ingest_noon_csv_v2 as noon
    import noon_order_fetcher as F

    skip = os.environ.get("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE") == "1"

    # ① / ②：生产入口加载后，orders live producer 已就位（默认态）。
    prod = C.get_live_row_producer(C.ORDERS)
    miss = C.missing_live_producers()
    ingest_view = noon.get_live_row_producer()
    if skip:
        # 复刻 fail 态：跳过接线 → 必须 None / missing 含 orders（证明是接线让它非 None）。
        ok = (prod is None and "orders" in miss and ingest_view is None)
        print(json.dumps({"mode": "skip", "producer_set": prod is not None,
                          "missing_has_orders": "orders" in miss}, ensure_ascii=False))
        return 0 if ok else 1

    if prod is None:
        print("FAIL: 生产入口加载后 orders live producer 仍未注册（接线缺失死法）")
        return 1
    if "orders" in miss:
        print(f"FAIL: missing_live_producers 仍含 orders: {miss}")
        return 1
    if ingest_view is None or ingest_view is not prod:
        print("FAIL: ingest 视图未读到同一已接线 producer（单一来源破）")
        return 1

    # ③ run_live 默认走该 producer：patch _get_session 打 sentinel，证明默认路径
    # （不传 live_producer）真的调到了已接线的订单 producer → get_platform_session。
    SENTINEL = "WIRED_ORDERS_PRODUCER_SENTINEL_7x"

    def _boom(*a, **k):
        raise RuntimeError(SENTINEL)
    F._get_session = _boom

    raised = ""
    try:
        with tempfile.TemporaryDirectory() as empty:
            noon.run_live(1, inbox=empty)  # 无 live_producer 参数 → 读默认注册表
    except noon.LiveSourceUnavailable as e:
        raised = str(e)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: run_live 默认路径异常类型不对: {type(e).__name__}: {e}")
        return 1
    if SENTINEL not in raised:
        print(f"FAIL: run_live 默认未走已接线 orders producer（疑似没读默认注册表）: {raised!r}")
        return 1

    print("WIRING_OK")
    return 0


# ── 父进程：起 fresh 解释器跑子进程（默认 + skip 两态）──────────────────────
def _run_child(extra_env) -> tuple:
    env = dict(os.environ)
    env["HIPOP_WIRING_CHILD"] = "1"
    env.pop("HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE", None)
    env.update(extra_env)
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    # 1) 生产默认：fresh 进程只 import 生产入口 → orders 自动接线 + run_live 默认走它。
    rc, out = _run_child({})
    assert rc == 0 and "WIRING_OK" in out, \
        f"生产默认应自动接线 orders 且 run_live 默认走它，子进程未绿:\n{out}"
    print("✓ fresh 进程 import 生产入口（workflow_runners）→ orders live producer 自动就位，"
          "missing 不含 orders，run_live 默认走真抓取器（接线缺失死法已堵）")

    # 2) fail 态：跳过自动接线 → 同一断言下 orders 未就位（证明非 None 来自接线）。
    rc2, out2 = _run_child({"HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE": "1"})
    assert rc2 == 0, \
        f"skip 态子进程应确认「未接线 → orders 未就位」(producer None / missing 含 orders)，得:\n{out2}"
    assert '"producer_set": false' in out2 and '"missing_has_orders": true' in out2, \
        f"skip 态应显示 orders 未就位: {out2}"
    print("✓ 跳过自动接线（HIPOP_SKIP_LIVE_PRODUCER_AUTOWIRE=1）→ orders 未就位（fail-then-pass 的 fail 态成立）")

    print("\n2/2 passed")
    return 0


if __name__ == "__main__":
    if os.environ.get("HIPOP_WIRING_CHILD") == "1":
        sys.exit(_child())
    sys.exit(main())
