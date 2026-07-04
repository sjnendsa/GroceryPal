"""Gentle overnight nutrition backfill + publish.

Fetches whatever the caches are still missing at a rate Loblaw's detail
endpoint tolerates (4 workers + jitter), then re-exports docs/ and pushes.
Safe to re-run: an already-complete cache fetches nothing and pushes nothing.

Run manually or via the one-shot cron entry:
    .venv/bin/python nightly_backfill.py
"""
import subprocess
import sys

import scraper  # noqa: F401  (configures logging)
import nutrition


def main():
    fetched = 0
    # nofrills first: its block cools slowest, so give it the earliest requests
    for retailer in ("nofrills", "superstore", "saveon"):
        store = nutrition.a_store_of(retailer)
        if not store:
            continue
        ids = sorted(nutrition.union_product_ids(retailer))
        print(f"=== {retailer}: {len(ids)} products ===", flush=True)
        fetched += nutrition.backfill(retailer, store, ids,
                                      budget=50000, workers=4, delay=0.15)
    print(f"fetched {fetched}", flush=True)
    if not fetched:
        return

    run = lambda *cmd: subprocess.run(cmd, check=True)
    run(sys.executable, "export_static.py")
    run("git", "add", "data", "docs")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode:
        run("git", "commit", "-m", "Nutrition backfill top-up")
        run("git", "pull", "--rebase", "origin", "main")
        run("git", "push", "origin", "main")
        print("pushed", flush=True)


if __name__ == "__main__":
    main()
