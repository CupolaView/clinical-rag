"""Parse JATS XML articles into section-aware chunks (JSONL).

For every article in the manifest with status == "ok" whose XML is in --src:

  * text     abstract and body paragraphs, packed into chunks of about --max-words
             words (default 300, roughly 400 tokens) that never cross a section boundary.
             A paragraph longer than the limit is split at sentence boundaries.
  * table    one chunk per table: label, caption, rows as "cell | cell", footnotes.
             Very long tables are split by rows, repeating the header row.
  * figure   one chunk per figure caption (the image itself is not indexed).

Every chunk records its section path, e.g. ["Materials and Methods", "Data Processing"],
so answers can cite "PMC5557770 §Materials and Methods → Data Processing". Tables that
PMC places at the end of the article (<floats-group>) are given the section of the
first paragraph that cites them. Reference lists, acknowledgements, funding, conflict
of interest, author contributions and similar boilerplate are skipped.

Output: one JSON object per line, with `embed_text` = title + section + text (what gets
embedded) and `text` (what gets shown and cited).

    python ingest/parse.py --src data/raw/v1 --out data/processed/v1/chunks.jsonl
"""

from __future__ import annotations

import argparse
import csv
import html.entities
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).parent / "manifest_v1.csv"
DEFAULT_MAX_WORDS = 300
SEP = " → "

BOILERPLATE = re.compile(
    r"^(acknowledg|funding|financial (support|disclosure)|conflicts? of interest|competing interest|"
    r"declaration of (competing|interest)|disclosure|author'?s?'? contributions?|authors’ contributions?|"
    r"contributions? of authors|ethic|(informed )?consent( for publication| statement| to participate)?$|"
    r"patient consent|data (availability|sharing)|availability of data|"
    r"abbreviations|publisher'?s note|publisher’s note|supplementary|supporting information|footnotes|"
    r"additional information|orcid|statements?$|declarations?$)",
    re.IGNORECASE,
)
_NUMBERING = re.compile(r"^\s*((\d+(\.\d+)*\.?)|([ivxlc]+\.)|([a-z]\.))\s+", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_WS = re.compile(r"\s+")


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean(s: str) -> str:
    return _WS.sub(" ", s).strip()


# ------------------------------------------------------------------ text extraction
_FORMULA = {"disp-formula", "inline-formula", "math"}
_SKIP_IN_TEXT = {"table-wrap", "fig", "table-wrap-group", "fig-group", "supplementary-material", "label", "alternatives"}


def text_of(el, skip=_SKIP_IN_TEXT) -> str:
    """Visible text of an element, leaving out floats and replacing math with [formula]."""
    parts: list[str] = []

    def rec(e):
        name = local(e.tag)
        if name in _FORMULA:
            tex = next((t for t in e.iter() if local(t.tag) == "tex-math"), None)
            parts.append(f" {clean(''.join(tex.itertext()))} " if tex is not None else " [formula] ")
        elif name in skip and e is not el:
            pass
        else:
            if e.text:
                parts.append(e.text)
            for c in e:
                rec(c)
        if e is not el and e.tail:
            parts.append(e.tail)

    rec(el)
    return clean("".join(parts))


def heading(sec) -> str:
    title = next((c for c in sec if local(c.tag) == "title"), None)
    label = next((c for c in sec if local(c.tag) == "label"), None)
    t = text_of(title, skip=set()) if title is not None else ""
    lab = clean("".join(label.itertext())) if label is not None else ""
    if lab and not t.startswith(lab):
        t = f"{lab} {t}".strip()
    return t or "(untitled)"


def is_boilerplate(title: str) -> bool:
    return bool(BOILERPLATE.match(_NUMBERING.sub("", title).strip()))


# ------------------------------------------------------------------ blocks
@dataclass
class Block:
    kind: str                 # text | table | figure
    path: list[str]
    text: str
    label: str = ""
    rows: list[str] = field(default_factory=list)   # tables only
    header: str = ""                                 # tables only


def table_block(tw, path) -> Block:
    label = clean("".join(next((c for c in tw if local(c.tag) == "label"), ET.Element("x")).itertext()))
    cap_el = next((c for c in tw if local(c.tag) == "caption"), None)
    caption = text_of(cap_el, skip=set()) if cap_el is not None else ""
    rows = []
    for tr in (e for e in tw.iter() if local(e.tag) == "tr"):
        cells = [text_of(td, skip=set()) for td in tr if local(td.tag) in ("td", "th")]
        if any(cells):
            rows.append(" | ".join(cells))
    foot_el = next((c for c in tw.iter() if local(c.tag) == "table-wrap-foot"), None)
    foot = text_of(foot_el, skip=set()) if foot_el is not None else ""
    header = rows[0] if rows else ""
    body = "\n".join(rows)
    text = "\n".join(x for x in (f"{label} {caption}".strip(), body, foot) if x)
    return Block("table", path, text, label=label, rows=rows, header=header)


def figure_block(fig, path) -> Block:
    label = clean("".join(next((c for c in fig if local(c.tag) == "label"), ET.Element("x")).itertext()))
    cap_el = next((c for c in fig if local(c.tag) == "caption"), None)
    caption = text_of(cap_el, skip=set()) if cap_el is not None else ""
    return Block("figure", path, f"{label} {caption}".strip(), label=label)


def walk(container, path, blocks, cited_in, floats_done):
    """Collect blocks from a <sec>, <body> or <abstract>, depth first, in document order."""
    for child in container:
        name = local(child.tag)
        if name == "sec":
            title = heading(child)
            if not is_boilerplate(title):
                walk(child, path + [title], blocks, cited_in, floats_done)
        elif name in ("p", "disp-quote", "boxed-text", "list", "def-list", "statement"):
            txt = text_of(child)
            if txt:
                blocks.append(Block("text", path, txt))
            for x in child.iter():
                if local(x.tag) == "xref" and x.get("ref-type") in ("table", "fig"):
                    for rid in (x.get("rid") or "").split():
                        cited_in.setdefault(rid, path)
            for fl in child.iter():  # floats embedded in a paragraph
                if local(fl.tag) == "table-wrap":
                    blocks.append(table_block(fl, path)); floats_done.add(fl.get("id"))
                elif local(fl.tag) == "fig":
                    blocks.append(figure_block(fl, path)); floats_done.add(fl.get("id"))
        elif name in ("table-wrap", "table-wrap-group"):
            for tw in ([child] if name == "table-wrap" else [c for c in child if local(c.tag) == "table-wrap"]):
                blocks.append(table_block(tw, path)); floats_done.add(tw.get("id"))
        elif name in ("fig", "fig-group"):
            for fg in ([child] if name == "fig" else [c for c in child if local(c.tag) == "fig"]):
                blocks.append(figure_block(fg, path)); floats_done.add(fg.get("id"))


@dataclass
class Parsed:
    title: str
    blocks: list[Block]
    has_body: bool


_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")


def _replace_named_entities(xml: str) -> str:
    """Some PMC files use HTML entities (e.g. &nbsp;) defined only in the JATS DTD."""
    def sub(m):
        name = m.group(1)
        if name in _XML_ENTITIES or name not in html.entities.name2codepoint:
            return m.group(0)
        return f"&#{html.entities.name2codepoint[name]};"
    return _NAMED_ENTITY.sub(sub, xml)


def _parse_xml(xml: bytes | str):
    try:
        return ET.fromstring(xml)
    except ET.ParseError as e:
        if "undefined entity" not in str(e):
            raise
        text = xml.decode("utf-8") if isinstance(xml, bytes) else xml
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)  # str input cannot carry an encoding declaration
        return ET.fromstring(_replace_named_entities(text))


def parse_article(xml: bytes | str) -> Parsed:
    root = _parse_xml(xml)
    meta = next((e for e in root.iter() if local(e.tag) == "article-meta"), None)
    title_el = next((e for e in meta.iter() if local(e.tag) == "article-title"), None) if meta is not None else None
    title = text_of(title_el, skip=set()) if title_el is not None else ""

    blocks: list[Block] = []
    cited_in: dict[str, list[str]] = {}
    floats_done: set = set()

    if meta is not None:
        for ab in (c for c in meta if local(c.tag) == "abstract"):
            if ab.get("abstract-type") in ("graphical", "teaser", "toc"):
                continue
            walk(ab, ["Abstract"], blocks, cited_in, floats_done)

    body = next((c for c in root if local(c.tag) == "body"), None)
    if body is not None:
        walk(body, [], blocks, cited_in, floats_done)

    # tables/figures PMC puts after the back matter: give them the section that cites them
    for fg in (c for c in root if local(c.tag) == "floats-group"):
        for fl in fg.iter():
            fid = fl.get("id")
            if fid in floats_done:
                continue
            if local(fl.tag) == "table-wrap":
                blocks.append(table_block(fl, cited_in.get(fid, ["(floating)"]))); floats_done.add(fid)
            elif local(fl.tag) == "fig":
                blocks.append(figure_block(fl, cited_in.get(fid, ["(floating)"]))); floats_done.add(fid)

    has_body = any(b.kind == "text" and b.path[:1] != ["Abstract"] for b in blocks)
    return Parsed(title, blocks, has_body)


# ------------------------------------------------------------------ chunking
@dataclass
class Chunk:
    chunk_id: str
    pmcid: str
    title: str
    license: str
    section_path: list[str]
    section: str
    label: str
    kind: str
    text: str
    embed_text: str
    n_words: int


def _split_long(text: str, max_words: int) -> list[str]:
    sentences = _SENTENCE.split(text)
    out, cur = [], []
    for s in sentences:
        words = s.split()
        if len(words) > max_words:  # one enormous "sentence": hard-split by words
            if cur:
                out.append(" ".join(cur)); cur = []
            out += [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
            continue
        if len(cur) + len(words) > max_words and cur:
            out.append(" ".join(cur))
            cur = []
        cur += words
    if cur:
        out.append(" ".join(cur))
    return out


def _pieces(blocks: list[Block], max_words: int):
    """Yield (block_kind, path, label, text) pieces sized for embedding."""
    buf: list[str] = []
    buf_path = None

    def flush():
        nonlocal buf
        if buf:
            yield ("text", buf_path, "", "\n\n".join(buf))
        buf = []

    for b in blocks:
        if b.kind == "text":
            if buf_path is not None and b.path != buf_path:
                yield from flush()
            buf_path = b.path
            n = len(b.text.split())
            if n > max_words:
                yield from flush()
                for part in _split_long(b.text, max_words):
                    yield ("text", b.path, "", part)
                continue
            if sum(len(x.split()) for x in buf) + n > max_words:
                yield from flush()
            buf.append(b.text)
        else:
            yield from flush()
            buf_path = None
            if b.kind == "table" and len(b.text.split()) > 2 * max_words and len(b.rows) > 2:
                head = f"{b.label} (continued)".strip()
                part, size = [], 0
                for r in b.rows[1:]:
                    if size + len(r.split()) > max_words and part:
                        yield ("table", b.path, b.label, "\n".join([head, b.header] + part)); part, size = [], 0
                    part.append(r); size += len(r.split())
                first = b.text.split("\n", 1)[0]
                if part:
                    yield ("table", b.path, b.label, "\n".join([head, b.header] + part))
                yield ("table", b.path, b.label, first)  # caption on its own, so it stays findable
            else:
                yield (b.kind, b.path, b.label, b.text)
    yield from flush()


def chunk_article(pmcid: str, parsed: Parsed, license_code: str = "", max_words: int = DEFAULT_MAX_WORDS) -> list[Chunk]:
    chunks = []
    for i, (kind, path, label, text) in enumerate(_pieces(parsed.blocks, max_words)):
        section = SEP.join(path) if path else "Body"
        where = f"{section} ({label})" if label else section
        chunks.append(Chunk(
            chunk_id=f"{pmcid}:{i:04d}", pmcid=pmcid, title=parsed.title, license=license_code,
            section_path=list(path), section=section, label=label, kind=kind, text=text,
            embed_text=f"{parsed.title}\nSection: {where}\n\n{text}", n_words=len(text.split()),
        ))
    return chunks


# ------------------------------------------------------------------ CLI
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=Path, default=Path("data/raw/v1"), help="folder of <PMCID>.xml files")
    p.add_argument("--out", type=Path, default=Path("data/processed/v1/chunks.jsonl"))
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--max-words", type=int, default=DEFAULT_MAX_WORDS)
    p.add_argument("--only", nargs="+", metavar="PMCID")
    args = p.parse_args()

    with args.manifest.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["status"] == "ok"]
    if args.only:
        rows = [r for r in rows if r["pmcid"] in set(args.only)]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    missing, failed, no_body = [], [], []
    kinds: Counter = Counter()
    words: list[int] = []
    n_articles = 0
    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        for r in rows:
            path = args.src / f"{r['pmcid']}.xml"
            if not path.exists():
                missing.append(r["pmcid"]); continue
            try:
                parsed = parse_article(path.read_bytes())
            except ET.ParseError as e:
                failed.append((r["pmcid"], str(e))); continue
            if not parsed.has_body:
                no_body.append(r["pmcid"])
            for c in chunk_article(r["pmcid"], parsed, r.get("license_code", ""), args.max_words):
                out.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
                kinds[c.kind] += 1
                words.append(c.n_words)
            n_articles += 1

    stats = {
        "articles_parsed": n_articles, "chunks": len(words), "chunks_by_kind": dict(kinds),
        "words_per_chunk": {"median": statistics.median(words) if words else 0, "max": max(words, default=0)},
        "missing_xml": len(missing), "parse_errors": failed,
        "abstract_only_articles": len(no_body), "max_words": args.max_words,
    }
    stats_path = args.out.with_suffix(".stats.json")
    stats_path.write_text(json.dumps({**stats, "abstract_only_pmcids": no_body, "missing_pmcids": missing}, indent=2) + "\n",
                          encoding="utf-8")
    print(json.dumps(stats, indent=2))
    print(f"wrote {args.out} and {stats_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
