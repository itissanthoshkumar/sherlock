"""Load consents into the Sherlock registry from an Excel/CSV file — the "start with
a spreadsheet" path, used until the live LOS consent DB query is wired.

    python ingest_consents_file.py consents.xlsx --dry-run        # preview the mapping, write nothing
    python ingest_consents_file.py consents.xlsx                  # upsert into consent_manager (source=LOS)
    python ingest_consents_file.py consents.csv --source SHERLOCK # legacy/manually-procured consents

Reads .xlsx (parsed natively — no openpyxl needed) or .csv. Column names are matched
DEFENSIVELY (the same aliases the LOS DB sync uses), so the sheet's headers don't have
to be exact. Every row needs a loan / LMS-account id column. Idempotent: one row per
(loan_id, consent_id); re-running updates in place. Respects MONGO_MOCK (writes to
whatever DB the app is pointed at — normally live Atlas, which is the point here).
"""
import argparse
import csv
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
LOAN_ALIASES = ["loan_id", "account_id", "loan_account_id", "lms_account_id", "lms_loan_id",
                "account id", "loan account id", "lms account id", "loan no", "account no",
                "accountnumber", "loanid", "accountid"]


# ---------------------------------------------------------------------------
# File readers -> list[dict] (header -> cell value), first row = header
# ---------------------------------------------------------------------------
def _col_to_idx(ref):  # "B7" -> 1 (0-based column)
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def read_xlsx(path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    # first worksheet by workbook order (fallback to sheet1.xml)
    sheet_path = "xl/worksheets/sheet1.xml"
    sheets = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if sheets:
        sheet_path = sheets[0]
    root = ET.fromstring(z.read(sheet_path))
    rows = []
    for row in root.iter(f"{_NS}row"):
        cells = {}
        maxc = -1
        for c in row.findall(f"{_NS}c"):
            idx = _col_to_idx(c.get("r", "A1"))
            maxc = max(maxc, idx)
            t = c.get("t")
            v = c.find(f"{_NS}v")
            if t == "s" and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
            elif t == "inlineStr":
                isn = c.find(f"{_NS}is")
                val = "".join(x.text or "" for x in isn.iter(f"{_NS}t")) if isn is not None else ""
            else:
                val = v.text if v is not None else ""
            cells[idx] = val
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
    if not rows:
        return []
    header = [str(h or "").strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue  # skip blank rows
        out.append({header[i]: (r[i] if i < len(r) else "") for i in range(len(header))})
    return out


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    if ext in (".csv", ".txt"):
        return read_csv(path)
    raise SystemExit(f"Unsupported file type '{ext}' — give a .xlsx or .csv")


def _norm_row(row):
    """Lowercase + spaces/hyphens -> underscores, so spreadsheet headers like 'Main Txn ID'
    or 'Consent Type' match the underscore aliases the mapper expects."""
    out = {}
    for k, v in row.items():
        nk = (k or "").strip().lower().replace(" ", "_").replace("-", "_")
        while "__" in nk:
            nk = nk.replace("__", "_")
        out[nk] = v
    return out


def _loan_of(nrow):
    for a in LOAN_ALIASES:
        v = nrow.get(a.replace(" ", "_"))
        if v not in (None, ""):
            return str(v).strip()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--source", default="LOS", help="LOS | SHERLOCK | MANUAL (default LOS)")
    ap.add_argument("--dry-run", action="store_true", help="preview mapping, write nothing")
    ap.add_argument("--limit", type=int, default=8, help="rows to show in the preview")
    a = ap.parse_args()

    rows = read_any(a.path)
    if not rows:
        raise SystemExit("No data rows found in the file.")
    print(f"Read {len(rows)} row(s). Detected columns: {', '.join(list(rows[0].keys()))}")

    import los_consent  # reuse the exact same defensive column mapper as the DB sync
    import mongostore as store

    mapped, skipped = [], 0
    for row in rows:
        nrow = _norm_row(row)
        loan = _loan_of(nrow)
        if not loan:
            skipped += 1
            continue
        c = los_consent._map_consent_row(loan, nrow)
        mapped.append(c)

    print(f"Mapped {len(mapped)} consent row(s); {skipped} row(s) had no loan/account id.")
    print("\nPreview (first %d):" % min(a.limit, len(mapped)))
    for c in mapped[:a.limit]:
        print(f"  loan={c['loan_id']:<16} type={c['consent_type']:<8} txn={c['main_txn_id'] or '—':<12} "
              f"status={c['status']:<8} start={c['start_date'] or '—'} exp={c['expiry'] or c['end_date'] or '—'}")

    missing_txn = sum(1 for c in mapped if c["consent_type"] == "PERIODIC" and not c["main_txn_id"])
    if missing_txn:
        print(f"\n⚠ {missing_txn} PERIODIC row(s) have NO main_txn_id — those stay NOT_PULLABLE "
              f"(a periodic AA pull needs the mandate/parent txn id).")

    if a.dry_run:
        print("\n[dry-run] nothing written. Re-run without --dry-run to upsert into consent_manager.")
        return

    n = 0
    for c in mapped:
        store.upsert_cm_consent(
            c["loan_id"], main_txn_id=c["main_txn_id"], consent_id=c["consent_id"],
            status=c["status"], expiry=c["expiry"], source=a.source.upper(),
            customer_name=c["customer_name"], mobile=c["mobile"], by="excel-import",
            consent_type=c["consent_type"], start_date=c["start_date"], end_date=c["end_date"])
        n += 1
    print(f"\n✓ Upserted {n} consent(s) into consent_manager (source={a.source.upper()}).")
    print("  Check the Consents tab / pre-flight — eligible borrowers should now appear.")


if __name__ == "__main__":
    main()
