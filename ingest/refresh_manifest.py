"""Refresh manifest rows whose PMC files changed after the manifest was frozen.

PMC sometimes reprocesses an article (same version, new XML, so a new MD5) or replaces
a version folder (the old key disappears). ingest/download.py then reports "MD5 mismatch"
or "NoSuchKey" for those articles. This script re-reads each one's current metadata from
the PMC bucket, using exactly the same lookup as build_manifest.py, and updates its row.

The set of articles does not change: only version, MD5, XML key and (if PMC changed it)
license or status. Every refresh is logged in manifest_v1.meta.json under "refreshes",
and the CSV diff is visible in git.

    python ingest/refresh_manifest.py --from-failures data/raw/v1/download_failures.csv --dry-run
    python ingest/refresh_manifest.py --from-failures data/raw/v1/download_failures.csv
    python ingest/download.py --dest data/raw/v1 --dest s3://...      # then fetch just those
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

try:  # imported as a package (tests) or run as a script from the repo root
    from ingest.build_manifest import FIELDS, OK, fetch_row, s3_client, summarize
except ImportError:
    from build_manifest import FIELDS, OK, fetch_row, s3_client, summarize

DEFAULT_MANIFEST = Path(__file__).parent / "manifest_v1.csv"
REFRESHABLE = ("MD5 mismatch", "NoSuchKey")


def failed_pmcids(failures_csv: Path) -> list[str]:
    """PMCIDs whose download failed because the file changed, not because of the network."""
    with failures_csv.open(newline="", encoding="utf-8") as f:
        return [r["pmcid"] for r in csv.DictReader(f) if any(k in r["error"] for k in REFRESHABLE)]


def refresh(rows: list[dict], pmcids: list[str], s3) -> list[dict]:
    """Replace the rows for `pmcids` in place; return a list of change records."""
    index = {r["pmcid"]: i for i, r in enumerate(rows)}
    changes = []
    for pmcid in pmcids:
        if pmcid not in index:
            changes.append({"pmcid": pmcid, "change": "not in manifest; skipped"})
            continue
        old = rows[index[pmcid]]
        new = {k: str(v) for k, v in fetch_row(s3, pmcid).items()}
        if not new.get("title"):  # article gone from the bucket: keep its metadata, change only the status
            new = {**old, "status": new["status"]}
        diff = {k: (old.get(k, ""), new[k]) for k in FIELDS if str(old.get(k, "")) != new[k]}
        rows[index[pmcid]] = new
        changes.append({"pmcid": pmcid, "change": diff})
    return changes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-failures", type=Path, help="download_failures.csv written by download.py")
    src.add_argument("--only", nargs="+", metavar="PMCID")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = p.parse_args()

    pmcids = args.only or failed_pmcids(args.from_failures)
    if not pmcids:
        print("Nothing to refresh: no MD5-mismatch or missing-key failures listed.")
        return 0

    with args.manifest.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    before_ok = sum(r["status"] == OK for r in rows)

    changes = refresh(rows, pmcids, s3_client())

    kinds: dict[str, int] = {}
    for c in changes:
        d = c["change"]
        label = d if isinstance(d, str) else ", ".join(sorted(d)) or "no change"
        kinds[label] = kinds.get(label, 0) + 1
        if isinstance(d, dict) and "status" in d:
            print(f"  {c['pmcid']}: status {d['status'][0]} -> {d['status'][1]}")
    after_ok = sum(r["status"] == OK for r in rows)
    print(f"{len(pmcids)} articles checked. Fields changed: {json.dumps(kinds, indent=2)}")
    print(f"articles in corpus: {before_ok} -> {after_ok}")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    with args.manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")  # LF everywhere, so git diffs stay small
        w.writeheader()
        w.writerows(rows)

    meta_path = args.manifest.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update(summarize(rows))
    meta.setdefault("refreshes", []).append({
        "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "reason": "PMC changed these files after the manifest was built (MD5 mismatch or missing key on download)",
        "articles": len(pmcids),
        "pmcids": pmcids,
        "fields_changed": kinds,
    })
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"updated {args.manifest} and {meta_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
