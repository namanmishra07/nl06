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
               score, score_title, score_reason, interesting_category, confidence,
               manual_override, editor_note, annexure_excerpts,
               vision_condition, first_seen_utc
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
    # Exclude gate-sentinel rows (score_prompt_version='gate-v1', a score of 1
    # written for blacklist-gated junk) so "scored" means real LLM scores.
    scored_active  = scalar(
        "SELECT COUNT(*) FROM lots "
        " WHERE status='active' AND score IS NOT NULL"
        "   AND COALESCE(score_prompt_version,'') != 'gate-v1'")
    top_score      = scalar(
        "SELECT COALESCE(MAX(score), 0) FROM lots WHERE status='active'")
    by_category    = dict(conn.execute(
        "SELECT CASE WHEN score_prompt_version = 'gate-v1' THEN 'gated' "
        "            WHEN interesting_category IS NULL      THEN 'unscored' "
        "            ELSE interesting_category END AS cat, COUNT(*) "
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
        "score_title":      row.get("score_title"),
        "category":         row["interesting_category"],
        "confidence":       row["confidence"],
        "note":             row["editor_note"] or row["score_reason"],
        "note_is_editor":   bool(row["editor_note"]),
        "boosted":          row["manual_override"] == "boost",
        "annexures":        _parse_annexures(row),
        "vision_condition": row.get("vision_condition"),
        "first_seen_utc":   row.get("first_seen_utc"),
        "group_total":      row.get("group_total", 1),
        "group_more":       row.get("group_more", 0),
        "photos":           [],   # filled by extract_photos() when PDFs yield images
    }


# ── search index (ALL active lots, not just published) ──────────────────────
#
# The register surfaces ~dozens of curated lots; search spans the whole
# active database (~20k) so "boeing" finds the airframe nobody scored a 9.
# Compact parallel-array JSON, lazy-fetched by the page on first search
# focus. Snippets only for lots with extractable annexure text, truncated
# hard — the budget is "comfortably under 1 MB brotli'd".

SEARCH_JSON   = APP_ROOT / "public" / "search.json"
SNIPPET_CHARS = 110


def build_search_index(conn: sqlite3.Connection,
                       published_ids: set[str] | None = None) -> tuple[int, int]:
    """Write search.json. Returns (row_count, byte_size). Rows carry a
    trailing pub flag (1 = on the register) so the client can route a hit
    to the lot plate instead of the raw MSTC PDF."""
    published_ids = published_ids or set()
    rows = conn.execute("""
        SELECT source, source_id, title, seller, seller_category, location,
               start_price_inr, close_at_utc, score, interesting_category,
               annexure_excerpts
          FROM lots
         WHERE status = 'active'
           AND (manual_override IS NULL OR manual_override != 'suppress')
         ORDER BY score DESC NULLS LAST, close_at_utc ASC
    """).fetchall()

    out = []
    for r in rows:
        snippet = ""
        if r["annexure_excerpts"] and r["annexure_excerpts"] != "[]":
            try:
                for ax in json.loads(r["annexure_excerpts"]):
                    ex = (ax.get("excerpt") or "").strip()
                    if ex and ex != "(annexure empty)":
                        snippet = " ".join(ex.split())[:SNIPPET_CHARS]
                        break
            except (TypeError, ValueError):
                pass
        lot_id = f"{r['source']}-{r['source_id']}"
        out.append([
            lot_id,
            _auction_id(r["source"], r["source_id"]),
            r["title"],
            r["seller"] or "",
            r["location"] or "",
            r["score"] or 0,
            r["interesting_category"] or "",
            r["start_price_inr"],
            r["close_at_utc"] or "",
            snippet,
            1 if lot_id in published_ids else 0,
        ])
    payload = json.dumps({"fields": ["id", "aid", "title", "seller", "location",
                                     "score", "category", "price_inr",
                                     "close_utc", "snippet", "pub"],
                          "rows": out}, ensure_ascii=False,
                         separators=(",", ":"))
    SEARCH_JSON.write_text(payload + "\n")
    return len(out), len(payload.encode("utf-8"))


# ── spotlight selection ──────────────────────────────────────────────────────
#
# The strangest live objects, chosen from the published payload: curio /
# niche-instrument / prestige-seller lots, highest score first, one lot per
# seller so a single agency can't fill the shelf. The model's score_reason
# (already on `note`) is the caption. Exported in meta.json so the page and
# the spotlight feed agree on the same picks.

SPOTLIGHT_CATEGORIES = ("curio", "niche-instrument", "prestige-seller")
SPOTLIGHT_N          = 10


def pick_spotlight(lots: list[dict]) -> list[str]:
    """Never feature a lot whose hammer has already fallen: anything with
    a close time in the past is excluded (the page also hides a card
    client-side if its close passes during the day)."""
    now_iso = _utcnow_iso()
    picks, sellers_seen = [], set()
    pool = [l for l in lots
            if l["category"] in SPOTLIGHT_CATEGORIES and l.get("note")
            and (not l["close_at_utc"] or l["close_at_utc"] > now_iso)]
    pool.sort(key=lambda l: (-(l["score"] or 0), -(l["confidence"] or 0)))
    for l in pool:
        if l["seller"] in sellers_seen:
            continue
        sellers_seen.add(l["seller"])
        picks.append(l["id"])
        if len(picks) >= SPOTLIGHT_N:
            break
    return picks


# ── spotlight history (feeds the /spotlight-archive page) ────────────────────
#
# Every lot that ever makes the spotlight is snapshotted once, with the
# date it was featured and a private copy of its first photo (the live
# photo dirs are wiped each sync, so the archive keeps its own). The
# archive page renders from this file alone — closed lots stay readable
# long after they leave the register.

SPOTLIGHT_HISTORY     = CONTENT_DIR / "spotlight-history.json"
PHOTO_ARCHIVE_DIR     = APP_ROOT / "public" / "photos-archive"
SPOTLIGHT_HISTORY_MAX = 90


def update_spotlight_history(lots: list[dict], spotlight_ids: list[str]) -> int:
    """Append never-before-seen picks to the history snapshot. Returns the
    number of new entries. Trims to the most recent SPOTLIGHT_HISTORY_MAX
    and garbage-collects archived photos that fell off the end."""
    import shutil
    by_id = {l["id"]: l for l in lots}
    try:
        hist = json.loads(SPOTLIGHT_HISTORY.read_text())
    except (OSError, ValueError):
        hist = []
    known = {h["id"] for h in hist}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    added = 0
    for sid in spotlight_ids:
        if sid in known or sid not in by_id:
            continue
        l = by_id[sid]
        photo = None
        if l.get("photos"):
            src = APP_ROOT / "public" / l["photos"][0].lstrip("/")
            slug = _safe_slug(sid)
            try:
                PHOTO_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, PHOTO_ARCHIVE_DIR / f"{slug}.webp")
                photo = f"/photos-archive/{slug}.webp"
            except OSError:
                pass
        hist.append({
            "id":              sid,
            "spotlit_on":      today,
            "title":           l["title"],
            "seller":          l["seller"],
            "seller_category": l["seller_category"],
            "location":        l["location"],
            "start_price_inr": l["start_price_inr"],
            "score":           l["score"],
            "category":        l["category"],
            "note":            l["note"],
            "close_at_utc":    l["close_at_utc"],
            "photo":           photo,
            "auction_id":      l["auction_id"],
        })
        added += 1
    hist = hist[-SPOTLIGHT_HISTORY_MAX:]
    keep = {Path(h["photo"]).name for h in hist if h.get("photo")}
    if PHOTO_ARCHIVE_DIR.exists():
        for f in PHOTO_ARCHIVE_DIR.glob("*.webp"):
            if f.name not in keep:
                f.unlink()
    SPOTLIGHT_HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1) + "\n")
    return added


# ── feeds (RSS + JSON; static, regenerated nightly) ──────────────────────────

FEEDS_DIR = APP_ROOT / "public" / "feeds"
SITE_URL  = "https://auctionhouse.nl06.com"


def _rss_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rss_item(l: dict) -> str:
    price = l["start_price_inr"]
    price_s = f"Rs {price:,}" if price is not None else "no reserve"
    desc = (f"{l['seller'] or '?'} ({l['seller_category']}) · "
            f"{l['location'] or '?'} · {price_s} reserve · "
            f"closes {l['close_at_utc'] or '?'} · score {l['score']}/10. "
            f"{l.get('note') or ''}")
    pub = l.get("first_seen_utc") or ""
    if pub:
        pub_dt = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        pub = pub_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return (f"<item><title>{_rss_escape(l['title'])}</title>"
            f"<link>{SITE_URL}/register#lot-{_rss_escape(l['id'])}</link>"
            f"<guid isPermaLink=\"false\">{_rss_escape(l['id'])}</guid>"
            + (f"<pubDate>{pub}</pubDate>" if pub else "")
            + f"<description>{_rss_escape(desc)}</description></item>")


def _rss_doc(title: str, desc: str, items: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<rss version=\"2.0\"><channel>"
            f"<title>{_rss_escape(title)}</title>"
            f"<link>{SITE_URL}</link>"
            f"<description>{_rss_escape(desc)}</description>"
            f"<lastBuildDate>{now}</lastBuildDate>"
            + "".join(items) +
            "</channel></rss>\n")


def write_feeds(lots: list[dict], spotlight_ids: list[str]) -> None:
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    by_id = {l["id"]: l for l in lots}
    (FEEDS_DIR / "register.xml").write_text(_rss_doc(
        "auctionhouse.nl06 · register",
        "Indian government surplus auctions worth a closer look. Updated nightly.",
        [_rss_item(l) for l in lots]))
    (FEEDS_DIR / "spotlight.xml").write_text(_rss_doc(
        "auctionhouse.nl06 · spotlight",
        "The strangest live lots on the register. Updated nightly.",
        [_rss_item(by_id[i]) for i in spotlight_ids if i in by_id]))
    (FEEDS_DIR / "register.json").write_text(
        json.dumps({"generated_utc": _utcnow_iso(), "site": SITE_URL,
                    "spotlight_ids": spotlight_ids, "lots": lots},
                   ensure_ascii=False, indent=1) + "\n")


# ── photo extraction (embedded images in annexure PDFs) ──────────────────────
#
# Published lots only (~dozens), so the asset budget stays tiny: capped at
# MAX_PHOTOS_PER_LOT webp thumbnails per lot and MAX_PHOTO_FILES total per
# deploy. PDFs were already downloaded by the scraper into data/raw — this
# touches no network and makes no API calls. Pages-deploy friendly: small
# files, bounded count, stale dirs wiped each sync.

PHOTOS_DIR          = APP_ROOT / "public" / "photos"
RAW_DIR             = BACKEND_ROOT / "data" / "raw"
MAX_PHOTOS_PER_LOT  = 6
MAX_PHOTO_FILES     = 600
MIN_IMG_DIM_PX      = 240          # skip logos / signature scans
MIN_IMG_BYTES       = 12_000
PHOTO_MAX_DIM       = 1100
WEBP_QUALITY        = 72


def _safe_slug(lot_id: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "_", lot_id)


def extract_photos(lots: list[dict]) -> int:
    """Extract embedded images from each published lot's annexure PDFs into
    public/photos/<slug>/<n>.webp and fill lot['photos']. Returns total
    images written. Gracefully no-ops if PyMuPDF/Pillow are unavailable
    (the script stays runnable under system python)."""
    try:
        import fitz                      # PyMuPDF
        from PIL import Image
        import io
    except ImportError:
        print("sync-auctionhouse: PyMuPDF/Pillow unavailable; skipping photos")
        return 0

    if PHOTOS_DIR.exists():
        import shutil
        shutil.rmtree(PHOTOS_DIR)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for lot in lots:
        if total >= MAX_PHOTO_FILES:
            print(f"sync-auctionhouse: photo cap {MAX_PHOTO_FILES} hit; "
                  "remaining lots ship without photos")
            break
        if lot["source"] != "mstc" or not lot["annexures"]:
            continue
        slug   = _safe_slug(lot["id"])
        outdir = PHOTOS_DIR / slug
        wrote  = []
        seen_xrefs: set[int] = set()
        for ax in lot["annexures"]:
            if len(wrote) >= MAX_PHOTOS_PER_LOT:
                break
            pdf = RAW_DIR / "mstc" / lot["auction_id"] / Path(ax["filename"]).name
            if not pdf.is_file():
                continue
            try:
                doc = fitz.open(pdf)
            except Exception:
                continue
            try:
                for pno in range(min(doc.page_count, 12)):
                    if len(wrote) >= MAX_PHOTOS_PER_LOT:
                        break
                    for img in doc.get_page_images(pno):
                        xref = img[0]
                        if xref in seen_xrefs:
                            continue
                        seen_xrefs.add(xref)
                        try:
                            raw = doc.extract_image(xref)
                        except Exception:
                            continue
                        if len(raw["image"]) < MIN_IMG_BYTES:
                            continue
                        try:
                            im = Image.open(io.BytesIO(raw["image"]))
                            im.load()
                        except Exception:
                            continue
                        if min(im.size) < MIN_IMG_DIM_PX:
                            continue
                        if im.mode not in ("RGB", "L"):
                            im = im.convert("RGB")
                        im.thumbnail((PHOTO_MAX_DIM, PHOTO_MAX_DIM))
                        outdir.mkdir(parents=True, exist_ok=True)
                        fname = f"{len(wrote)+1}.webp"
                        im.save(outdir / fname, "WEBP", quality=WEBP_QUALITY)
                        wrote.append(f"/photos/{slug}/{fname}")
                        total += 1
                        if len(wrote) >= MAX_PHOTOS_PER_LOT or total >= MAX_PHOTO_FILES:
                            break
            finally:
                doc.close()
        lot["photos"] = wrote
    return total


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

    spotlight_ids  = pick_spotlight(lots_payload)
    meta_payload["spotlight_ids"] = spotlight_ids

    if not args.dry_run:
        search_rows, search_bytes = build_search_index(
            conn, {l["id"] for l in lots_payload})
        meta_payload["counts"]["searchable"] = search_rows
        print(f"sync-auctionhouse: search index {search_rows} lots, "
              f"{search_bytes/1e6:.1f} MB raw")
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

    n_photos = extract_photos(lots_payload)
    n_archived = update_spotlight_history(lots_payload, spotlight_ids)
    if n_archived:
        print(f"sync-auctionhouse: {n_archived} new spotlight pick(s) archived")
    LOTS_JSON.write_text(json.dumps(lots_payload, indent=2, ensure_ascii=False) + "\n")
    META_JSON.write_text(json.dumps(meta_payload, indent=2, ensure_ascii=False) + "\n")
    write_feeds(lots_payload, spotlight_ids)
    n_shims = write_catalogue_shims(lots_payload, CATALOGUE_DIR)
    print(f"sync-auctionhouse: wrote {LOTS_JSON.relative_to(WEBSITE_ROOT)}, "
          f"{META_JSON.relative_to(WEBSITE_ROOT)}, feeds (register/spotlight/json), "
          f"{n_photos} lot photos, {len(spotlight_ids)} spotlight picks, "
          f"and {n_shims} catalogue shims")

    if args.deploy:
        return deploy_auctionhouse()
    return 0


if __name__ == "__main__":
    sys.exit(main())
