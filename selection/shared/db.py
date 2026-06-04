"""
SQLite source of truth.

设计:
- 独立 selector.db, 不污染 hipop.db
- 后期成熟可 ATTACH hipop.db 共读 (selector 通过 thin adapter 已经能读 wf1-wf6)
- 飞书表是 view 不是 store (§3 / §8.6)

🚨 sa_main 表硬规则 (memory: feedback_no_sa_main.md):
  - 任何 SQL 列表名/schema 探查都要 `WHERE name NOT IN ('sa_main')` 显式排除
  - 任何对 sa_main 的 SELECT/写都禁止
  - 此规则同时用于 hipop.db 的 schema 探查 (selector 读 wf 表时), 也用于本模块自己
"""
from __future__ import annotations
import os, json, sqlite3
from contextlib import contextmanager
from typing import Optional

from selection.l1_normalize.product_record import ProductRecord, SKU, SalesSignal, ReturnRisk


# selection package root → ../selector.db
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "SELECTOR_DB_PATH",
    os.path.normpath(os.path.join(_HERE, "..", "selector.db")),
)


# 禁用表清单 (硬规则)
FORBIDDEN_TABLES = frozenset({"sa_main"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS sel_runs (
    run_id          TEXT PRIMARY KEY,        -- batch_<ts>_<kw_hash>
    trigger         TEXT NOT NULL,           -- monthly | keyword | category | url_drop
    keyword         TEXT,
    category        TEXT,                    -- T3 入口的品类
    markets_json    TEXT NOT NULL,           -- ["noon_ae", ...]
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,           -- running | done | failed
    note            TEXT,
    cost_credits    INTEGER DEFAULT 0        -- 累计 Firecrawl credit 消耗
);

CREATE TABLE IF NOT EXISTS sel_products (
    id                  TEXT PRIMARY KEY,    -- platform:platform_id
    run_id              TEXT,
    platform            TEXT NOT NULL,
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    brand               TEXT,
    category_path_json  TEXT,
    images_json         TEXT,
    image_embeddings_json TEXT,
    price_json          TEXT,
    sales_signal_json   TEXT NOT NULL,
    reviews_json        TEXT,
    inferred_features_json TEXT,
    shipping_json       TEXT,
    policy_flags_json   TEXT,
    return_risk_json    TEXT,
    market_meta_json    TEXT,
    fetched_at          TEXT NOT NULL,
    source_path         TEXT,
    updated_at          TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sel_products_platform ON sel_products(platform);
CREATE INDEX IF NOT EXISTS idx_sel_products_run ON sel_products(run_id);

CREATE TABLE IF NOT EXISTS sel_skus (
    product_id      TEXT NOT NULL,
    sku_idx         INTEGER NOT NULL,
    spec_axes_json  TEXT NOT NULL,
    price           REAL NOT NULL,
    currency        TEXT NOT NULL,
    stock_signal    TEXT,
    review_count    INTEGER,
    sold_count      INTEGER,
    is_oversize     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, sku_idx)
);

-- 用户对候选池的行为 (人审 + 必填理由)
CREATE TABLE IF NOT EXISTS sel_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    product_id      TEXT NOT NULL,
    stage           TEXT NOT NULL,           -- candidate_review | inquiry | sample_qc | post_launch
    action          TEXT NOT NULL,           -- accept | reject | hold | evaluate
    reason_tags_json TEXT NOT NULL,
    reason_text     TEXT,
    score           INTEGER,
    agent_predicted_json TEXT,
    outcome         TEXT,
    decided_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sel_decisions_product ON sel_decisions(product_id);
CREATE INDEX IF NOT EXISTS idx_sel_decisions_stage ON sel_decisions(stage);

-- 询盘草稿 (AI 起草 → 人审 → 旺旺手动发)
CREATE TABLE IF NOT EXISTS sel_inquiries (
    inquiry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL,
    supplier_url    TEXT,
    template_key    TEXT,                    -- spec | weight | sample | cross_border
    draft_text      TEXT NOT NULL,
    status          TEXT NOT NULL,           -- draft | approved | sent | replied | abandoned
    sent_at         TEXT,
    reply_text      TEXT,
    reply_at        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sel_inquiries_status ON sel_inquiries(status);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    """首次部署 / schema 改动后运行. 幂等."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


def safe_list_tables(c: sqlite3.Connection) -> list[str]:
    """列表名时强制排除 FORBIDDEN_TABLES (sa_main 等)."""
    forbidden = ",".join(f"'{t}'" for t in FORBIDDEN_TABLES)
    rows = c.execute(
        f"SELECT name FROM sqlite_master "
        f"WHERE type='table' AND name NOT IN ({forbidden}) "
        f"ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


# ── ProductRecord 持久化 ──────────────────────────────────────

def upsert_product(record: ProductRecord, run_id: Optional[str] = None) -> None:
    d = record.to_dict()
    with conn() as c:
        c.execute(
            """
            INSERT INTO sel_products
                (id, run_id, platform, url, title, brand,
                 category_path_json, images_json, image_embeddings_json,
                 price_json, sales_signal_json, reviews_json,
                 inferred_features_json, shipping_json, policy_flags_json,
                 return_risk_json, market_meta_json,
                 fetched_at, source_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                run_id          = COALESCE(excluded.run_id, sel_products.run_id),
                title           = excluded.title,
                brand           = COALESCE(excluded.brand, sel_products.brand),
                category_path_json = excluded.category_path_json,
                images_json     = excluded.images_json,
                image_embeddings_json = COALESCE(excluded.image_embeddings_json, sel_products.image_embeddings_json),
                price_json      = excluded.price_json,
                sales_signal_json = excluded.sales_signal_json,
                reviews_json    = COALESCE(excluded.reviews_json, sel_products.reviews_json),
                inferred_features_json = COALESCE(excluded.inferred_features_json, sel_products.inferred_features_json),
                shipping_json   = COALESCE(excluded.shipping_json, sel_products.shipping_json),
                policy_flags_json = COALESCE(excluded.policy_flags_json, sel_products.policy_flags_json),
                return_risk_json = COALESCE(excluded.return_risk_json, sel_products.return_risk_json),
                market_meta_json = COALESCE(excluded.market_meta_json, sel_products.market_meta_json),
                fetched_at      = excluded.fetched_at,
                source_path     = excluded.source_path,
                updated_at      = datetime('now', 'localtime')
            """,
            (
                record.id, run_id, record.platform, record.url, record.title, record.brand,
                json.dumps(record.category_path, ensure_ascii=False),
                json.dumps(record.images, ensure_ascii=False),
                json.dumps(record.image_embeddings, ensure_ascii=False) if record.image_embeddings else None,
                json.dumps(record.price, ensure_ascii=False),
                json.dumps(d["sales_signal"], ensure_ascii=False),
                json.dumps(record.reviews, ensure_ascii=False) if record.reviews else None,
                json.dumps(record.inferred_features, ensure_ascii=False) if record.inferred_features else None,
                json.dumps(record.shipping, ensure_ascii=False) if record.shipping else None,
                json.dumps(record.policy_flags, ensure_ascii=False) if record.policy_flags else None,
                json.dumps(d["return_risk"], ensure_ascii=False) if record.return_risk else None,
                json.dumps(record.market_meta, ensure_ascii=False) if record.market_meta else None,
                record.fetched_at.isoformat() if record.fetched_at else None,
                record.source_path,
            ),
        )
        c.execute("DELETE FROM sel_skus WHERE product_id = ?", (record.id,))
        for i, sku in enumerate(record.skus):
            c.execute(
                """INSERT INTO sel_skus
                   (product_id, sku_idx, spec_axes_json, price, currency,
                    stock_signal, review_count, sold_count, is_oversize)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (record.id, i, json.dumps(sku.spec_axes, ensure_ascii=False),
                 sku.price, sku.currency, sku.stock_signal,
                 sku.review_count, sku.sold_count, 1 if sku.is_oversize else 0),
            )


def get_product(product_id: str) -> Optional[ProductRecord]:
    with conn() as c:
        row = c.execute("SELECT * FROM sel_products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            return None
        skus = c.execute(
            "SELECT * FROM sel_skus WHERE product_id = ? ORDER BY sku_idx", (product_id,)
        ).fetchall()
    return _row_to_record(row, skus)


def list_products_by_run(run_id: str) -> list[ProductRecord]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM sel_products WHERE run_id = ? ORDER BY fetched_at DESC", (run_id,)
        ).fetchall()
        out = []
        for r in rows:
            skus = c.execute(
                "SELECT * FROM sel_skus WHERE product_id = ? ORDER BY sku_idx", (r["id"],)
            ).fetchall()
            out.append(_row_to_record(r, skus))
    return out


def _row_to_record(row, sku_rows) -> ProductRecord:
    skus = [
        SKU(
            spec_axes=json.loads(s["spec_axes_json"]),
            price=s["price"], currency=s["currency"],
            stock_signal=s["stock_signal"],
            review_count=s["review_count"], sold_count=s["sold_count"],
            is_oversize=bool(s["is_oversize"]),
        )
        for s in sku_rows
    ]
    return ProductRecord.from_dict({
        "id": row["id"], "platform": row["platform"], "url": row["url"],
        "title": row["title"], "brand": row["brand"],
        "category_path": json.loads(row["category_path_json"] or "[]"),
        "images": json.loads(row["images_json"] or "[]"),
        "image_embeddings": json.loads(row["image_embeddings_json"] or "{}"),
        "price": json.loads(row["price_json"] or "{}"),
        "skus": [s.__dict__ for s in skus],
        "sales_signal": json.loads(row["sales_signal_json"]),
        "reviews": json.loads(row["reviews_json"] or "{}"),
        "inferred_features": json.loads(row["inferred_features_json"] or "[]"),
        "shipping": json.loads(row["shipping_json"] or "{}"),
        "policy_flags": json.loads(row["policy_flags_json"] or "{}"),
        "return_risk": json.loads(row["return_risk_json"] or "null"),
        "market_meta": json.loads(row["market_meta_json"] or "{}"),
        "fetched_at": row["fetched_at"],
        "source_path": row["source_path"] or "",
    })


# ── Run lifecycle ────────────────────────────────────────────

def start_run(*, trigger: str, keyword: Optional[str], category: Optional[str],
              markets: list[str]) -> str:
    import time, secrets
    # hex random 4 bytes 避免同秒 collision
    run_id = f"batch_{int(time.time())}_{secrets.token_hex(4)}"
    with conn() as c:
        c.execute(
            """INSERT INTO sel_runs
               (run_id, trigger, keyword, category, markets_json, started_at, status)
               VALUES (?,?,?,?,?,datetime('now','localtime'),'running')""",
            (run_id, trigger, keyword, category, json.dumps(markets, ensure_ascii=False)),
        )
    return run_id


def finish_run(run_id: str, *, status: str = "done",
               note: Optional[str] = None, cost_credits: int = 0):
    with conn() as c:
        c.execute(
            """UPDATE sel_runs
               SET status=?, note=?, cost_credits=cost_credits+?,
                   finished_at=datetime('now','localtime')
               WHERE run_id=?""",
            (status, note, cost_credits, run_id),
        )


def add_run_credits(run_id: str, n: int):
    with conn() as c:
        c.execute("UPDATE sel_runs SET cost_credits=cost_credits+? WHERE run_id=?",
                  (n, run_id))


# ── 决策 ────────────────────────────────────────────────────

def record_decision(*, run_id: Optional[str], product_id: str, stage: str, action: str,
                    reason_tags: list[str], reason_text: Optional[str] = None,
                    score: Optional[int] = None, agent_predicted: Optional[dict] = None) -> int:
    """记录用户决策. reason_tags 必填 (§8 第 3 条)."""
    if not reason_tags:
        raise ValueError("reason_tags 必填, 不能为空 (§8 第 3 条)")
    if action not in {"accept", "reject", "hold", "evaluate"}:
        raise ValueError(f"action: {action}")
    if stage not in {"candidate_review", "inquiry", "sample_qc", "post_launch"}:
        raise ValueError(f"stage: {stage}")
    with conn() as c:
        cur = c.execute(
            """INSERT INTO sel_decisions
               (run_id, product_id, stage, action, reason_tags_json,
                reason_text, score, agent_predicted_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, product_id, stage, action,
             json.dumps(reason_tags, ensure_ascii=False), reason_text, score,
             json.dumps(agent_predicted, ensure_ascii=False) if agent_predicted else None),
        )
        return cur.lastrowid


if __name__ == "__main__":
    init_db()
    print(f"[selector.db] initialized at {DB_PATH}")
