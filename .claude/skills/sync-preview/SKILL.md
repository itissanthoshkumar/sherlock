---
name: sync-preview
description: Sync the DPD Early-Warning app into its sandboxed preview mirror at /tmp/dpd-early-warning and repair the vendored dependencies. Use this whenever code under dpd-early-warning/ has changed and the browser preview must reflect it, whenever the preview server fails to start or throws import errors (e.g. "cannot import name 'MongoClient' from 'pymongo' (unknown location)", ModuleNotFoundError), and whenever /tmp/dpd-early-warning looks wiped or half-empty. Always run this BEFORE debugging any preview startup failure — the mirror being stale or gutted is the most common cause.
---

# Sync the preview mirror

The preview server cannot read `~/Documents` (macOS TCC), so the app runs from a
mirror at `/tmp/dpd-early-warning` (see `launch.json` → `run_preview.py`, port 8001).
macOS periodically cleans `/tmp` and sometimes strips only the *files*, leaving
empty directories behind — a gutted `vendor/pymongo/` produces the confusing
"unknown location" import error because Python treats the empty folder as a
namespace package.

## Steps

1. Run the sync script (idempotent, safe to run any time):

   ```bash
   bash "$(dirname_of_this_skill)/scripts/sync.sh"
   ```

   (Resolve the path relative to this SKILL.md: `scripts/sync.sh` next to it.)

   It copies every app file + `static/` + `.env`, and re-copies `vendor/` only
   when `vendor/pymongo/__init__.py` is missing — the cheap probe for a gutted tree.

2. Restart the preview server (`preview_stop` then `preview_start` with server
   name `dpd-early-warning`) — a running uvicorn keeps old code in memory.

3. Confirm startup: server log should reach "Application startup complete" and
   `GET /api/health` should return `connected: true`.

## Notes

- Any file changed later must be re-synced; the mirror never updates itself.
- If a cycle was mid-run when the server died, it stays stuck in RUNNING —
  see the `run-cycle` skill for recovery.
