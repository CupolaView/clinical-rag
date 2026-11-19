"""Download the frozen corpus (JATS XML) listed in ingest/manifest_v1.csv.

Reads every manifest row with status == "ok", fetches its XML from the public PMC
Cloud Service bucket (s3://pmc-oa-opendata, us-east-1, no AWS account needed), checks
it against the manifest MD5, and writes it to one or more destinations:

    a local folder   e.g. data/raw/v1                          (no AWS credentials needed)
    an S3 prefix     e.g. s3://gaurav-mlops-us-west-2/clinical-rag/raw/v1   (uses your AWS profile)

Each article is stored as <dest>/<PMCID>.xml. Re-running is safe: files already at a
destination with the right MD5 are skipped, so an interrupted run just picks up where
it stopped. Failures are listed in <first local dest or data>/download_failures.csv.

    python ingest/download.py --dest data/raw/v1 --limit 20                  # quick trial
    python ingest/download.py --dest data/raw/v1 --dest s3://gaurav-mlops-us-west-2/clinical-rag/raw/v1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

SOURCE_BUCKET = "pmc-oa-opendata"
SOURCE_REGION = "us-east-1"
DEFAULT_MANIFEST = Path(__file__).parent / "manifest_v1.csv"


@dataclass(frozen=True)
class Article:
    pmcid: str
    xml_key: str
    xml_md5: str


def load_articles(manifest: Path) -> list[Article]:
    with manifest.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [Article(r["pmcid"], r["xml_key"], r["xml_md5"]) for r in rows if r["status"] == "ok"]


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # integrity check against the manifest, not security


# ------------------------------------------------------------------ destinations
class LocalDest:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def __str__(self) -> str:
        return str(self.root)

    def has(self, a: Article) -> bool:
        p = self.root / f"{a.pmcid}.xml"
        return p.exists() and md5(p.read_bytes()) == a.xml_md5

    def put(self, a: Article, data: bytes) -> None:
        p = self.root / f"{a.pmcid}.xml"
        tmp = p.with_suffix(".xml.part")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic, so a crash never leaves a half-written file under the real name


class S3Dest:
    def __init__(self, uri: str, client=None):
        bucket, _, prefix = uri.removeprefix("s3://").partition("/")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.client = client or _s3_client(signed=True)

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def _key(self, a: Article) -> str:
        return f"{self.prefix}/{a.pmcid}.xml" if self.prefix else f"{a.pmcid}.xml"

    def has(self, a: Article) -> bool:
        from botocore.exceptions import ClientError

        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._key(a))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        # put_object (single part) makes the ETag the MD5; the metadata is a fallback
        return head.get("ETag", "").strip('"') == a.xml_md5 or head.get("Metadata", {}).get("md5") == a.xml_md5

    def put(self, a: Article, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._key(a), Body=data, ContentType="application/xml",
            Metadata={"md5": a.xml_md5, "pmcid": a.pmcid, "source": f"s3://{SOURCE_BUCKET}/{a.xml_key}"},
        )


def make_dest(spec: str, s3_client=None):
    return S3Dest(spec, s3_client) if spec.startswith("s3://") else LocalDest(spec)


def _s3_client(signed: bool):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    cfg = Config(max_pool_connections=32, retries={"max_attempts": 5, "mode": "standard"})
    if signed:
        return boto3.client("s3", config=cfg)  # region comes from your AWS profile / AWS_REGION
    return boto3.client("s3", region_name=SOURCE_REGION, config=cfg.merge(Config(signature_version=UNSIGNED)))


# ------------------------------------------------------------------ work
def fetch_one(a: Article, dests: list, source) -> str:
    """Return 'skipped', 'downloaded', or raise on failure."""
    missing = [d for d in dests if not d.has(a)]
    if not missing:
        return "skipped"
    data = source.get_object(Bucket=SOURCE_BUCKET, Key=a.xml_key)["Body"].read()
    got = md5(data)
    if got != a.xml_md5:
        raise ValueError(f"MD5 mismatch: manifest {a.xml_md5}, downloaded {got}")
    for d in missing:
        d.put(a, data)
    return "downloaded"


def run(articles: list[Article], dests: list, source, workers: int = 16) -> dict:
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, a, dests, source): a for a in articles}
        for n, fut in enumerate(as_completed(futures), 1):
            a = futures[fut]
            try:
                counts[fut.result()] += 1
            except Exception as e:  # noqa: BLE001 - record and keep going
                counts["failed"] += 1
                failures.append((a.pmcid, repr(e)[:300]))
            if n % 250 == 0 or n == len(articles):
                print(f"  {n}/{len(articles)}  {counts}", flush=True)
    return {"articles": len(articles), **counts, "seconds": round(time.time() - t0, 1), "failures": failures}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", action="append", required=True,
                   help="local folder or s3://bucket/prefix; repeat to write to several")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--limit", type=int, help="only the first N articles (trial run)")
    p.add_argument("--only", nargs="+", metavar="PMCID", help="only these PMCIDs")
    p.add_argument("--workers", type=int, default=16)
    args = p.parse_args()

    articles = load_articles(args.manifest)
    if args.only:
        wanted = set(args.only)
        articles = [a for a in articles if a.pmcid in wanted]
    if args.limit:
        articles = articles[: args.limit]
    dests = [make_dest(d) for d in args.dest]
    print(f"{len(articles)} articles -> {', '.join(map(str, dests))}")

    summary = run(articles, dests, _s3_client(signed=False), args.workers)

    report_dir = next((d.root for d in dests if isinstance(d, LocalDest)), Path("data"))
    report_dir.mkdir(parents=True, exist_ok=True)
    fail_path = report_dir / "download_failures.csv"
    with fail_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pmcid", "error"])
        w.writerows(summary["failures"])
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=2))
    if summary["failed"]:
        changed = sum(any(k in e for k in ("MD5 mismatch", "NoSuchKey")) for _, e in summary["failures"])
        other = summary["failed"] - changed
        print(f"{summary['failed']} failed; see {fail_path}.")
        if changed:
            print(f"  {changed} changed at PMC since the manifest was built (MD5 mismatch or missing file).\n"
                  f"  Fix: python ingest/refresh_manifest.py --from-failures {fail_path}\n"
                  f"  then re-run this command.")
        if other:
            print(f"  {other} look like network or permission errors: re-run this command to retry them.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
