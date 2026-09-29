"""Unit tests for the Texas statutes scraper (no network)."""
from src.scrapers.us.states.tx.statutes import scrapeTX as tx


# ---------------------------------------------------------------------------
# Task 1: single-code runs via TX_ONLY_CODES
# ---------------------------------------------------------------------------

def test_work_items_defaults_to_every_code_not_done():
    items = tx._work_items(titles_done={"AG", "AL"}, only_codes="")
    codes = [c for c, _ in items]
    assert "AG" not in codes and "AL" not in codes
    assert len(codes) == len(tx.TX_CODES) - 2


def test_work_items_only_codes_filters_after_done_set():
    items = tx._work_items(titles_done={"AG"}, only_codes="pe, AG ,cv")
    assert [c for c, _ in items] == ["CV", "PE"]


def test_work_items_only_codes_unknown_code_is_ignored():
    items = tx._work_items(titles_done=set(), only_codes="ZZ,PE")
    assert [c for c, _ in items] == ["PE"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.utils.pydanticModels import Node

FIX = Path(__file__).parent / "fixtures" / "tx"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _soup(name: str) -> BeautifulSoup:
    return BeautifulSoup(_fixture(name), "html.parser")


def _code_node(code: str, name: str) -> Node:
    return Node(
        id=f"us/tx/statutes/code={code.lower()}",
        link=f"https://statutes.capitol.texas.gov/?link={code}",
        top_level_title=code.lower(),
        node_type="structure",
        level_classifier="code",
        number=code.lower(),
        node_name=name,
        parent="us/tx/statutes",
    )


@pytest.fixture
def emitted(monkeypatch):
    """Capture every Node the scraper would insert, in order."""
    out: list = []
    monkeypatch.setattr(tx, "insert_node", lambda node, *a, **k: out.append(node) or node)
    return out


def _parse(name: str, code: str, code_name: str, emitted, page=None, fallback_chapter_name=None):
    url = page or f"https://tcss.legis.texas.gov/resources/{code}/htm/{name}"
    tx._parse_page(
        _soup(name), _code_node(code, code_name), code, code_name, url,
        fallback_chapter_name=fallback_chapter_name, seen=set(),
    )
    return emitted


def _by_id(nodes):
    return {n.node_id: n for n in nodes}


# ---------------------------------------------------------------------------
# Task 2: code ids, CV enumeration, article containers, zero-output guard
# ---------------------------------------------------------------------------

def test_code_ids_built_from_quickcodes_json():
    ids = tx._build_code_ids(json.loads(_fixture("QuickCodes.json")))
    assert ids["CV"] == "8"
    assert ids["PE"] == "24"
    assert ids["UT"] == "29"


def test_literal_code_id_table_matches_live_quickcodes():
    live = tx._build_code_ids(json.loads(_fixture("QuickCodes.json")))
    # PB (Probate Code, repealed 2014) is kept in the literal but no longer
    # listed live; every live code must agree with the literal.
    assert {c: tx.TX_CODE_IDS.get(c) for c in live} == live
    assert set(tx.TX_CODE_IDS) - set(live) == {"PB"}


def test_page_token_rejects_the_cv_placeholder_url():
    assert tx._page_token("https://tcss.legis.texas.gov/resources/CV/htm/CV...htm#", "CV") is None
    assert tx._page_token("https://tcss.legis.texas.gov/resources/CV/htm/CV.4.11.htm", "CV") == "4.11"
    assert tx._page_token("https://tcss.legis.texas.gov/resources/PE/htm/PE.19.htm", "CV") is None


def test_pages_from_tree_lists_every_cv_page_once():
    pages = tx._pages_from_tree(json.loads(_fixture("cv_toplevel.json")), "CV")
    urls = [p["url"] for p in pages]
    assert len(urls) == len(set(urls)) == 37
    assert urls[0].endswith("/CV/htm/CV.1.0.htm")
    # chapter with a broken htmLink ("/CV/htm/.htm") is rebuilt from its numbers
    assert any(u.endswith("/CV/htm/CV.22.1.htm") for u in urls)
    # chapter token with a slash ("CHAPTER 6-1/2") is the site's own file token
    assert any(u.endswith("/CV/htm/CV.71.6-1_2.htm") for u in urls)
    names = {p["url"].rsplit("/", 1)[-1]: p["name"] for p in pages}
    assert names["CV.4.11.htm"] == "CHAPTER 11. COTTON"
    assert names["CV.1.0.htm"] == "TITLE 1. GENERAL PROVISIONS"


def test_fetch_pages_falls_through_to_tree_when_api_has_no_page_tokens(monkeypatch):
    calls = []

    def fake_get(url, as_json=False, **kw):
        calls.append(url)
        if "GetStatuteArray" in url:
            return json.loads(_fixture("cv_statute_array.json"))
        if "GetTopLevelHeadings" in url:
            assert "/%2F8/CV/1/true/false" in url
            return json.loads(_fixture("cv_toplevel.json"))
        raise AssertionError(url)

    monkeypatch.setattr(tx, "_get", fake_get)
    monkeypatch.setattr(tx, "_code_ids", lambda: {"CV": "8"})
    pages = tx._fetch_pages("CV")
    assert len(pages) == 37
    assert any("GetTopLevelHeadings" in c for c in calls)


def test_fetch_pages_keeps_usable_api_entries(monkeypatch):
    entries = [{"name": "CHAPTER 19. CRIMINAL HOMICIDE",
                "url": "https://tcss.legis.texas.gov/resources/PE/htm/PE.19.htm"}]
    monkeypatch.setattr(tx, "_get", lambda url, as_json=False, **kw: entries)
    assert tx._fetch_pages("PE") == entries


def test_cv_article_with_sections_is_a_structure_node(emitted):
    nodes = _by_id(_parse("CV.4.11.htm", "CV", "Vernon's Civil Statutes", emitted))
    art = nodes["us/tx/statutes/code=cv/title=4/chapter=11/article=165-4"]
    assert art.node_type == "structure" and art.level_classifier == "article"
    assert art.node_name == "Art. 165-4. COTTON RESEARCH AWARD FUND."
    kids = [n for n in nodes.values() if n.parent == art.node_id]
    assert sorted(n.number for n in kids) == ["1", "2", "3"]
    assert all(n.node_type == "content" and n.level_classifier == "section" for n in kids)
    assert nodes["us/tx/statutes/code=cv/title=4/chapter=11/article=165-4/section=1"].citation == \
        "Tex. Vernon's Civil Statutes art. 165-4, § 1"


def test_wl_sections_nest_under_their_article(emitted):
    nodes = _by_id(_parse("WL.1.htm", "WL", "Auxiliary Water Laws", emitted))
    assert "us/tx/statutes/code=wl/chapter=1/article=7621f/section=3-a" in nodes
    assert nodes["us/tx/statutes/code=wl/chapter=1/article=7621f"].node_type == "structure"


def test_cr_articles_without_sections_are_leaves(emitted):
    nodes = _by_id(_parse("CR.1.htm", "CR", "Code of Criminal Procedure", emitted))
    for num in ("1.01", "1.02", "1.025"):
        n = nodes[f"us/tx/statutes/code=cr/title=1/chapter=1/article={num}"]
        assert n.node_type == "content" and n.level_classifier == "article"
    n = nodes["us/tx/statutes/code=cr/title=1/chapter=1/article=1.01"]
    assert n.citation == "Tex. Code of Criminal Procedure art. 1.01"
    assert n.node_name == "Art. 1.01. SHORT TITLE."
    assert n.node_text is not None and "Code of Criminal Procedure" in list(n.node_text.paragraphs.values())[0].text
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 32


@pytest.mark.parametrize("head", ["Art. 4015b. NAME.", "Sec. 842a-1. X.", "Sec. 3-a. Y.", "Sec. 9A. Z."])
def test_section_regex_accepts_letter_suffixes(head):
    assert tx.SECTION_RE.match(head)


def test_empty_page_emits_nothing_and_code_is_not_marked_done(monkeypatch, tmp_path, emitted):
    monkeypatch.setattr(tx, "_fetch_pages", lambda code: [
        {"name": "CHAPTER 1. X", "url": "https://tcss.legis.texas.gov/resources/CV/htm/CV.22.1.htm"}])
    monkeypatch.setattr(tx, "_get", lambda url, as_json=False, **kw: "")
    monkeypatch.setattr(tx, "REQUEST_DELAY", 0)
    done = tmp_path / "state_tx_titles_done.txt"
    monkeypatch.setattr(tx, "_titles_done_path", lambda: done)
    corpus = Node(id="us/tx/statutes", node_type="structure", level_classifier="corpus", parent="us/tx")
    assert tx.scrape_code(corpus, "CV", "Vernon's Civil Statutes") == (1, 0)
    assert tx._run_code(corpus, ("CV", "Vernon's Civil Statutes")) == ("CV", "fail", "empty")
    assert not done.exists()


# ---------------------------------------------------------------------------
# Task 3: heading-driven hierarchy
# ---------------------------------------------------------------------------

def test_pe_sections_sit_under_title_and_chapter(emitted):
    nodes = _by_id(_parse("PE.19.htm", "PE", "Penal Code", emitted))
    assert nodes["us/tx/statutes/code=pe/title=5"].node_name == "TITLE 5. OFFENSES AGAINST THE PERSON"
    ch = nodes["us/tx/statutes/code=pe/title=5/chapter=19"]
    assert ch.node_name == "CHAPTER 19. CRIMINAL HOMICIDE" and ch.parent == "us/tx/statutes/code=pe/title=5"
    sec = nodes["us/tx/statutes/code=pe/title=5/chapter=19/section=19.02"]
    assert sec.parent == ch.node_id
    assert sec.citation == "Tex. Penal Code § 19.02"
    assert sec.node_name == "§ 19.02. MURDER."
    assert str(sec.link).endswith("/PE/htm/PE.19.htm#19.02")
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 6


def test_gv_subtitle_and_subchapter_levels(emitted):
    nodes = _by_id(_parse("GV.311.htm", "GV", "Government Code", emitted))
    base = "us/tx/statutes/code=gv/title=3/subtitle=b/chapter=311/subchapter=a"
    for n in ("311.001", "311.002", "311.003", "311.004", "311.005"):
        assert f"{base}/section={n}" in nodes
    assert "us/tx/statutes/code=gv/title=3/subtitle=b/chapter=311/subchapter=b/section=311.011" in nodes
    assert nodes[base].parent == "us/tx/statutes/code=gv/title=3/subtitle=b/chapter=311"
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 27


def test_pr_part_level(emitted):
    nodes = _by_id(_parse("PR.116.htm", "PR", "Property Code", emitted))
    assert "us/tx/statutes/code=pr/title=9/subtitle=b/chapter=116/subchapter=d/part=1/section=116.151" in nodes
    # a later subchapter closes the open part
    assert "us/tx/statutes/code=pr/title=9/subtitle=b/chapter=116/subchapter=e/section=116.201" in nodes
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 33


def test_centered_notes_are_not_headings_and_slash_chapter_token(emitted):
    nodes = _by_id(_parse("CV.71.6-1_2.htm", "CV", "Vernon's Civil Statutes", emitted))
    assert "us/tx/statutes/code=cv/title=71/chapter=6-1_2/article=4512.1" in nodes
    assert nodes["us/tx/statutes/code=cv/title=71/chapter=6-1_2"].number == "6-1_2"
    assert not any("impliedly repealed" in (n.node_name or "") for n in nodes.values())
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 6


def test_chapter_name_falls_back_to_api_entry_when_page_prints_none(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">TITLE 2. X</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 5.01. SHORT. Body text.</p></body></html>')
    code = _code_node("AG", "Agriculture Code")
    tx._parse_page(BeautifulSoup(html, "html.parser"), code, "AG", "Agriculture Code",
                   "https://tcss.legis.texas.gov/resources/AG/htm/AG.5.htm",
                   fallback_chapter_name="CHAPTER 5. FOO", seen=set())
    nodes = _by_id(emitted)
    assert nodes["us/tx/statutes/code=ag/title=2/chapter=5"].node_name == "CHAPTER 5. FOO"
    assert "us/tx/statutes/code=ag/title=2/chapter=5/section=5.01" in nodes


def test_title_node_emitted_once_across_pages(emitted):
    seen = set()
    code = _code_node("PE", "Penal Code")
    for _ in range(2):
        tx._parse_page(_soup("PE.19.htm"), code, "PE", "Penal Code",
                       "https://tcss.legis.texas.gov/resources/PE/htm/PE.19.htm",
                       fallback_chapter_name=None, seen=seen)
    assert sum(1 for n in emitted if n.node_id == "us/tx/statutes/code=pe/title=5") == 1


def test_spelled_out_section_one_nests_under_its_article(emitted):
    nodes = _by_id(_parse("WL.1.htm", "WL", "Auxiliary Water Laws", emitted))
    sec1 = nodes["us/tx/statutes/code=wl/chapter=1/article=7621f/section=1"]
    assert sec1.node_name == "§ 1. CONTRACTS FOR POLLUTION CONTROL; TERMS."
    assert "us/tx/statutes/code=wl/chapter=1/article=7621f/section=2" in nodes


def test_body_reference_to_a_section_is_not_a_heading(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">CHAPTER 1. X</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1.01. SHORT. Body text.</p>'
            '<p style="text-indent:13ex;" class="left">Section 2.01 of this code applies to this section.</p>'
            '<p style="text-indent:13ex;" class="left">Sec. 3 of the Act is repealed.</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("AG", "Agriculture Code"),
                   "AG", "Agriculture Code", "https://tcss.legis.texas.gov/resources/AG/htm/AG.1.htm",
                   fallback_chapter_name=None, seen=set())
    content = [n for n in emitted if n.node_type == "content"]
    assert [n.number for n in content] == ["1.01"]
    assert len(content[0].node_text.paragraphs) == 3


def test_part_headings_inside_an_article_nest_under_it(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">TITLE 109. PENSIONS</p>'
            '<p style="text-indent:7ex;" class="left">Art. 6243e. FIREMEN\'S RELIEF.</p>'
            '<p class="center" style="font-weight:bold;">PART 1. GENERAL PROVISIONS</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1.01. DEFINITIONS. In this Act:</p>'
            '<p class="center" style="font-weight:bold;">PART 2. BOARD</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 2.01. BOARD. The board is composed of:</p>'
            '<p style="text-indent:7ex;" class="left">Art. 6243f. POLICE RELIEF.</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1. APPLICATION. This Act applies to:</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("CV", "Vernon's Civil Statutes"),
                   "CV", "Vernon's Civil Statutes", "https://tcss.legis.texas.gov/resources/CV/htm/CV.109.0.htm",
                   fallback_chapter_name=None, seen=set())
    ids = [n.node_id for n in emitted]
    assert "us/tx/statutes/code=cv/title=109/article=6243e/part=1/section=1.01" in ids
    assert "us/tx/statutes/code=cv/title=109/article=6243e/part=2/section=2.01" in ids
    assert "us/tx/statutes/code=cv/title=109/article=6243f/section=1" in ids
    assert len(ids) == len(set(ids))


def test_part_under_subchapter_is_closed_by_the_next_part(emitted):
    nodes = _by_id(_parse("PR.116.htm", "PR", "Property Code", emitted))
    sub_d = "us/tx/statutes/code=pr/title=9/subtitle=b/chapter=116/subchapter=d"
    parts = [n for n in nodes.values() if n.level_classifier == "part" and n.parent == sub_d]
    assert sorted(n.number for n in parts) == ["1", "2", "3"]
    assert not any(n.level_classifier == "part" and n.parent != sub_d for n in nodes.values())


# ---------------------------------------------------------------------------
# Task 4: last_amended_year on content nodes
# ---------------------------------------------------------------------------

def test_section_core_metadata_carries_last_amended_year(emitted):
    nodes = _by_id(_parse("PE.19.htm", "PE", "Penal Code", emitted))
    sec = nodes["us/tx/statutes/code=pe/title=5/chapter=19/section=19.01"]
    assert sec.addendum.history.text.startswith("Acts 1973")
    assert sec.core_metadata == {"last_amended_year": 1993}


def test_section_without_history_has_no_core_metadata(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">CHAPTER 1. X</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1.01. SHORT. Body text only.</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("AG", "Agriculture Code"),
                   "AG", "Agriculture Code", "https://tcss.legis.texas.gov/resources/AG/htm/AG.1.htm",
                   fallback_chapter_name=None, seen=set())
    sec = _by_id(emitted)["us/tx/statutes/code=ag/chapter=1/section=1.01"]
    assert sec.core_metadata is None and sec.addendum is None


@pytest.mark.parametrize("text,num,expected", [
    ("Art. 6243h. MUNICIPAL PENSION SYSTEM IN CITIES OF 1,500,000 OR MORE.", "6243h",
     ("MUNICIPAL PENSION SYSTEM IN CITIES OF 1,500,000 OR MORE.", "")),
    ("Art. 974d-41. VALIDATION OF ACTS OF MUNICIPALITIES OF MORE THAN 1.5 MILLION.", "974d-41",
     ("VALIDATION OF ACTS OF MUNICIPALITIES OF MORE THAN 1.5 MILLION.", "")),
    ("Sec. 19.03. CAPITAL MURDER. A person commits an offense if", "19.03",
     ("CAPITAL MURDER.", "A person commits an offense if")),
    ("Sec. 311.001. SHORT TITLE. This chapter may be cited as the Code.", "311.001",
     ("SHORT TITLE.", "This chapter may be cited as the Code.")),
    ("Sec. 19.02. MURDER. (a) In this section:", "19.02", ("MURDER.", "(a) In this section:")),
    ("Sec. 7. Repealed by Acts 2019, 86th Leg.", "7", ("", "Repealed by Acts 2019, 86th Leg.")),
    ("Sec. 5.", "5", ("", "")),
    ("Sec. 2. U.S. CITIZENSHIP; PROOF. An applicant must", "2", ("U.S. CITIZENSHIP; PROOF.", "An applicant must")),
    ("Art. 6243i. UNITARY RETIREMENT SYSTEM FOR CERTAIN MUNICIPALITIES", "6243i",
     ("UNITARY RETIREMENT SYSTEM FOR CERTAIN MUNICIPALITIES", "")),
])
def test_split_head_caption_and_body(text, num, expected):
    assert tx._split_head(text, num) == expected


def test_centered_article_headings_inside_an_act_nest_under_it(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">TITLE 109. PENSIONS</p>'
            '<p style="text-indent:7ex;" class="left">Art. 6243e.2. FIREFIGHTERS\' RELIEF.</p>'
            '<p class="center" style="font-weight:bold;">ARTICLE 1. GENERAL PROVISIONS</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1.01. DEFINITIONS. In this Act:</p>'
            '<p class="center" style="font-weight:bold;">ARTICLE 2. BOARD</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 2.01. BOARD. The board is composed of:</p>'
            '<p style="text-indent:7ex;" class="left">Art. 6243f. POLICE RELIEF.</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 1. APPLICATION. This Act applies to:</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("CV", "Vernon's Civil Statutes"),
                   "CV", "Vernon's Civil Statutes", "https://tcss.legis.texas.gov/resources/CV/htm/CV.109.0.htm",
                   fallback_chapter_name=None, seen=set())
    ids = [n.node_id for n in emitted]
    assert "us/tx/statutes/code=cv/title=109/article=6243e.2/article=1/section=1.01" in ids
    assert "us/tx/statutes/code=cv/title=109/article=6243e.2/article=2/section=2.01" in ids
    assert "us/tx/statutes/code=cv/title=109/article=6243f/section=1" in ids
    assert len(ids) == len(set(ids))


def test_repealed_section_keeps_its_notice_text(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">CHAPTER 1. X</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 7. Repealed by Acts 2019, 86th Leg., R.S., Ch. 203 (H.B. 2820), Sec. 1.10(3), eff. September 1, 2019.</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 8. [Reserved]</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("AG", "Agriculture Code"),
                   "AG", "Agriculture Code", "https://tcss.legis.texas.gov/resources/AG/htm/AG.1.htm",
                   fallback_chapter_name=None, seen=set())
    nodes = _by_id(emitted)
    sec7 = nodes["us/tx/statutes/code=ag/chapter=1/section=7"]
    assert sec7.status == "reserved"
    assert list(sec7.node_text.paragraphs.values())[0].text.startswith("Repealed by Acts 2019")
    sec8 = nodes["us/tx/statutes/code=ag/chapter=1/section=8"]
    assert sec8.status == "reserved" and sec8.node_name == "§ 8."
    assert list(sec8.node_text.paragraphs.values())[0].text == "[Reserved]"


# ---------------------------------------------------------------------------
# Review fix: compact ARTICLE lines printed inside a section are body text
# ---------------------------------------------------------------------------

def test_compact_article_lines_inside_a_section_stay_in_its_text(emitted):
    nodes = _by_id(_parse("TX.141.htm", "TX", "Tax Code", emitted))
    base = "us/tx/statutes/code=tx/title=2/subtitle=d/chapter=141"
    sec = nodes[f"{base}/section=141.001"]
    paras = [p.text for p in sec.node_text.paragraphs.values()]
    assert "ARTICLE I. PURPOSES" in paras and "ARTICLE XII. CONSTRUCTION AND SEVERABILITY" in paras
    assert "MULTISTATE TAX COMPACT" in paras
    assert len(paras) > 100
    assert sec.addendum.history.text.startswith("Acts 1981")
    assert not any(n.level_classifier == "article" for n in nodes.values())
    assert nodes[f"{base}/section=141.002"].parent == base
    assert sum(1 for n in nodes.values() if n.node_type == "content") == 5


def test_version_note_after_history_is_not_appended_to_previous_section(emitted):
    html = ('<html><body><p class="center" style="font-weight:bold;">CHAPTER 32. FRAUD</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 32.55. X. Body one.</p>'
            '<p class="left">Acts 1973, 63rd Leg., ch. 399, Sec. 1, eff. Jan. 1, 1974.</p>'
            '<p class="center">For text of section as added by Acts 2025, 89th Leg., R.S., Ch. 817, see other Sec. 32.56.</p>'
            '<p style="text-indent:7ex;" class="left">Sec. 32.56. Y. Body two.</p></body></html>')
    tx._parse_page(BeautifulSoup(html, "html.parser"), _code_node("PE", "Penal Code"),
                   "PE", "Penal Code", "https://tcss.legis.texas.gov/resources/PE/htm/PE.32.htm",
                   fallback_chapter_name=None, seen=set())
    nodes = _by_id(emitted)
    paras = [p.text for p in nodes["us/tx/statutes/code=pe/chapter=32/section=32.55"].node_text.paragraphs.values()]
    assert paras == ["Body one."]
