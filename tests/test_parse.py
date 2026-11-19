from pathlib import Path

from ingest.parse import chunk_article, parse_article

FIXTURE = Path(__file__).parent / "fixtures" / "mini_article.xml"
LONG = " ".join(f"Sentence number {i} describes the method in detail." for i in range(120))  # ~840 words


def _parsed():
    xml = FIXTURE.read_text(encoding="utf-8").replace("LONGPARA", LONG)
    return parse_article(xml.encode("utf-8"))


def _chunks(max_words=300):
    return chunk_article("PMC0000001", _parsed(), "CC BY", max_words)


def test_title_and_structured_abstract():
    p = _parsed()
    assert p.title == "Synthetic Study of Gait Freezing"
    paths = {tuple(b.path) for b in p.blocks}
    assert ("Abstract", "Background") in paths and ("Abstract", "Results") in paths
    assert not any("Graphical abstract" in b.text for b in p.blocks)


def test_section_paths_keep_numbering_and_nesting():
    paths = {tuple(c.section_path) for c in _chunks()}
    assert ("1. Introduction",) in paths
    assert ("Materials and Methods", "Data Processing") in paths
    assert ("4. Discussion", "Limitations") in paths


def test_boilerplate_and_references_are_skipped():
    text = " ".join(c.text for c in _chunks())
    for gone in ("None declared", "Everyone did everything", "Thanks to all", "must not appear"):
        assert gone not in text


def test_formula_replaced_and_citation_kept():
    text = " ".join(c.text for c in _chunks())
    assert "[formula]" in text and "<mml" not in text
    assert "prior work [1]" in text


def test_floating_table_gets_citing_section_and_footnote():
    t1 = [c for c in _chunks() if c.label == "Table 1"]
    assert len(t1) == 1
    assert t1[0].kind == "table"
    assert t1[0].section_path == ["Materials and Methods", "Data Processing"]
    assert "Freeze index | Ratio of freeze to locomotor band power" in t1[0].text
    assert "SD, standard deviation." in t1[0].text


def test_inline_table_and_figure_are_own_chunks():
    cs = _chunks()
    t2 = next(c for c in cs if c.label == "Table 2")
    assert t2.section_path == ["Materials and Methods", "Statistical Analysis"]
    assert "Feature | p" in t2.text and "Turning | 0.01" in t2.text
    f1 = next(c for c in cs if c.label == "Figure 1")
    assert f1.kind == "figure" and "Sensor placement" in f1.text
    # the table text is not duplicated inside the paragraph chunk
    para = next(c for c in cs if "paired t-tests" in c.text)
    assert "Turning" not in para.text


def test_long_paragraph_split_within_limit_and_section():
    cs = [c for c in _chunks(max_words=300) if "Sentence number" in c.text]
    assert len(cs) >= 3
    assert all(c.n_words <= 300 for c in cs)
    assert all(c.section_path == ["Materials and Methods", "Data Processing"] for c in cs)


def test_chunks_never_cross_sections_and_ids_are_unique():
    cs = _chunks()
    assert len({c.chunk_id for c in cs}) == len(cs)
    for c in cs:
        assert c.embed_text.startswith("Synthetic Study of Gait Freezing\nSection: ")
        assert c.license == "CC BY"
    intro = [c for c in cs if c.section_path == ["1. Introduction"]]
    assert all("paired t-tests" not in c.text for c in intro)


def test_abstract_only_article_flagged():
    xml = b"""<article><front><article-meta><title-group><article-title>T</article-title></title-group>
    <abstract><p>Only an abstract.</p></abstract></article-meta></front></article>"""
    p = parse_article(xml)
    assert not p.has_body
    assert chunk_article("PMC9", p)[0].section_path == ["Abstract"]


def test_boilerplate_title_variants_are_skipped():
    from ingest.parse import is_boilerplate
    for title in ("Consent", "Informed consent", "Conflict of Interest Statement", "Data Availability Statement",
                  "Ethics Statement", "Author Contributions", "7. Funding", "Supplementary Material",
                  "Supporting information", "Publisher\u2019s Note", "Abbreviations"):
        assert is_boilerplate(title), title
    for title in ("Methods", "Discussion", "Consent and recruitment procedure", "Data Processing"):
        assert not is_boilerplate(title), title


def test_named_html_entities_do_not_break_parsing():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD v1.2 20190208//EN" "JATS-archivearticle1.dtd">
    <article><front><article-meta><title-group><article-title>T</article-title></title-group></article-meta></front>
    <body><sec><title>Methods</title><p>Speed&nbsp;was 1.2&#x00A0;m/s &amp; cadence &lt; 110.</p></sec></body></article>"""
    p = parse_article(xml)
    assert p.blocks[0].text == "Speed was 1.2 m/s & cadence < 110."  # non-breaking spaces normalised
