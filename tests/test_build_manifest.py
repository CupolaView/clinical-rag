import csv
import io
import json

from ingest import build_manifest as bm


class FakeS3:
    """Mimics the two boto3 calls build_manifest uses, with the bucket's real layout."""

    def __init__(self, articles):
        self.articles = articles  # {"PMC1": {1: meta, 2: meta}}

    def list_objects_v2(self, Bucket, Prefix, Delimiter):
        pmcid = Prefix.rstrip(".")
        versions = self.articles.get(pmcid, {})
        # include a look-alike ID to make sure prefix matching is exact
        prefixes = [{"Prefix": f"{pmcid}.{v}/"} for v in versions] + [{"Prefix": f"{pmcid}0.1/"}]
        return {"CommonPrefixes": prefixes if versions else []}

    def get_object(self, Bucket, Key):
        pmcid, rest = Key.split(".", 1)
        v = int(rest.split("/")[0])
        return {"Body": io.BytesIO(json.dumps(self.articles[pmcid][v]).encode())}


def meta(pmcid, v, **kw):
    base = {
        "pmcid": pmcid, "version": v, "pmid": 1, "doi": "10.1/x", "title": "T  \n itle",
        "citation": "J. 2023", "is_pmc_openaccess": True, "is_retracted": False,
        "license_code": "CC BY",
        "xml_url": f"s3://pmc-oa-opendata/{pmcid}.{v}/{pmcid}.{v}.xml?md5=abc123",
    }
    return {**base, **kw}


def test_picks_latest_version_and_parses_xml_key():
    s3 = FakeS3({"PMC1": {1: meta("PMC1", 1), 2: meta("PMC1", 2)}})
    row = bm.fetch_row(s3, "PMC1")
    assert row["version"] == 2
    assert row["xml_key"] == "PMC1.2/PMC1.2.xml"
    assert row["xml_md5"] == "abc123"
    assert row["title"] == "T itle"
    assert row["status"] == bm.OK


def test_statuses():
    s3 = FakeS3({
        "PMC2": {1: meta("PMC2", 1, is_retracted=True)},
        "PMC3": {1: meta("PMC3", 1, is_pmc_openaccess=False)},
        "PMC4": {1: meta("PMC4", 1, xml_url=None)},
        "PMC5": {1: meta("PMC5", 1, license_code=None)},
    })
    assert bm.fetch_row(s3, "PMC2")["status"] == bm.RETRACTED
    assert bm.fetch_row(s3, "PMC3")["status"] == bm.NOT_OA
    assert bm.fetch_row(s3, "PMC4")["status"] == bm.NO_XML
    assert bm.fetch_row(s3, "PMC5")["license_code"] == "unknown"
    assert bm.fetch_row(s3, "PMC9")["status"] == bm.MISSING


def test_write_outputs(tmp_path):
    s3 = FakeS3({"PMC10": {1: meta("PMC10", 1)}, "PMC9": {1: meta("PMC9", 1, is_retracted=True)}})
    rows = [bm.fetch_row(s3, p) for p in ("PMC10", "PMC9")]
    out = tmp_path / "m.csv"
    summary = bm.write_outputs(rows, out, "q")
    got = list(csv.DictReader(out.open()))
    assert [r["pmcid"] for r in got] == ["PMC9", "PMC10"]  # numeric sort
    assert summary["in_corpus"] == 1
    assert summary["by_status"] == {"ok": 1, "retracted": 1}
    assert json.loads(out.with_suffix(".meta.json").read_text())["query"] == "q"
