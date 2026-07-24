#!/bin/bash
# Sync the DPD Early-Warning app into the sandboxed preview mirror.
# /tmp is periodically cleaned by macOS — sometimes stripping files while
# leaving empty dirs (a gutted vendor/ breaks pymongo imports), so we probe
# vendor/pymongo/__init__.py and re-copy the whole vendor tree if it's gone.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../../../.." && pwd)"
DST=/tmp/dpd-early-warning

mkdir -p "$DST/static"

for f in app.py checker.py cycle.py db.py dbconfig.py demo_seed.py digitap.py \
         insights.py aa_report.py aa_live.py ingest_aa.py los_client.py ingest_los.py \
         lms_client.py ingest_lms.py los_consent.py los_consent.sql mock_report.py mongostore.py \
         rbac.py scheduler.py store.py userstore.py run_preview.py check_db.py lookup.sql \
         users.yaml .env; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/"
done
cp "$SRC"/static/*.html "$DST/static/"
# bundled AA sample reports (ingestion + live-pull mock replay)
mkdir -p "$DST/samples"
cp "$SRC"/samples/*.json "$DST/samples/" 2>/dev/null || true
cp "$SRC"/samples/*.csv "$DST/samples/" 2>/dev/null || true

if [ ! -f "$DST/vendor/pymongo/__init__.py" ]; then
  echo "vendor/ missing or gutted — re-copying full tree…"
  rm -rf "$DST/vendor"
  cp -R "$SRC/vendor" "$DST/vendor"
fi

echo "synced $SRC -> $DST"
echo "python files: $(ls "$DST"/*.py | wc -l | tr -d ' ') · vendor pkgs: $(ls "$DST/vendor" | wc -l | tr -d ' ')"
echo "restart the preview server (name: dpd-early-warning) to load the new code."
