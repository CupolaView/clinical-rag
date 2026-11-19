import hashlib
import io

import pytest

from ingest.download import Article, LocalDest, fetch_one, run


class FakeSource:
    def __init__(self, files):
        self.files, self.calls = files, 0

    def get_object(self, Bucket, Key):
        self.calls += 1
        return {"Body": io.BytesIO(self.files[Key])}


def art(pmcid, data):
    return Article(pmcid, f"{pmcid}.1/{pmcid}.1.xml", hashlib.md5(data).hexdigest())


def test_downloads_then_skips_on_rerun(tmp_path):
    data = b"<article/>"
    a = art("PMC1", data)
    src = FakeSource({a.xml_key: data})
    dest = LocalDest(str(tmp_path))
    assert fetch_one(a, [dest], src) == "downloaded"
    assert (tmp_path / "PMC1.xml").read_bytes() == data
    assert fetch_one(a, [dest], src) == "skipped"
    assert src.calls == 1
    assert not list(tmp_path.glob("*.part"))


def test_md5_mismatch_is_a_failure_and_writes_nothing(tmp_path):
    a = Article("PMC2", "PMC2.1/PMC2.1.xml", "0" * 32)
    dest = LocalDest(str(tmp_path))
    with pytest.raises(ValueError, match="MD5 mismatch"):
        fetch_one(a, [dest], FakeSource({a.xml_key: b"<article/>"}))
    assert not (tmp_path / "PMC2.xml").exists()


def test_corrupt_local_copy_is_replaced(tmp_path):
    data = b"<article>ok</article>"
    a = art("PMC3", data)
    (tmp_path / "PMC3.xml").write_bytes(b"truncated")
    assert fetch_one(a, [LocalDest(str(tmp_path))], FakeSource({a.xml_key: data})) == "downloaded"
    assert (tmp_path / "PMC3.xml").read_bytes() == data


def test_run_counts_and_collects_failures(tmp_path):
    good, bad = art("PMC4", b"<a/>"), Article("PMC5", "PMC5.1/PMC5.1.xml", "0" * 32)
    src = FakeSource({good.xml_key: b"<a/>", bad.xml_key: b"<b/>"})
    s = run([good, bad], [LocalDest(str(tmp_path))], src, workers=2)
    assert (s["downloaded"], s["failed"]) == (1, 1)
    assert s["failures"][0][0] == "PMC5"
