# /website/scripts — gs subdomain data pipeline

These scripts move data from the Lucknow rig's decode log into
`gs.nl06.com`, with zero AI in the loop. They run on the NAS,
typically on a nightly systemd timer.

## The chain

```
SatDump decode   →  .processed-iq-log.ndjson   (append-only, history)
                       │
                       │ build-canonical.py
                       ▼
                    .processed-iq-canonical.json   (1 entry per IQ, latest wins)
                       │
                       │ sync-plates.py
                       ▼
                    apps/gs/src/content/plates.json  +  apps/gs/public/plates/*.png
                       │
                       │ pnpm build:gs && wrangler pages deploy
                       ▼
                    https://gs.nl06.com
```

## Scripts

### `build-canonical.py`

Reads `/satellite/.processed-iq-log.ndjson` (forensic, append-only),
groups by `sha256_first_mb`, and writes
`/satellite/.processed-iq-canonical.json` where each IQ has exactly one
entry — the most recent `processed_at` wins. Adds `decode_attempts` and
a compact `decode_history` to entries whose IQ was decoded more than
once.

Also scans `/satellite/passes/` for CADU files on disk that don't
appear in NDJSON. Reports them as **forensic gaps** — these decodes
happened but were never logged.

Usage:
```
./build-canonical.py             # write canonical, warn on gaps
./build-canonical.py --check     # warn only; do not write
./build-canonical.py --verbose   # list every grouped entry
```

### `sync-plates.py`

Reads `/satellite/.processed-iq-canonical.json` and propagates new IQs
into `/website/apps/gs/src/content/plates.json`. Idempotent: an entry
already present (by sha256, or by satellite + capture-time within a
per-band window) is skipped. Hand-curated entries are never modified.

For decodes that produced imagery, the script searches:
1. `/satellite/images/<sat>/...` (post-2026-05-21 layout)
2. The NDJSON's `nas_pass_dir` field (legacy layout)
3. `/satellite/passes/<sat>/<date>/` (post-reorg mirror)

and copies the first hit to `apps/gs/public/plates/`.

Usage:
```
./sync-plates.py            # update plates.json + copy PNGs, no deploy
./sync-plates.py --deploy   # also: pnpm build:gs + wrangler deploy
./sync-plates.py --dry-run  # report what would change, write nothing
```

`--deploy` requires `~/.config/nl06/cloudflare.token` (mode 600).

## systemd installation

Copy the unit files into the user systemd directory and enable the timer:

```
mkdir -p ~/.config/systemd/user/
cp /website/scripts/systemd/*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nl06-gs-sync.timer
systemctl --user list-timers --all     # verify
```

The timer fires nightly at 03:00 IST. To run it ad-hoc:

```
systemctl --user start nl06-gs-sync.service
journalctl --user -u nl06-gs-sync.service -n 60 --no-pager
```

To keep the user instance running across logout (so the timer fires when
no one is logged in), enable lingering once:

```
loginctl enable-linger naman
```

## What's still manual

- **Decode wrapper not yet implemented.** Today, decodes are run by hand
  (per CLAUDE.md), so structured logging depends on the operator
  remembering to append to NDJSON. That's how the 6 forensic gaps
  documented in `build-canonical.py` warnings exist. The fix is a
  `decode-and-log.sh` wrapper described in CLAUDE.md, to be built when
  live decodes resume (Pi currently in Mumbai).
- **No automatic git commit.** The pipeline updates `plates.json` and
  `public/plates/` in-place and ships directly via wrangler. The git
  working tree drifts from production until you manually `git add &&
  commit && push`. That's intentional — pushing daily auto-commits to
  GitHub would just be noise.
