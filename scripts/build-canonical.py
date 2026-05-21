#!/usr/bin/env python3
"""
build-canonical.py — project the append-only NDJSON forensic log into
a canonical "one entry per IQ" state file.

Reads:  /satellite/.processed-iq-log.ndjson         (history, append-only)
Writes: /satellite/.processed-iq-canonical.json     (latest state, upsert)

Grouping is by sha256_first_mb. Within each group, the entry with the
latest processed_at wins; all other fields are taken from that entry.
A new field `decode_attempts` records how many NDJSON lines map to
this IQ.

Also scans /satellite/passes/ for decoded CADU artefacts and warns
about any that don't appear in NDJSON. That catches the failure mode
where the operator decoded an IQ but forgot to write a log line.

Idempotent. No LLM. Pure reduce-over-history.

Usage:
  ./build-canonical.py                # write canonical, warn on gaps
  ./build-canonical.py --check        # warn only; do not write
  ./build-canonical.py --verbose      # list every grouped entry
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NDJSON       = Path("/satellite/.processed-iq-log.ndjson")
CANONICAL    = Path("/satellite/.processed-iq-canonical.json")
PASSES_ROOT  = Path("/satellite/passes")

def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None

def load_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"build-canonical: NDJSON not found: {path}")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"build-canonical: warn: malformed line {n}: {e}", file=sys.stderr)
    return rows

def canonicalise(rows: list[dict]) -> list[dict]:
    """Group NDJSON entries by sha256_first_mb. Within each group, the
    entry with the latest processed_at wins; we add decode_attempts to
    record how many NDJSON lines mapped to this IQ. Entries without
    sha256_first_mb fall back to (pi_filename, aos_iso) as the key so
    legacy records still survive."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        sha = row.get("sha256_first_mb")
        if sha:
            key = f"sha:{sha}"
        else:
            key = f"fn:{row.get('pi_filename', '?')}@{row.get('aos_iso', row.get('pi_mtime_iso', '?'))}"
        groups[key].append(row)

    out: list[dict] = []
    for key, group in groups.items():
        group.sort(
            key=lambda r: parse_iso(r.get("processed_at", "")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        latest = dict(group[0])  # copy
        latest["decode_attempts"] = len(group)

        # If the group has more than one attempt, surface a compact
        # history so the reader can see what changed.
        if len(group) > 1:
            history: list[dict[str, Any]] = []
            for r in group:
                history.append({
                    "processed_at": r.get("processed_at"),
                    "decode_outcome": r.get("decode_outcome"),
                    "cadu_bytes": r.get("cadu_bytes"),
                })
            latest["decode_history"] = history

        out.append(latest)

    # Sort canonical by AOS (or pi_mtime) desc — most recent capture first.
    def cap_key(e: dict) -> datetime:
        dt = parse_iso(e.get("aos_iso", "")) or parse_iso(e.get("pi_mtime_iso", "")) or datetime.min.replace(tzinfo=timezone.utc)
        return dt
    out.sort(key=cap_key, reverse=True)
    return out

def scan_passes_for_gaps(canonical: list[dict]) -> list[Path]:
    """Find decoded CADUs on disk that don't appear in canonical.json.

    Walks /satellite/passes/<sat>/<date>/ looking for meteor_m2-x_lrpt.cadu
    or other pipeline outputs (>0 bytes). Compares against canonical
    nas_pass_dir + decode_history. Returns the paths that look orphan."""
    if not PASSES_ROOT.exists():
        return []

    # Build a set of "known" pass dirs from canonical (including history).
    # The 2026-05-21 reorg moved /satellite/<sat>/<date> →
    # /satellite/passes/<sat>/<date>, so register both forms for each
    # nas_pass_dir referenced in NDJSON.
    known_dirs: set[str] = set()
    legacy_re = re.compile(r"^/satellite/(meteor-m2-[34]|gk-2a|elektro-l[35]|fengyun-2h)/(.+)$")
    for c in canonical:
        d = c.get("nas_pass_dir", "")
        if not d:
            continue
        d = d.rstrip("/")
        known_dirs.add(d)
        m = legacy_re.match(d)
        if m:
            known_dirs.add(str(PASSES_ROOT / m.group(1) / m.group(2)).rstrip("/"))
        # And the reverse — if NDJSON already has the new form, register the legacy form too.
        if d.startswith("/satellite/passes/"):
            tail = d[len("/satellite/passes/"):]
            known_dirs.add(f"/satellite/{tail}")

    suspect: list[Path] = []
    for cadu in PASSES_ROOT.rglob("*.cadu"):
        if cadu.stat().st_size < 1024:
            continue
        parent = str(cadu.parent).rstrip("/")
        # If this CADU lives in a known pass dir OR a subdir of one, it's
        # accounted for IF the parent is recorded. _redecode_* subdirs are
        # NOT recorded — they're the gap we're hunting.
        recorded = False
        for known in known_dirs:
            if parent == known:
                recorded = True
                break
        if not recorded:
            suspect.append(cadu)
    return suspect

def main(argv: list[str]) -> int:
    rows = load_ndjson(NDJSON)
    canonical = canonicalise(rows)

    print(f"build-canonical: {len(rows)} NDJSON lines → {len(canonical)} canonical entries.")
    dup_groups = sum(1 for c in canonical if c.get("decode_attempts", 1) > 1)
    if dup_groups:
        print(f"build-canonical: {dup_groups} IQ(s) have multiple decode attempts; "
              f"latest processed_at wins.")
        if "--verbose" in argv:
            for c in canonical:
                if c.get("decode_attempts", 1) > 1:
                    sha = (c.get("sha256_first_mb") or "")[:12]
                    print(f"  {sha}…  {c.get('pi_filename')}  ×{c['decode_attempts']}  "
                          f"now {c.get('decode_outcome')} {c.get('cadu_bytes', 0)} B")

    gaps = scan_passes_for_gaps(canonical)
    if gaps:
        print(f"\nbuild-canonical: WARNING — {len(gaps)} CADU file(s) on disk are not "
              f"represented in NDJSON. The decode happened but was never logged:")
        for g in gaps:
            sz = g.stat().st_size
            mtime = datetime.fromtimestamp(g.stat().st_mtime, tz=timezone.utc).isoformat()
            print(f"  {g}  ({sz} B, mtime {mtime})")
        print("\nThese SHOULD be back-filled into NDJSON to keep the audit trail honest.")
        print("Use append-log.py (or hand-jq) to add a structured entry per missing decode.")

    if "--check" in argv:
        print("\nbuild-canonical: --check; canonical.json not written.")
        return 0

    with CANONICAL.open("w", encoding="utf-8") as f:
        json.dump(canonical, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nbuild-canonical: wrote {CANONICAL} ({len(canonical)} entries).")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
