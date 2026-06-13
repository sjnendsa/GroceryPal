import sqlite3
import os
import sys
from contextlib import contextmanager

_BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE, "data")
DB_PATH = os.path.join(_DATA_DIR, "grocery_pal_saveon_1982.db")


def set_store(store_id):
    """Point all subsequent DB access at the given store's database file."""
    global DB_PATH
    DB_PATH = os.path.join(_DATA_DIR, f"grocery_pal_{store_id}.db")
    init_db()
    # Builds from before the retailer-prefix naming wrote Save-On data to
    # grocery_pal_<id>.db; fold any such file in so its history isn't lost.
    if store_id.startswith("saveon_") and _merge_legacy_db(store_id.split("_", 1)[1]):
        init_db()   # re-run backfill for products whose latest_at was reset


def _merge_legacy_db(bare_id):
    """Merge a legacy grocery_pal_<id>.db (no retailer prefix) into the current
    saveon DB, then delete it. Returns True if a merge happened."""
    legacy = os.path.join(_DATA_DIR, f"grocery_pal_{bare_id}.db")
    if not os.path.exists(legacy) or os.path.abspath(legacy) == os.path.abspath(DB_PATH):
        return False
    try:
        with get_conn() as conn:
            conn.execute("ATTACH DATABASE ? AS legacy", (legacy,))
            try:
                pcols = [r[1] for r in conn.execute("PRAGMA legacy.table_info(products)")]
                hcols = [r[1] for r in conn.execute("PRAGMA legacy.table_info(price_history)")]
                if "product_id" not in pcols or "product_id" not in hcols:
                    return False
                pl = ", ".join(c for c in
                               ("product_id", "name", "brand", "category", "subcategory",
                                "image_url", "unit", "size", "url", "created_at", "updated_at")
                               if c in pcols)
                conn.execute(f"""INSERT OR IGNORE INTO products ({pl})
                                 SELECT {pl} FROM legacy.products""")
                hl = ", ".join(c for c in
                               ("product_id", "price", "regular_price", "was_price", "on_sale",
                                "sale_label", "min_qty", "in_stock", "scraped_at")
                               if c in hcols)
                conn.execute(f"""INSERT INTO price_history ({hl})
                                 SELECT {hl} FROM legacy.price_history lp
                                 WHERE NOT EXISTS (SELECT 1 FROM price_history ph
                                                   WHERE ph.product_id = lp.product_id
                                                     AND ph.scraped_at = lp.scraped_at)""")
                conn.execute("""INSERT INTO scrape_runs
                                    (started_at, completed_at, products_scraped,
                                     new_products, status, notes)
                                SELECT started_at, completed_at, products_scraped,
                                       new_products, status, notes
                                FROM legacy.scrape_runs lr
                                WHERE NOT EXISTS (SELECT 1 FROM scrape_runs sr
                                                  WHERE sr.started_at = lr.started_at)""")
                # merged rows may be newer than the denormalized snapshot —
                # clearing latest_at makes init_db's backfill recompute them
                conn.execute("""UPDATE products SET latest_at = NULL
                                WHERE COALESCE(latest_at, '') <
                                      (SELECT MAX(scraped_at) FROM price_history ph
                                       WHERE ph.product_id = products.product_id)""")
            finally:
                conn.commit()
                conn.execute("DETACH DATABASE legacy")
    except Exception as e:
        print(f"Legacy DB merge skipped ({legacy}): {e}")
        return False
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(legacy + suffix)
        except OSError:
            pass
    return True


def _ensure_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                product_id   TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                brand        TEXT,
                category     TEXT,
                subcategory  TEXT,
                image_url    TEXT,
                unit         TEXT,
                size         TEXT,
                url          TEXT,
                created_at   TEXT DEFAULT (datetime('now')),
                updated_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id    TEXT NOT NULL,
                price         REAL NOT NULL,
                regular_price REAL,
                was_price     REAL,
                on_sale       INTEGER DEFAULT 0,
                sale_label    TEXT,
                min_qty       INTEGER DEFAULT 1,
                in_stock      INTEGER DEFAULT 1,
                scraped_at    TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at       TEXT DEFAULT (datetime('now')),
                completed_at     TEXT,
                products_scraped INTEGER DEFAULT 0,
                new_products     INTEGER DEFAULT 0,
                status           TEXT DEFAULT 'running',
                notes            TEXT
            );

            CREATE TABLE IF NOT EXISTS price_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  TEXT NOT NULL,
                target_price REAL NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                triggered   INTEGER DEFAULT 0,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ph_product  ON price_history(product_id);
            CREATE INDEX IF NOT EXISTS idx_ph_scraped  ON price_history(scraped_at);
            CREATE INDEX IF NOT EXISTS idx_p_category  ON products(category);
        """)
        # Migrate existing DB: add new columns if they don't exist yet
        for col_sql in [
            "ALTER TABLE price_history ADD COLUMN was_price REAL",
            "ALTER TABLE price_history ADD COLUMN min_qty INTEGER DEFAULT 1",
            # Denormalized current/previous price (kept fresh by save_batch).
            # Readers never scan price_history — it exists for charts only.
            "ALTER TABLE products ADD COLUMN latest_price REAL",
            "ALTER TABLE products ADD COLUMN regular_price REAL",
            "ALTER TABLE products ADD COLUMN was_price REAL",
            "ALTER TABLE products ADD COLUMN on_sale INTEGER DEFAULT 0",
            "ALTER TABLE products ADD COLUMN sale_label TEXT",
            "ALTER TABLE products ADD COLUMN min_qty INTEGER DEFAULT 1",
            "ALTER TABLE products ADD COLUMN in_stock INTEGER DEFAULT 1",
            "ALTER TABLE products ADD COLUMN latest_at TEXT",
            "ALTER TABLE products ADD COLUMN prev_price REAL",
            "ALTER TABLE products ADD COLUMN prev_at TEXT",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_prod_time ON price_history(product_id, scraped_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_p_name ON products(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_p_sale ON products(on_sale)")

        # One-time backfill from price_history for DBs created before denormalization
        if conn.execute("SELECT 1 FROM products WHERE latest_at IS NULL AND product_id IN (SELECT product_id FROM price_history) LIMIT 1").fetchone():
            conn.execute("""
                UPDATE products SET
                  latest_price=(SELECT price FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),
                  regular_price=(SELECT regular_price FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),
                  was_price=(SELECT was_price FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),
                  on_sale=COALESCE((SELECT on_sale FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),0),
                  sale_label=(SELECT sale_label FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),
                  min_qty=COALESCE((SELECT min_qty FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),1),
                  in_stock=COALESCE((SELECT in_stock FROM price_history ph WHERE ph.product_id=products.product_id ORDER BY scraped_at DESC LIMIT 1),1),
                  latest_at=(SELECT MAX(scraped_at) FROM price_history ph WHERE ph.product_id=products.product_id),
                  prev_price=(SELECT price FROM price_history ph WHERE ph.product_id=products.product_id
                              AND date(scraped_at) < date((SELECT MAX(scraped_at) FROM price_history WHERE product_id=products.product_id))
                              ORDER BY scraped_at DESC LIMIT 1),
                  prev_at=(SELECT MAX(scraped_at) FROM price_history ph WHERE ph.product_id=products.product_id
                           AND date(scraped_at) < date((SELECT MAX(scraped_at) FROM price_history WHERE product_id=products.product_id)))
                WHERE latest_at IS NULL""")


# ── Products ──────────────────────────────────────────────────────────────────

def upsert_product(d: dict) -> bool:
    """Insert or update a product. Returns True if it was new."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT product_id FROM products WHERE product_id = ?", (d["product_id"],)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE products
                   SET name=?, brand=?, category=?, subcategory=?,
                       image_url=?, unit=?, size=?, url=?,
                       updated_at=datetime('now')
                   WHERE product_id=?""",
                (d.get("name"), d.get("brand"), d.get("category"), d.get("subcategory"),
                 d.get("image_url"), d.get("unit"), d.get("size"), d.get("url"),
                 d["product_id"]),
            )
            return False
        else:
            conn.execute(
                """INSERT INTO products
                       (product_id, name, brand, category, subcategory,
                        image_url, unit, size, url)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (d["product_id"], d.get("name"), d.get("brand"), d.get("category"),
                 d.get("subcategory"), d.get("image_url"), d.get("unit"),
                 d.get("size"), d.get("url")),
            )
            return True


def record_price(product_id, price, regular_price=None, on_sale=False,
                 was_price=None, sale_label=None, min_qty=1, in_stock=True):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO price_history
                   (product_id, price, regular_price, was_price, on_sale, sale_label, min_qty, in_stock)
               VALUES (?,?,?,?,?,?,?,?)""",
            (product_id, price, regular_price, was_price,
             1 if on_sale else 0, sale_label, min_qty or 1,
             1 if in_stock else 0),
        )


def get_all_product_ids():
    with get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT product_id FROM products")}


def save_batch(rows):
    """Bulk upsert products and append price history in ONE transaction.
    rows: list of (product_dict, price_dict) tuples.
    Orders of magnitude faster than per-product upsert_product/record_price."""
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO products
                   (product_id, name, brand, category, subcategory,
                    image_url, unit, size, url,
                    latest_price, regular_price, was_price, on_sale,
                    sale_label, min_qty, in_stock, latest_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(product_id) DO UPDATE SET
                   name=excluded.name, brand=excluded.brand,
                   category=excluded.category, subcategory=excluded.subcategory,
                   image_url=excluded.image_url, unit=excluded.unit,
                   size=excluded.size, url=excluded.url,
                   -- new calendar day: yesterday's price becomes "previous"
                   prev_price=CASE WHEN latest_at IS NOT NULL AND date(latest_at) < date('now')
                                   THEN latest_price ELSE prev_price END,
                   prev_at=CASE WHEN latest_at IS NOT NULL AND date(latest_at) < date('now')
                                THEN latest_at ELSE prev_at END,
                   latest_price=excluded.latest_price, regular_price=excluded.regular_price,
                   was_price=excluded.was_price, on_sale=excluded.on_sale,
                   sale_label=excluded.sale_label, min_qty=excluded.min_qty,
                   in_stock=excluded.in_stock, latest_at=excluded.latest_at,
                   updated_at=datetime('now')""",
            [(p["product_id"], p.get("name"), p.get("brand"), p.get("category"),
              p.get("subcategory"), p.get("image_url"), p.get("unit"),
              p.get("size"), p.get("url"),
              pr["price"], pr.get("regular_price"), pr.get("was_price"),
              1 if pr.get("on_sale") else 0, pr.get("sale_label"),
              pr.get("min_qty") or 1, 1 if pr.get("in_stock", True) else 0)
             for p, pr in rows],
        )
        conn.executemany(
            """INSERT INTO price_history
                   (product_id, price, regular_price, was_price,
                    on_sale, sale_label, min_qty, in_stock)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(p["product_id"], pr["price"], pr.get("regular_price"), pr.get("was_price"),
              1 if pr.get("on_sale") else 0, pr.get("sale_label"), pr.get("min_qty") or 1,
              1 if pr.get("in_stock", True) else 0) for p, pr in rows],
        )


def get_products(search=None, category=None, on_sale=False,
                 sort="name", page=1, per_page=48):
    with get_conn() as conn:
        clauses = ["1=1"]
        params = []

        if search:
            clauses.append("(p.name LIKE ? OR p.brand LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if category:
            clauses.append("p.category = ?")
            params.append(category)
        if on_sale:
            clauses.append("p.on_sale = 1")

        order = {
            "name": "p.name ASC",
            "price_asc": "p.latest_price ASC",
            "price_desc": "p.latest_price DESC",
            "newest": "p.created_at DESC",
        }.get(sort, "p.name ASC")

        where = " AND ".join(clauses)
        offset = (page - 1) * per_page

        rows = conn.execute(
            f"""SELECT p.*, p.latest_at AS last_seen
                FROM products p
                WHERE {where}
                ORDER BY {order}
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM products p WHERE {where}", params
        ).fetchone()[0]

        return [dict(r) for r in rows], total


def get_product(product_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        return dict(row) if row else None


def get_price_history(product_id):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT price, regular_price, on_sale, sale_label, in_stock, scraped_at
               FROM price_history
               WHERE product_id = ?
               ORDER BY scraped_at ASC""",
            (product_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_categories():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS count FROM products WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        on_sale = conn.execute("SELECT COUNT(*) FROM products WHERE on_sale = 1").fetchone()[0]
        categories = conn.execute(
            "SELECT COUNT(DISTINCT category) FROM products WHERE category IS NOT NULL"
        ).fetchone()[0]
        avg_price = conn.execute("SELECT AVG(latest_price) FROM products").fetchone()[0]
        last_run = conn.execute(
            "SELECT completed_at, products_scraped, status FROM scrape_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total_products": total,
            "on_sale": on_sale,
            "categories": categories,
            "avg_price": round(avg_price, 2) if avg_price else None,
            "last_run": dict(last_run) if last_run else None,
        }


def get_sales(page=1, per_page=48):
    with get_conn() as conn:
        offset = (page - 1) * per_page
        rows = conn.execute(
            """SELECT p.*, p.latest_price AS price, p.latest_at AS last_seen,
                      ROUND((p.regular_price - p.latest_price) / p.regular_price * 100, 1) AS discount_pct
               FROM products p
               WHERE p.on_sale = 1
               ORDER BY discount_pct DESC
               LIMIT ? OFFSET ?""",
            (per_page, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM products WHERE on_sale = 1"
        ).fetchone()[0]
        return [dict(r) for r in rows], total


def get_top_price_drops(limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.*, p.latest_price AS current_price,
                      ROUND((p.prev_price - p.latest_price) / p.prev_price * 100, 1) AS drop_pct
               FROM products p
               WHERE p.prev_price > p.latest_price
               ORDER BY drop_pct DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Scrape runs ───────────────────────────────────────────────────────────────

def create_run():
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO scrape_runs DEFAULT VALUES")
        return cur.lastrowid


def complete_run(run_id, products_scraped, new_products, status="completed", notes=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scrape_runs
               SET completed_at=datetime('now'), products_scraped=?, new_products=?,
                   status=?, notes=?
               WHERE id=?""",
            (products_scraped, new_products, status, notes, run_id),
        )


def get_runs(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Price alerts ──────────────────────────────────────────────────────────────

def get_alerts():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT a.*, p.name, p.brand, p.image_url,
                      p.latest_price AS current_price
               FROM price_alerts a
               JOIN products p ON p.product_id = a.product_id
               ORDER BY a.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def add_alert(product_id, target_price):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_alerts (product_id, target_price) VALUES (?,?)",
            (product_id, target_price),
        )


def delete_alert(alert_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM price_alerts WHERE id=?", (alert_id,))


def check_alerts():
    """Returns list of triggered alerts (current price <= target)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT a.*, p.name, p.latest_price AS current_price
               FROM price_alerts a
               JOIN products p ON p.product_id = a.product_id
               WHERE p.latest_price <= a.target_price AND a.triggered = 0"""
        ).fetchall()
        triggered = [dict(r) for r in rows]
        for alert in triggered:
            conn.execute("UPDATE price_alerts SET triggered=1 WHERE id=?", (alert["id"],))
        return triggered


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
