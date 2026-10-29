"""Build the frozen corpus manifest (ingest/manifest_v1.csv).

Step 1  PMC E-utilities search -> list of PMCIDs matching the corpus query.
Step 2  For each PMCID, read its metadata JSON from the PMC Cloud Service
        (s3://pmc-oa-opendata, public, us-east-1, no AWS account needed),
        taking the latest article version.
Step 3  Write one row per article, plus a sidecar JSON recording the query,
        date, and counts so the corpus is reproducible and auditable.

Run once and commit the outputs. Do NOT re-run casually: PMC keeps adding
papers, so a re-run produces a different corpus (that would be manifest_v2).

    python ingest/build_manifest.py
    python ingest/build_manifest.py --limit 50 --out /tmp/manifest_test.csv   # quick trial
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

QUERY = 'parkinson*[tiab] AND (gait[tiab] OR "freezing of gait"[tiab] OR walking[tiab]) AND open access[filter]'
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
BUCKET = "pmc-oa-opendata"
REGION = "us-east-1"

FIELDS = [
    "pmcid", "version", "pmid", "doi", "title", "citation", "license_code",
    "xml_key", "xml_md5", "status",
]
# status values
OK = "ok"                    # included in the corpus
RETRACTED = "retracted"      # kept in the manifest for audit, excluded from the corpus
NOT_OA = "not_oa"            # metadata says not open access; excluded
NO_XML = "no_xml"            # no JATS XML available; excluded
MISSING = "missing_in_s3"    # search hit, but no folder in the bucket; excluded

_S3_URL_RE = re.compile(r"^s3://[^/]+/(?P<key>[^?]+)(?:\?md5=(?P<md5>[0-9a-f]+))?$")


# ---------------------------------------------------------------- step 1
def search_pmc(query: str, email: str | None = None, api_key: str | None = None) -> list[str]:
    """Return PMCIDs (as 'PMC123') for the query. PMC esearch caps retmax at 10,000."""
    params = {"db": "pmc", "term": query, "retmode": "json", "retmax": 10000, "tool": "clinical-rag"}
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    url = f"{EUTILS}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        result = json.load(r)["esearchresult"]
    count, ids = int(result["count"]), result["idlist"]
    if count > len(ids):
        raise RuntimeError(f"query matches {count} articles but only {len(ids)} returned; narrow the query")
    return [f"PMC{i}" for i in ids]


# ---------------------------------------------------------------- step 2
def s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3", region_name=REGION,
        config=Config(signature_version=UNSIGNED, max_pool_connections=32, retries={"max_attempts": 5}),
    )


def latest_version(s3, pmcid: str) -> int | None:
    """Article folders are named PMC123.<version>/ ; return the highest version, or None."""
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{pmcid}.", Delimiter="/")
    versions = []
    for p in resp.get("CommonPrefixes", []):
        m = re.fullmatch(rf"{pmcid}\.(\d+)/", p["Prefix"])
        if m:
            versions.append(int(m.group(1)))
    return max(versions) if versions else None


def parse_s3_url(url: str | None) -> tuple[str, str]:
    if not url:
        return "", ""
    m = _S3_URL_RE.match(url)
    return (m.group("key"), m.group("md5") or "") if m else ("", "")


def row_from_metadata(pmcid: str, meta: dict) -> dict:
    xml_key, xml_md5 = parse_s3_url(meta.get("xml_url"))
    if meta.get("is_retracted"):
        status = RETRACTED
    elif not meta.get("is_pmc_openaccess"):
        status = NOT_OA
    elif not xml_key:
        status = NO_XML
    else:
        status = OK
    return {
        "pmcid": pmcid,
        "version": meta.get("version", ""),
        "pmid": meta.get("pmid") or "",
        "doi": meta.get("doi") or "",
        "title": " ".join((meta.get("title") or "").split()),
        "citation": " ".join((meta.get("citation") or "").split()),
        "license_code": meta.get("license_code") or "unknown",
        "xml_key": xml_key,
        "xml_md5": xml_md5,
        "status": status,
    }


def fetch_row(s3, pmcid: str) -> dict:
    v = latest_version(s3, pmcid)
    if v is None:
        return {**{f: "" for f in FIELDS}, "pmcid": pmcid, "status": MISSING}
    obj = s3.get_object(Bucket=BUCKET, Key=f"{pmcid}.{v}/{pmcid}.{v}.json")
    return row_from_metadata(pmcid, json.load(obj["Body"]))


# ---------------------------------------------------------------- step 3
def summarize(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_license: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == OK:
            by_license[r["license_code"]] = by_license.get(r["license_code"], 0) + 1
    return {
        "search_hits": len(rows),
        "in_corpus": by_status.get(OK, 0),
        "by_status": dict(sorted(by_status.items())),
        "in_corpus_by_license": dict(sorted(by_license.items(), key=lambda kv: -kv[1])),
    }


def write_outputs(rows: list[dict], out: Path, query: str) -> dict:
    rows = sorted(rows, key=lambda r: int(r["pmcid"][3:]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    meta = {
        "query": query,
        "built_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": f"s3://{BUCKET} ({REGION}) + NCBI E-utilities esearch db=pmc",
        **summarize(rows),
    }
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path(__file__).parent / "manifest_v1.csv")
    p.add_argument("--query", default=QUERY)
    p.add_argument("--limit", type=int, help="only process the first N hits (for a trial run)")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--email", help="contact email NCBI asks API users to send (optional)")
    p.add_argument("--api-key", help="NCBI API key (optional)")
    p.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = p.parse_args()

    if args.out.exists() and not args.force:
        print(f"{args.out} already exists. The manifest is meant to be frozen; use --force or a new --out.")
        return 1

    t0 = time.time()
    pmcids = search_pmc(args.query, args.email, args.api_key)
    print(f"search: {len(pmcids)} hits")
    if args.limit:
        pmcids = pmcids[: args.limit]

    s3 = s3_client()
    rows, failed = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_row, s3, pid): pid for pid in pmcids}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                rows.append(fut.result())
            except Exception as e:  # noqa: BLE001 - report and keep going
                failed.append((futures[fut], repr(e)))
            if n % 500 == 0:
                print(f"  {n}/{len(pmcids)}")

    if failed:
        print(f"{len(failed)} lookups failed (first 5): {failed[:5]}")
        print("Nothing written. Re-run; failures are usually transient.")
        return 1

    meta = write_outputs(rows, args.out, args.query)
    print(json.dumps({k: v for k, v in meta.items() if k != "query"}, indent=2))
    print(f"wrote {args.out} in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
