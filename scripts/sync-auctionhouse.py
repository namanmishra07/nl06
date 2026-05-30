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
from collections import Counter
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── paths + constants (intentionally hardcoded; this script is NAS-specific) ──

WEBSITE_ROOT  = Path("/website")
BACKEND_ROOT  = Path("/agents/auctionhouse-backend")
AUCTIONS_DB   = BACKEND_ROOT / "data" / "auctions.db"
APP_ROOT      = WEBSITE_ROOT / "apps" / "auctionhouse"
CONTENT_DIR   = APP_ROOT / "src" / "content"
LOTS_JSON     = CONTENT_DIR / "lots.json"
META_JSON     = CONTENT_DIR / "meta.json"
CATALOGUE_DIR = APP_ROOT / "public" / "catalogue"

CF_TOKEN_PATH = Path.home() / ".config" / "nl06" / "cloudflare.token"
CF_ACCOUNT_ID = "412810aff4761352d5c33b9257311f20"
CF_PROJECT    = "nl06-auctionhouse"

# MSTC has no GET endpoint for catalogue PDFs — only POST to
# auction_detailed_report_pdf.jsp. We generate a one-line static HTML
# shim per distinct auction at public/catalogue/<aid>.html that
# auto-POSTs in the visitor's browser, so the title link lands on the
# real PDF without us republishing it.
MSTC_DETAIL_PDF_URL = "https://www.mstcecommerce.com/auctionhome/mstc/auction_detailed_report_pdf.jsp"
MSTC_ANNEX_URL_TPL  = ("https://www.mstcecommerce.com/auctionhome/mstc/admin/upload/"
                      "downAttachedFiles.jsp?FILE_ID={filename}&doc_type=attached_annex")

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

# Max lots surfaced per (seller, interesting_category). A single high-volume
# seller disposing of one consumable across dozens of near-identical lots (an
# ordnance factory's webbing curios, say) would otherwise dominate the register
# and make the scoring look undiscriminating. 0 disables the cap.
MAX_PER_SELLER_CATEGORY = int(os.environ.get("AUCTIONHOUSE_MAX_PER_SELLER_CATEGORY", "3"))


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
               manual_override, editor_note, annexure_excerpts,
               vision_condition
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


# ── dedupe + annexure helpers ────────────────────────────────────────────────


def _auction_id(source: str, source_id: str) -> str:
    """MSTC source_id is '<auction_id>-<sublot>/<seller-path>'; the auction_id
    is everything before the first dash. Unknown sources fall back to the
    full source_id (each lot becomes its own group)."""
    if source == "mstc":
        return source_id.split("-", 1)[0]
    return source_id


def dedupe_sublots(rows: list[dict]) -> list[dict]:
    """Collapse rows sharing (source, auction_id, title) into one. The input
    is already ordered by the publish-filter SQL (boosted, score, confidence,
    close), so the first occurrence per group is the natural representative.
    Adds `sublot_count` and `auction_id` to each emitted row."""
    seen: dict[tuple, dict] = {}
    out: list[dict] = []
    for r in rows:
        aid = _auction_id(r["source"], r["source_id"])
        key = (r["source"], aid, r["title"])
        if key in seen:
            seen[key]["sublot_count"] += 1
            continue
        rep = dict(r, sublot_count=1, auction_id=aid)
        seen[key] = rep
        out.append(rep)
    return out


def collapse_seller_category(rows: list[dict], cap: int) -> tuple[list[dict], int]:
    """Cap the number of lots shown per (seller, interesting_category) group so
    one prolific seller's near-identical lots can't flood the register. Input is
    already score-ordered, so the kept representatives are the strongest. Each
    kept row gets `group_total`; the last kept rep of an over-cap group also gets
    `group_more` (how many were collapsed) so the page can surface it — nothing
    is dropped silently. Returns (kept_rows, dropped_count)."""
    if cap <= 0:
        return rows, 0
    totals: Counter = Counter((r["seller"], r["interesting_category"]) for r in rows)
    kept: list[dict] = []
    kept_n: Counter = Counter()
    last_idx: dict[tuple, int] = {}
    dropped = 0
    for r in rows:
        key = (r["seller"], r["interesting_category"])
        if kept_n[key] >= cap:
            dropped += 1
            continue
        kept_n[key] += 1
        kept.append(dict(r, group_total=totals[key]))
        last_idx[key] = len(kept) - 1
    for key, idx in last_idx.items():
        kept[idx]["group_more"] = totals[key] - kept_n[key]
    return kept, dropped


def _parse_annexures(row: dict) -> list[dict]:
    """Read annexure_excerpts JSON, return [{filename, url, excerpt_preview}].
    URL is reconstructed from the canonical MSTC template — the adapter
    doesn't persist it but the filename uniquely keys the endpoint."""
    raw = row.get("annexure_excerpts")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for item in items or []:
        fn = item.get("filename")
        if not fn:
            continue
        url = item.get("url") or MSTC_ANNEX_URL_TPL.format(filename=fn)
        excerpt = (item.get("excerpt") or "").strip()
        empty = excerpt in ("", "(annexure empty)")
        out.append({
            "filename": fn,
            "url":      url,
            "has_text": not empty,
        })
    return out


# ── JSON projection ──────────────────────────────────────────────────────────


def project_lot(row: dict) -> dict:
    """Drop adapter-internal fields. Keep what the Astro page renders.
    `note` is the editor's hand-written caption when present, otherwise
    the model's score_reason — same field on the page, different
    provenance, which is fine for v1.

    `source_url` is the in-site shim that auto-POSTs to MSTC for the
    catalogue PDF; MSTC's deep-link (a generic forthcoming-auctions
    page with a fragment anchor) is preserved on `mstc_listing_url`
    as the search fallback."""
    aid = row.get("auction_id") or _auction_id(row["source"], row["source_id"])
    return {
        "id":               f"{row['source']}-{row['source_id']}",
        "source":           row["source"],
        "source_url":       f"/catalogue/{aid}.html" if row["source"] == "mstc"
                              else row["source_url"],
        "mstc_listing_url": row["source_url"],
        "auction_id":       aid,
        "sublot_count":     row.get("sublot_count", 1),
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
        "annexures":        _parse_annexures(row),
        "vision_condition": row.get("vision_condition"),
        "group_total":      row.get("group_total", 1),
        "group_more":       row.get("group_more", 0),
    }


# ── catalogue shim writer ────────────────────────────────────────────────────


_SHIM_TPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MSTC auction {aid} — catalogue PDF</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<style>
  body {{ font-family: ui-serif, Georgia, serif; max-width: 36rem;
         margin: 4rem auto; padding: 0 1rem; color: #1a1814;
         background: #f7f3ec; }}
  h1 {{ font-family: ui-sans-serif, system-ui, sans-serif;
        font-size: .72rem; text-transform: uppercase; letter-spacing: .14em;
        color: #7a6f5e; margin: 0 0 1.5em; font-weight: 600; }}
  p {{ line-height: 1.55; font-size: 1rem; }}
  code {{ font-family: ui-monospace, monospace; background: #ece4d2;
          padding: .1em .35em; }}
  .row {{ margin-top: 2rem; padding-top: 1rem;
          border-top: 1px solid #d6cdb8; font-size: .9rem;
          color: #5b5346; display: flex; gap: 1.2em; align-items: center; }}
  button, a.btn {{ font: inherit; padding: .45rem .9rem; cursor: pointer;
            background: #1a1814; color: #f7f3ec; border: 1px solid #1a1814;
            text-decoration: none; }}
  a {{ color: #6b3e1a; }}
</style>
</head>
<body>
<h1>auctionhouse.nl06 · catalogue handoff</h1>
<p>Loading the catalogue PDF for MSTC auction <code>{aid}</code> from
<code>mstcecommerce.com</code>&hellip; The PDF is served by MSTC over a POST
endpoint, so this page hands the request off to your browser.</p>
<form id="f" method="post" action="{detail_url}" target="_self">
  <input type="hidden" name="auc" value="{aid}">
</form>
<div class="row">
  <button onclick="document.getElementById('f').submit()">Open PDF</button>
  <a href="https://www.mstcindia.co.in/">mstcindia.co.in</a>
</div>
<script>setTimeout(function(){{document.getElementById('f').submit();}}, 60);</script>
</body>
</html>
"""


def write_catalogue_shims(lots: list[dict], dest: Path) -> int:
    """One static HTML per distinct MSTC auction. The Astro page links to
    /catalogue/<aid>.html; the shim auto-submits a POST to MSTC's PDF
    endpoint so the visitor lands on the real PDF without us hosting it.
    Returns the number of files written. Removes stale shims first so
    we don't leak files for auctions that have aged out."""
    dest.mkdir(parents=True, exist_ok=True)
    for existing in dest.glob("*.html"):
        existing.unlink()
    aids = {l["auction_id"] for l in lots if l["source"] == "mstc"}
    for aid in sorted(aids):
        (dest / f"{aid}.html").write_text(
            _SHIM_TPL.format(aid=aid, detail_url=MSTC_DETAIL_PDF_URL)
        )
    return len(aids)


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

    rows           = load_published_lots(conn)
    raw_count      = len(rows)
    deduped_rows   = dedupe_sublots(rows)
    collapsed_rows, collapsed_dropped = collapse_seller_category(
        deduped_rows, MAX_PER_SELLER_CATEGORY)
    lots_payload   = [project_lot(r) for r in collapsed_rows]
    meta_payload   = load_meta(conn, len(lots_payload))
    meta_payload["counts"]["published_sublots"]        = raw_count
    meta_payload["counts"]["collapsed_seller_category"] = collapsed_dropped
    meta_payload["publish_filter"]["max_per_seller_category"] = MAX_PER_SELLER_CATEGORY
    conn.close()

    thr = PUBLISH_THRESHOLDS
    print(f"sync-auctionhouse: {meta_payload['counts']['active']} active lots, "
          f"{meta_payload['counts']['scored_active']} scored; "
          f"{raw_count} clear publish filter → {len(deduped_rows)} after sublot dedupe "
          f"→ {len(lots_payload)} after seller/category collapse "
          f"(cap {MAX_PER_SELLER_CATEGORY}, dropped {collapsed_dropped}) "
          f"(arb≥{thr['arbitrage']} / niche≥{thr['niche-instrument']} / "
          f"prestige≥{thr['prestige-seller']} / curio≥{thr['curio']} / "
          f"prestige-seller≥{PRESTIGE_SELLER_FLOOR})")

    if args.dry_run:
        print("sync-auctionhouse: --dry-run; no files written, no deploy")
        return 0

    LOTS_JSON.write_text(json.dumps(lots_payload, indent=2, ensure_ascii=False) + "\n")
    META_JSON.write_text(json.dumps(meta_payload, indent=2, ensure_ascii=False) + "\n")
    n_shims = write_catalogue_shims(lots_payload, CATALOGUE_DIR)
    print(f"sync-auctionhouse: wrote {LOTS_JSON.relative_to(WEBSITE_ROOT)}, "
          f"{META_JSON.relative_to(WEBSITE_ROOT)}, "
          f"and {n_shims} catalogue shims")

    if args.deploy:
        return deploy_auctionhouse()
    return 0


if __name__ == "__main__":
    sys.exit(main())
