#!/usr/bin/env python3
"""
sync-auctionhouse.py
====================

Snapshot the auctionhouse-backend SQLite DB into the static content
files apps/auctionhouse reads at build time, optionally building and
deploying the site.

Mirrors the gs subdomain's sync-plates.py motion: SQL → JSON → pnpm
build → wrangler pages deploy. Idempotent — safe to re-run.

Reads:  /agents/auctionhouse-backend/data/auctions.db
Writes: /website/apps/auctionhouse/src/content/lots.json
        /website/apps/auctionhouse/src/content/meta.json

Usage:
    ./sync-auctionhouse.py            # write JSON, do not build/deploy
    ./sync-auctionhouse.py --deploy   # also: pnpm build:auctionhouse + wrangler
    ./sync-auctionhouse.py --dry-run  # print summary, write nothing

Publish filter (defaults; override AUCTIONHOUSE_MIN_PUBLISH_SCORE):
    (manual_override = 'boost')
        OR
    (status = 'active'
     AND score >= MIN_PUBLISH_SCORE
     AND (manual_override IS NULL OR manual_override != 'suppress'))

`--deploy` requires ~/.config/nl06/cloudflare.token (mode 600). See
/website/scripts/README.md for the corresponding gs pattern.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── paths + constants (intentionally hardcoded; this script is NAS-specific) ──

WEBSITE_ROOT  = Path("/website")
BACKEND_ROOT  = Path("/agents/auctionhouse-backend")
AUCTIONS_DB   = BACKEND_ROOT / "data" / "auctions.db"
CONTENT_DIR   = WEBSITE_ROOT / "apps" / "auctionhouse" / "src" / "content"
LOTS_JSON     = CONTENT_DIR / "lots.json"
META_JSON     = CONTENT_DIR / "meta.json"

CF_TOKEN_PATH = Path.home() / ".config" / "nl06" / "cloudflare.token"
CF_ACCOUNT_ID = "412810aff4761352d5c33b9257311f20"
CF_PROJECT    = "nl06-auctionhouse"

# ── publish filter ───────────────────────────────────────────────────────────
#
# The prompt scores 1-10 where 7+ is "worth featuring" and 4-6 is "has some
# hook but nothing strong enough to write up". The two-tier filter below
# treats those tiers differently per category:
#
#   - `arbitrage` lots are "buy material" — the story is the deal — so they
#     need the high bar (7+). A score-5 arbitrage lot isn't a deal worth
#     surfacing.
#   - `niche-instrument`, `prestige-seller`, and `curio` are "interesting
#     regardless of whether you'd bid" — the story is the object or the
#     seller. They get a lower bar.
#
# Plus a separate path: any lot from a prestige seller_category (DRDO,
# CSIR, HAL, ISRO, academic) is surfaced at score ≥ 4 even if the model
# tagged it as `arbitrage`. This catches mis-categorisation of prestige-
# provenance lots and reflects the "the seller is the story" axis.
#
# `AUCTIONHOUSE_MIN_PUBLISH_SCORE` (env) is the legacy uniform-override
# escape hatch — if set, it overrides every per-category threshold.

PUBLISH_THRESHOLDS = {
    "arbitrage":        int(os.environ.get("AUCTIONHOUSE_THRESHOLD_ARBITRAGE",        "7")),
    "niche-instrument": int(os.environ.get("AUCTIONHOUSE_THRESHOLD_NICHE",            "5")),
    "prestige-seller":  int(os.environ.get("AUCTIONHOUSE_THRESHOLD_PRESTIGE",         "4")),
    "curio":            int(os.environ.get("AUCTIONHOUSE_THRESHOLD_CURIO",            "4")),
}
PRESTIGE_SELLER_CATEGORIES = ("DRDO", "CSIR", "HAL", "ISRO", "academic")
PRESTIGE_SELLER_FLOOR      = int(os.environ.get("AUCTIONHOUSE_THRESHOLD_PRESTIGE_FLOOR", "4"))

_uniform_override = os.environ.get("AUCTIONHOUSE_MIN_PUBLISH_SCORE")
if _uniform_override is not None:
    _val = int(_uniform_override)
    PUBLISH_THRESHOLDS    = {k: _val for k in PUBLISH_THRESHOLDS}
    PRESTIGE_SELLER_FLOOR = _val


# ── DB read ──────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_published_lots(conn: sqlite3.Connection) -> list[dict]:
    """Apply the per-category + prestige-seller publish filter. See module
    header for the editorial logic. Order: boosted first, then highest
    score, then by closing soonest."""
    placeholders_prestige = ",".join("?" * len(PRESTIGE_SELLER_CATEGORIES))
    sql = f"""
        SELECT source, source_id, source_url,
               title, seller, seller_category, location,
               start_price_inr, emd_inr, close_at_utc, status,
               score, score_reason, interesting_category, confidence,
               manual_override, editor_note
          FROM lots
         WHERE (manual_override = 'boost')
            OR (
                status = 'active'
                AND (manual_override IS NULL OR manual_override != 'suppress')
                AND score IS NOT NULL
                AND (
                       (interesting_category = 'arbitrage'        AND score >= ?)
                    OR (interesting_category = 'niche-instrument' AND score >= ?)
                    OR (interesting_category = 'prestige-seller'  AND score >= ?)
                    OR (interesting_category = 'curio'            AND score >= ?)
                    OR (seller_category IN ({placeholders_prestige}) AND score >= ?)
                )
            )
         ORDER BY (manual_override = 'boost') DESC,
                  score DESC,
                  confidence DESC,
                  close_at_utc ASC
    """
    params = (
        PUBLISH_THRESHOLDS["arbitrage"],
        PUBLISH_THRESHOLDS["niche-instrument"],
        PUBLISH_THRESHOLDS["prestige-seller"],
        PUBLISH_THRESHOLDS["curio"],
        *PRESTIGE_SELLER_CATEGORIES,
        PRESTIGE_SELLER_FLOOR,
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def load_meta(conn: sqlite3.Connection, published_count: int) -> dict:
    """Snapshot of pipeline health, surfaced on the site so anyone can
    see whether yesterday's run worked without opening journalctl."""
    def scalar(sql: str, *params) -> int:
        v = conn.execute(sql, params).fetchone()[0]
        return v or 0

    total_lots     = scalar("SELECT COUNT(*) FROM lots")
    active_lots    = scalar("SELECT COUNT(*) FROM lots WHERE status='active'")
    scored_active  = scalar(
        "SELECT COUNT(*) FROM lots "
        " WHERE status='active' AND score IS NOT NULL")
    top_score      = scalar(
        "SELECT COALESCE(MAX(score), 0) FROM lots WHERE status='active'")
    by_category    = dict(conn.execute(
        "SELECT COALESCE(interesting_category, 'unscored') AS cat, COUNT(*) "
        "  FROM lots WHERE status='active' "
        " GROUP BY cat ORDER BY 2 DESC"
    ).fetchall())

    last_run = conn.execute(
        """
        SELECT source, started_at_utc, finished_at_utc, error,
               lots_seen, lots_new, parse_failures, score_calls
          FROM runs
         ORDER BY id DESC
         LIMIT 1
        """).fetchone()
    last_run_dict = dict(last_run) if last_run else None

    # 30-day rolling spend rollup. Pricing math is duplicated here (instead
    # of importing the backend module) because this script is intentionally
    # stdlib-only and runs from a different cwd. Haiku 4.5 list rates as of
    # session start; update if Anthropic changes pricing.
    USD_PER_MTOK = {"input": 1.00, "output": 5.00,
                    "cache_read": 0.10, "cache_creation": 1.25}
    spend_row = conn.execute("""
        SELECT COALESCE(SUM(input_tokens), 0)                AS in_tk,
               COALESCE(SUM(output_tokens), 0)               AS out_tk,
               COALESCE(SUM(cache_read_input_tokens), 0)     AS cache_r_tk,
               COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_c_tk,
               COUNT(*)                                      AS calls,
               SUM(origin='api')                             AS api_calls
          FROM score_audit
         WHERE scored_at_utc >= datetime('now', '-30 days')
    """).fetchone()
    usd_30d = (
        (spend_row["in_tk"]      * USD_PER_MTOK["input"]
       + spend_row["out_tk"]     * USD_PER_MTOK["output"]
       + spend_row["cache_r_tk"] * USD_PER_MTOK["cache_read"]
       + spend_row["cache_c_tk"] * USD_PER_MTOK["cache_creation"]) / 1_000_000.0
    )

    return {
        "synced_at_utc":     _utcnow_iso(),
        "publish_filter": {
            "category_thresholds":    PUBLISH_THRESHOLDS,
            "prestige_categories":    list(PRESTIGE_SELLER_CATEGORIES),
            "prestige_seller_floor":  PRESTIGE_SELLER_FLOOR,
        },
        "counts": {
            "total":             total_lots,
            "active":            active_lots,
            "scored_active":     scored_active,
            "published":         published_count,
            "top_active_score":  top_score,
        },
        "by_category": by_category,
        "last_run":    last_run_dict,
        "spend_30d": {
            "calls":           spend_row["calls"]      or 0,
            "api_calls":       spend_row["api_calls"]  or 0,
            "input_tokens":    spend_row["in_tk"]      or 0,
            "output_tokens":   spend_row["out_tk"]     or 0,
            "cache_read_tk":   spend_row["cache_r_tk"] or 0,
            "cache_create_tk": spend_row["cache_c_tk"] or 0,
            "usd":             round(usd_30d, 4),
        },
    }


# ── JSON projection ──────────────────────────────────────────────────────────


def project_lot(row: dict) -> dict:
    """Drop adapter-internal fields. Keep what the Astro page renders.
    `note` is the editor's hand-written caption when present, otherwise
    the model's score_reason — same field on the page, different
    provenance, which is fine for v1."""
    return {
        "id":               f"{row['source']}-{row['source_id']}",
        "source":           row["source"],
        "source_url":       row["source_url"],
        "title":            row["title"],
        "seller":           row["seller"],
        "seller_category":  row["seller_category"],
        "location":         row["location"],
        "start_price_inr":  row["start_price_inr"],
        "emd_inr":          row["emd_inr"],
        "close_at_utc":     row["close_at_utc"],
        "status":           row["status"],
        "score":            row["score"],
        "category":         row["interesting_category"],
        "confidence":       row["confidence"],
        "note":             row["editor_note"] or row["score_reason"],
        "note_is_editor":   bool(row["editor_note"]),
        "boosted":          row["manual_override"] == "boost",
    }


# ── deploy ───────────────────────────────────────────────────────────────────


def deploy_auctionhouse() -> int:
    """pnpm build → wrangler pages deploy. Each step is idempotent.
    Returns the subprocess return code of the failing step, or 0."""
    print("sync-auctionhouse: building apps/auctionhouse ...")
    r = subprocess.run(
        ["pnpm", "--filter", "@nl06/auctionhouse", "build"],
        cwd=WEBSITE_ROOT,
    )
    if r.returncode != 0:
        print("sync-auctionhouse: build failed", file=sys.stderr)
        return r.returncode

    if not CF_TOKEN_PATH.exists():
        print(f"sync-auctionhouse: CF token not found at {CF_TOKEN_PATH}; "
              "skipping deploy.", file=sys.stderr)
        return 2

    print(f"sync-auctionhouse: deploying {CF_PROJECT} ...")
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"]  = CF_TOKEN_PATH.read_text().strip()
    env["CLOUDFLARE_ACCOUNT_ID"] = CF_ACCOUNT_ID
    r = subprocess.run(
        [
            "pnpm", "dlx", "wrangler", "pages", "deploy",
            "apps/auctionhouse/dist",
            f"--project-name={CF_PROJECT}",
            "--branch=main",
            "--commit-dirty=true",
        ],
        cwd=WEBSITE_ROOT, env=env,
    )
    return r.returncode


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy",  action="store_true",
                        help="also build apps/auctionhouse and deploy via wrangler")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing")
    args = parser.parse_args()

    if not AUCTIONS_DB.exists():
        print(f"sync-auctionhouse: DB not found at {AUCTIONS_DB}", file=sys.stderr)
        return 1
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{AUCTIONS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows         = load_published_lots(conn)
    lots_payload = [project_lot(r) for r in rows]
    meta_payload = load_meta(conn, len(lots_payload))
    conn.close()

    thr = PUBLISH_THRESHOLDS
    print(f"sync-auctionhouse: {meta_payload['counts']['active']} active lots, "
          f"{meta_payload['counts']['scored_active']} scored; "
          f"{len(lots_payload)} clear publish filter "
          f"(arb≥{thr['arbitrage']} / niche≥{thr['niche-instrument']} / "
          f"prestige≥{thr['prestige-seller']} / curio≥{thr['curio']} / "
          f"prestige-seller≥{PRESTIGE_SELLER_FLOOR})")

    if args.dry_run:
        print("sync-auctionhouse: --dry-run; no files written, no deploy")
        return 0

    LOTS_JSON.write_text(json.dumps(lots_payload, indent=2, ensure_ascii=False) + "\n")
    META_JSON.write_text(json.dumps(meta_payload, indent=2, ensure_ascii=False) + "\n")
    print(f"sync-auctionhouse: wrote {LOTS_JSON.relative_to(WEBSITE_ROOT)} "
          f"and {META_JSON.relative_to(WEBSITE_ROOT)}")

    if args.deploy:
        return deploy_auctionhouse()
    return 0


if __name__ == "__main__":
    sys.exit(main())
