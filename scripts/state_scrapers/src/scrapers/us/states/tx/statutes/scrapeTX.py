"""Texas (TX) statutes scraper.

Source: Texas Constitution and Statutes site (statutes.capitol.texas.gov)
API:    https://tcss.legis.texas.gov/api/  (undocumented JSON API backing the Angular SPA)
HTML:   https://tcss.legis.texas.gov/resources/{CODE}/htm/{CODE}.{N}.htm

Strategy (HTTP + bs4 only, no Selenium):
1. Iterate ALL 26 Texas codes (Agriculture through Water).
2. Fetch chapter list via GetStatuteArray API per code.
3. For each chapter, fetch the static HTML from tcss.legis.texas.gov/resources/.
4. Parse <p class="left"> paragraphs:
   - Paragraphs starting with "Sec. " open a new content node.
   - Subsequent indented paragraphs are added to the current section's NodeText.
   - Non-indented paragraphs (history lines) are treated as addendum history.
5. Build Node objects (structure: code, chapter; content: section) and insert each.

Parallelism: codes run in parallel via ThreadPoolExecutor; concurrency is
controlled by env var ``VAQUILL_TITLE_WORKERS`` (default 8). Code-level
resume is persisted in ``state_tx_titles_done.txt`` so a crashed/interrupted
run skips codes that have already completed (set ``VAQUILL_FORCE_RESCRAPE=1``
to override).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Resolve project root and add to sys.path (mirrors DE pattern).
# ---------------------------------------------------------------------------
current_file = Path(__file__).resolve()
src_directory = current_file.parent
while src_directory.name != "src" and src_directory.parent != src_directory:
    src_directory = src_directory.parent
project_root = src_directory.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import requests
from requests.exceptions import HTTPError, ConnectionError as ReqConnectionError

# Vaquill: shared HTTP layer (proxy + UA rotation + Cloudflare bypass + pool).
from vaquill_pipeline.http_client import fetch_html

from src.scrapers.us.states.tx.statutes.history import last_amended_year
from src.utils.pydanticModels import (
    Addendum,
    AddendumType,
    Node,
    NodeText,
)
from src.utils.scrapingHelpers import (
    insert_jurisdiction_and_corpus_node,
    insert_node,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COUNTRY = "us"
JURISDICTION = "tx"
CORPUS = "statutes"
TABLE_NAME = f"{COUNTRY}_{JURISDICTION}_{CORPUS}"

TCAS_API = "https://tcss.legis.texas.gov/api/"
TCAS_RESOURCES = "https://tcss.legis.texas.gov/resources/"
STATUTE_ORIGIN = "https://statutes.capitol.texas.gov"

# All 26 Texas codes. Verified 2026-05-11 by probing
# https://tcss.legis.texas.gov/resources/{CODE}/htm/{CODE}.*.htm.
# Note: the "Code Construction Act" is NOT a standalone code - it lives as
# chapters 311-312 inside the Government Code, so it is not listed here.
TX_CODES: List[Tuple[str, str]] = [
    ("AG", "Agriculture Code"),
    ("AL", "Alcoholic Beverage Code"),
    ("BC", "Business & Commerce Code"),
    ("BO", "Business Organizations Code"),
    ("CP", "Civil Practice and Remedies Code"),
    ("CR", "Code of Criminal Procedure"),
    ("CV", "Vernon's Civil Statutes"),
    ("ED", "Education Code"),
    ("EL", "Election Code"),
    ("ES", "Estates Code"),
    ("FA", "Family Code"),
    ("FI", "Finance Code"),
    ("GV", "Government Code"),
    ("HS", "Health and Safety Code"),
    ("HR", "Human Resources Code"),
    ("I1", "Insurance Code - Not Codified"),
    ("IN", "Insurance Code"),
    ("LA", "Labor Code"),
    ("LG", "Local Government Code"),
    ("NR", "Natural Resources Code"),
    ("OC", "Occupations Code"),
    ("PB", "Probate Code"),
    ("PE", "Penal Code"),
    ("PR", "Property Code"),
    ("PW", "Parks and Wildlife Code"),
    ("SD", "Special District Local Laws Code"),
    ("TX", "Tax Code"),
    ("TN", "Transportation Code"),
    ("UT", "Utilities Code"),
    ("WA", "Water Code"),
    ("WL", "Auxiliary Water Laws"),
]

RESERVED_KEYWORDS = ["[Repealed", "[Expired", "[Reserved", "Repealed"]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Origin": STATUTE_ORIGIN,
    "Referer": STATUTE_ORIGIN + "/",
}
REQUEST_DELAY = 0.3  # seconds between requests to be a polite scraper


# ---------------------------------------------------------------------------
# Mojibake fixer (TX site occasionally serves curly quotes / dashes mis-encoded
# when Content-Type lacks an explicit charset). Mirrors the DE pattern.
# ---------------------------------------------------------------------------

_MOJIBAKE_MARKERS = (
    "\xc2",      # Â prefix
    "\xe2\x80",  # â\x80 (curly quotes, em/en dashes)
    "\xe2\x82",
    "\xe2\x84",
    "\xe2\x86",
    "â€",
)


def _fix_encoding(s: str) -> str:
    if not s:
        return s
    if not any(m in s for m in _MOJIBAKE_MARKERS):
        return s
    try:
        fixed = s.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return s
    if sum(m in fixed for m in _MOJIBAKE_MARKERS) < sum(m in s for m in _MOJIBAKE_MARKERS):
        return fixed
    return s


# ---------------------------------------------------------------------------
# HTTP helpers (route through vaquill_pipeline.http_client for proxy + UA
# rotation + connection pooling + Cloudflare bypass).
# ---------------------------------------------------------------------------

def _get(url: str, as_json: bool = False, retries: int = 3, timeout: float = 30.0):
    """GET via fetch_html. Returns parsed JSON or text.

    ``timeout`` is the per-attempt HTTP timeout. Chapter-list endpoints for
    huge codes (SD has 1300+ chapters) need 60-120s; per-section HTML pages
    are small and fit in the default 30s.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            body = fetch_html(
                url,
                country_code="us",
                timeout=timeout,
                max_retries=2,
                referer=STATUTE_ORIGIN + "/",
                extra_headers={
                    "Accept": "application/json, text/html, */*",
                    "Origin": STATUTE_ORIGIN,
                },
            )
            if as_json:
                return json.loads(body)
            return body
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    if last_exc is not None:
        raise last_exc
    return None  # unreachable


def _get_soup(url: str) -> BeautifulSoup:
    html = _get(url, as_json=False)
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Resume: persist completed codes so a crashed run skips them on restart.
# ---------------------------------------------------------------------------

def _titles_done_path():
    from vaquill_pipeline.config import SETTINGS
    return SETTINGS.chunks_dir / "state_tx_titles_done.txt"


def _load_titles_done() -> set:
    path = _titles_done_path()
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text().splitlines() if l.strip()}


def _mark_title_done(code: str) -> None:
    path = _titles_done_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(f"{code}\n")
        fh.flush()




# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

# codeID lookup for TX codes (the TLC internal identifier the QuickSearch,
# StatutesByDate and StatuteCode endpoints key on). The live table is
# https://statutes.capitol.texas.gov/assets/QuickCodes.json and is fetched
# once per run by ``_code_ids``; this literal is the offline fallback,
# corrected against the live file on 2026-09-29.
TX_CODE_IDS: dict[str, str] = {
    "AG": "1", "AL": "2", "BC": "4", "BO": "32", "CN": "5", "CP": "6",
    "CR": "7", "CV": "8", "ED": "9", "EL": "10", "ES": "35", "FA": "11",
    "FI": "12", "GV": "13", "HR": "14", "HS": "15", "I1": "16", "IN": "17",
    "LA": "18", "LG": "19", "NR": "21", "OC": "22", "PB": "23", "PE": "24",
    "PR": "25", "PW": "26", "SD": "33", "TN": "27", "TX": "28",
    "UT": "29", "WA": "30", "WL": "31",
}
QUICKCODES_URL = STATUTE_ORIGIN + "/assets/QuickCodes.json"

_CODE_IDS_CACHE: Optional[dict] = None


def _build_code_ids(payload: dict) -> dict[str, str]:
    """{code: codeID} from the QuickCodes.json payload."""
    return {
        str(e["code"]).upper(): str(e["codeID"])
        for e in payload.get("StatuteCode", [])
        if e.get("code") and e.get("codeID") is not None
    }


def _code_ids() -> dict[str, str]:
    """Live code-id table, fetched once; falls back to ``TX_CODE_IDS``."""
    global _CODE_IDS_CACHE
    if _CODE_IDS_CACHE is None:
        try:
            ids = _build_code_ids(_get(QUICKCODES_URL, as_json=True))
        except Exception as exc:  # noqa: BLE001
            print(f"[TX] QuickCodes.json unavailable ({exc}); using the literal table", flush=True)
            ids = {}
        _CODE_IDS_CACHE = {**TX_CODE_IDS, **ids}
    return _CODE_IDS_CACHE


_PAGE_TOKEN_RE = re.compile(r"/([A-Z0-9]{2})/htm/\1\.([0-9A-Za-z][0-9A-Za-z._-]*)\.htm", re.IGNORECASE)


def _page_token(url: str, code: str) -> Optional[str]:
    """The ``N`` in ``.../{CODE}/htm/{CODE}.N.htm`` (``4.11``, ``6-1_2``), else None.

    GetStatuteArray returns ``.../CV/htm/CV...htm#`` for every Vernon's
    entry: no token, so the entry cannot be fetched.
    """
    m = _PAGE_TOKEN_RE.search(url or "")
    if not m or m.group(1).upper() != code.upper():
        return None
    return m.group(2)


def _heading_number(name: str) -> Optional[str]:
    """``CHAPTER 6-1/2. ABORTION`` -> ``6-1_2``; ``TITLE 22. BONDS`` -> ``22``."""
    m = re.match(r"^(?:TITLE|SUBTITLE|CHAPTER|SUBCHAPTER|PART|ARTICLE)\s+([0-9A-Za-z][0-9A-Za-z.\-/]*?)\.?(?:\s|$)", name.strip(), re.IGNORECASE)
    if not m:
        return None
    return m.group(1).replace("/", "_")


def _fetch_chapters_quicksearch(code: str) -> list:
    """Fallback chapter fetch via QuickSearch/PopulateChapterList.

    Used for codes too large for GetStatuteArray to enumerate within the
    request timeout (SD ~1300 chapters, etc.). Returns the same shape as
    ``_fetch_chapters`` ({name, url}) so callers don't care which path
    produced the list.
    """
    code_id = _code_ids().get(code)
    if not code_id:
        return []
    url = TCAS_API + f"QuickSearch/PopulateChapterList/{code_id}/CH"
    try:
        items = _get(url, as_json=True, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[TX] QuickSearch fallback failed for {code}: {exc}", flush=True)
        return []
    if not isinstance(items, list):
        return []
    # Normalize {text,value,url} -> {name,url}. ``url`` is the page token
    # as the site spells it ("SD.1", "CV.4.11"); use it verbatim.
    out = []
    for it in items:
        name = (it.get("text") or "").strip()
        rel = (it.get("url") or "").strip()
        full = f"{TCAS_RESOURCES}{code}/htm/{rel}.htm" if rel else ""
        if name and full:
            out.append({"name": name, "url": full})
    return out


def _pages_from_tree(titles: list, code: str) -> list:
    """Distinct HTML pages from a StatuteCode/GetTopLevelHeadings response.

    The tree is title -> (chapter | article). Chapters carry ``htmLink``
    (``/CV/htm/CV.4.11.htm``); when the link is broken (``/CV/htm/.htm``)
    the page is rebuilt from the title and chapter numbers. Articles link
    into their title's page (``/CV/htm/CV.1.0.htm#30``). Returns
    [{name, url}] in tree order, one entry per page.
    """
    out: list = []
    seen: set = set()

    def _add(name: str, url: str):
        if url and url not in seen:
            seen.add(url)
            out.append({"name": name, "url": url})

    for title in titles:
        title_name = (title.get("name") or "").strip()
        title_num = _heading_number(title_name)
        for child in title.get("children") or []:
            child_name = (child.get("name") or "").strip()
            link = (child.get("htmLink") or "").split("#")[0]
            if child_name.upper().startswith("CHAPTER"):
                if not _page_token(link, code):
                    ch_num = _heading_number(child_name)
                    link = f"/{code}/htm/{code}.{title_num}.{ch_num}.htm" if title_num and ch_num else ""
                _add(child_name, TCAS_RESOURCES + link.lstrip("/") if link else "")
            else:
                if not _page_token(link, code):
                    link = f"/{code}/htm/{code}.{title_num}.0.htm" if title_num else ""
                _add(title_name, TCAS_RESOURCES + link.lstrip("/") if link else "")
    return out


def _fetch_chapters_tree(code: str) -> list:
    """Page list via StatuteCode/GetTopLevelHeadings (the site's tree browser)."""
    code_id = _code_ids().get(code)
    if not code_id:
        return []
    url = TCAS_API + f"StatuteCode/GetTopLevelHeadings/%2F{code_id}/{code}/1/true/false"
    try:
        titles = _get(url, as_json=True, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[TX] GetTopLevelHeadings failed for {code}: {exc}", flush=True)
        return []
    if not isinstance(titles, list):
        return []
    return _pages_from_tree(titles, code)


def _fetch_chapters(code: str):
    """Return list of chapter dicts: [{name, ahid, hid, url}, ...].

    The GetStatuteArray endpoint takes 11 path params after the handler prefix:
      code / chapter / artSec / p1 / p2 / p3 / p4 / p5 / p6 / p7 / docType
    Passing code twice (code/code) with all others null returns the full chapter list.

    Falls back to QuickSearch/PopulateChapterList when GetStatuteArray returns
    an empty list (e.g. on timeout for huge codes like SD with 1300+ chapters).
    """
    url = (
        TCAS_API
        + "GetStatuteArray/GetStatuteArray/"
        + f"{code}/{code}/null/null/null/null/null/null/null/null/htm"
    )
    try:
        # 120s timeout - TX API is intermittently slow from non-residential
        # IPs and large codes (CR, ED, GV) take 20-90s to enumerate.
        chapters = _get(url, as_json=True, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[TX] GetStatuteArray failed for {code}: {exc} — trying QuickSearch fallback", flush=True)
        return _fetch_chapters_quicksearch(code)
    if not isinstance(chapters, list) or not chapters:
        print(f"[TX] GetStatuteArray returned empty for {code} — trying QuickSearch fallback", flush=True)
        return _fetch_chapters_quicksearch(code)
    return chapters


def _fetch_pages(code: str) -> list:
    """Pages to scrape for ``code``: [{name, url}] with a fetchable url each.

    GetStatuteArray entries without a page token (all of CV) are dropped;
    when nothing usable is left the site's tree browser is used instead.
    """
    entries = [e for e in _fetch_chapters(code) if _page_token(e.get("url", ""), code)]
    if entries:
        return entries
    print(f"[TX] no fetchable chapter urls for {code} — enumerating via GetTopLevelHeadings", flush=True)
    return _fetch_chapters_tree(code)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _clean_text(raw: str) -> str:
    text = raw.replace("\xa0", " ").replace(" ", " ")
    text = re.sub(r"\s+", " ", text)
    text = _fix_encoding(text)
    return text.strip()


def _is_history_line(text: str) -> bool:
    """Detect amendment/history lines (no text-indent or known prefixes)."""
    history_prefixes = (
        "Acts ", "Added by", "Amended by", "Redesignated", "Transferred",
        "Expired ", "Renumbered", "Reenacted",
    )
    return text.startswith(history_prefixes)


def _section_status(name: str) -> Optional[str]:
    for kw in RESERVED_KEYWORDS:
        if kw in name:
            return "reserved"
    return None


# Structure levels in nesting order. A heading at one level closes every
# deeper level. ``article`` is last because an "Art. N." paragraph that is
# followed by "Sec. N." paragraphs is a container for those sections.
LEVELS: List[str] = ["title", "subtitle", "chapter", "subchapter", "part", "article"]

# Centered, bold heading: "TITLE 5. OFFENSES AGAINST THE PERSON",
# "SUBCHAPTER A. GENERAL PROVISIONS", "CHAPTER 6-1/2. ABORTION".
HEADING_RE = re.compile(
    r"^(TITLE|SUBTITLE|CHAPTER|SUBCHAPTER|PART|ARTICLE)\s+([0-9A-Z][0-9A-Za-z.\-/]*?)\.\s+(\S.*)$"
)
# Section / article paragraph: "Sec. 19.01. TYPES ...", "Art. 4015b. NAME.",
# "Sec. 842a-1. ...", "Sec. 3-a. ...", "Sec. 9A. ...". The first section of
# an uncodified act is printed "Section 1." in full. The number must be
# followed by a period and a space (or end), so a body paragraph such as
# "Section 2.01 of this code applies ..." is not read as a heading.
SECTION_RE = re.compile(r"^(Sec\.|Section|Art\.)\s+(\d[\dA-Za-z.\-]*)\.(?=\s|$)")
_HEAD_PREFIX_RE = re.compile(r"^(?:Sec\.|Section|Art\.)\s+[\dA-Za-z.\-]+\.\s*")


def _split_head(text: str, raw_num: str):
    """(caption, body) for a section/article paragraph.

    "Sec. 19.02. MURDER. (a) In this section:" -> ("MURDER.", "(a) In this
    section:"). The caption is the run of ALL-CAPS words (digits and
    punctuation allowed: "CITIES OF 1,500,000 OR MORE.", "U.S. CITIZENSHIP.")
    up to the last one ending in a period before the first word with a
    lowercase letter or an opening parenthesis; "" when there is none.
    """
    rest = _HEAD_PREFIX_RE.sub("", text, count=1)
    words = rest.split(" ")
    i = 0
    while i < len(words) and words[i] and not re.search(r"[a-z]", words[i]) and not words[i].startswith("("):
        i += 1
    if i < len(words):
        # Body follows: the caption ends at its last period. A caption that
        # fills the whole paragraph ("Art. 6243i. UNITARY RETIREMENT SYSTEM")
        # may lack the period and is kept whole.
        while i > 0 and not words[i - 1].endswith("."):
            i -= 1
    caption = " ".join(words[:i]).strip()
    body = " ".join(words[i:]).strip()
    return caption, _clean_text(body)


class _PageParser:
    """Stateful walk over one chapter page's paragraphs in document order.

    Emits structure nodes for headings (title, subtitle, chapter, subchapter,
    part, article) as they open, and content nodes for sections and leaf
    articles. Node ids are the open-level stack joined:
    ``code=pr/title=9/subtitle=b/chapter=116/subchapter=d/part=1/section=116.151``.
    """

    def __init__(self, code_node: Node, code: str, code_name: str, page_url: str, seen: set):
        self.code_node = code_node
        self.code = code
        self.code_name = code_name
        self.page_url = page_url
        self.seen = seen              # structure ids already inserted for this code
        # (level, number, node, container): container marks an "Art. N."
        # paragraph promoted to hold sections, parts or ARTICLE headings.
        self.stack: List[Tuple[str, str, Node, bool]] = []
        self.content_emitted = 0
        # Pending section (or leaf-article candidate)
        self.cur_kind: Optional[str] = None      # "section" | "article"
        self.cur_number: Optional[str] = None
        self.cur_name: Optional[str] = None
        self.cur_caption: str = ""
        self.cur_text: Optional[NodeText] = None
        self.cur_history: str = ""
        self.cur_anchor: str = ""
        self.cur_status: Optional[str] = None

    # -- structure -------------------------------------------------------

    @property
    def parent_node(self) -> Node:
        return self.stack[-1][2] if self.stack else self.code_node

    def _container_index(self) -> Optional[int]:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][3]:
                return i
        return None

    def _close_to(self, level: str, container: bool = False) -> None:
        """Pop the open levels a new ``level`` heading closes.

        Canonical nesting is title > subtitle > chapter > subchapter > part >
        article, and a heading closes every level at or below its own rank.
        Uncodified acts (CV, WL) print an "Art. N." paragraph that acts as
        a container: PART and ARTICLE headings inside it nest under it
        (closing only their peers inside the same act), a new "Art." closes
        the whole act, and any title/subtitle/chapter/subchapter heading
        closes it too.
        """
        inside = self._container_index()
        if container:
            if inside is not None:
                del self.stack[inside:]
                return
        elif inside is not None and level in ("part", "article"):
            rank = LEVELS.index(level)
            while len(self.stack) - 1 > inside and LEVELS.index(self.stack[-1][0]) >= rank:
                self.stack.pop()
            return
        rank = LEVELS.index(level)
        while self.stack and LEVELS.index(self.stack[-1][0]) >= rank:
            self.stack.pop()

    def open_level(self, level: str, number: str, name: str, link: Optional[str] = None,
                   container: bool = False) -> Node:
        self.flush()
        self._close_to(level, container=container)
        parent = self.parent_node
        node_id = f"{parent.node_id}/{level}={number}"
        node = Node(
            id=node_id,
            link=link or self.page_url,
            top_level_title=self.code.lower(),
            node_type="structure",
            level_classifier=level,
            number=number,
            node_name=name,
            parent=parent.node_id,
        )
        if node_id not in self.seen:
            self.seen.add(node_id)
            insert_node(node, TABLE_NAME, ignore_duplicate=True, debug_mode=True)
        self.stack.append((level, number, node, container))
        return node

    def heading(self, text: str) -> bool:
        m = HEADING_RE.match(text)
        if not m:
            return False
        level = m.group(1).lower()
        number = m.group(2).replace("/", "_").lower()
        if level in ("part", "article") and self.cur_kind == "article":
            # "Art. 6243e. NAME." followed by "PART 1. ..." or "ARTICLE 1. ...":
            # the act is a container for those headings (and their sections).
            self.promote_article()
        self.open_level(level, number, text)
        return True

    # -- content ---------------------------------------------------------

    def start(self, kind: str, raw_num: str, text: str, anchor: str) -> None:
        """Begin a section (kind="section") or article (kind="article")."""
        self.flush()
        if kind == "article":
            # A new act closes any open act container and its contents.
            self._close_to("article", container=True)
        caption, body = _split_head(text, raw_num)
        self.cur_kind = kind
        self.cur_number = raw_num
        self.cur_caption = caption
        self.cur_anchor = anchor or raw_num
        self.cur_status = _section_status(caption) or _section_status(body[:80])
        prefix = "Art." if kind == "article" else "§"
        self.cur_name = f"{prefix} {raw_num}. {caption}".rstrip() if caption else f"{prefix} {raw_num}."
        self.cur_text = NodeText()
        if body:
            self.cur_text.add_paragraph(body)
        self.cur_history = ""

    def promote_article(self) -> None:
        """The pending article is followed by a Sec.: make it a container."""
        num, name = self.cur_number, self.cur_name
        link = f"{self.page_url}#{self.cur_anchor}" if self.cur_anchor else self.page_url
        if self.cur_text and self.cur_text.paragraphs:
            print(f"  [note] article {num} has body text before its sections; text dropped", flush=True)
        self._reset_current()
        self.open_level("article", num.lower(), name, link=link, container=True)

    def add_body(self, text: str, indented: bool) -> None:
        if self.cur_number is None:
            return
        if not indented or _is_history_line(text):
            self.cur_history += text + "\n"
        else:
            if self.cur_text is None:
                self.cur_text = NodeText()
            self.cur_text.add_paragraph(text)

    def _reset_current(self) -> None:
        self.cur_kind = None
        self.cur_number = None
        self.cur_name = None
        self.cur_caption = ""
        self.cur_text = None
        self.cur_history = ""
        self.cur_anchor = ""
        self.cur_status = None

    def flush(self) -> None:
        """Emit the pending section / leaf article, if any."""
        if self.cur_number is None:
            return
        parent = self.parent_node
        level = "article" if self.cur_kind == "article" else "section"
        node_id = f"{parent.node_id}/{level}={self.cur_number}"
        citation = self._citation(level, self.cur_number)
        link = f"{self.page_url}#{self.cur_anchor}" if self.cur_anchor else self.page_url

        addendum = None
        history = self.cur_history.strip()
        if history:
            addendum = Addendum(history=AddendumType(type="history", text=history))

        node = Node(
            id=node_id,
            link=link,
            citation=citation,
            top_level_title=self.code.lower(),
            node_type="content",
            level_classifier=level,
            number=self.cur_number,
            node_name=self.cur_name,
            parent=parent.node_id,
            status=self.cur_status,
            # Repealed/reserved sections keep their notice ("Repealed by Acts
            # 2019, ...") so the year and the reason survive downstream.
            node_text=self.cur_text if (self.cur_text and self.cur_text.paragraphs) else None,
            addendum=addendum,
            core_metadata=_core_metadata(history),
        )
        insert_node(node, TABLE_NAME, ignore_duplicate=True, debug_mode=True)
        self.content_emitted += 1
        self._reset_current()

    def _citation(self, level: str, number: str) -> str:
        if level == "article":
            return f"Tex. {self.code_name} art. {number}"
        for lvl, num, _node, is_container in reversed(self.stack):
            if lvl == "article" and is_container:
                return f"Tex. {self.code_name} art. {num}, § {number}"
        return f"Tex. {self.code_name} § {number}"


def _core_metadata(history: str) -> Optional[dict]:
    """Per-node metadata derived from the history line."""
    year = last_amended_year(history)
    return {"last_amended_year": year} if year is not None else None


def _parse_page(
    soup: BeautifulSoup,
    code_node: Node,
    code: str,
    code_name: str,
    page_url: str,
    fallback_chapter_name: Optional[str] = None,
    seen: Optional[set] = None,
) -> int:
    """Walk every <p> of a chapter page; emit structure + content nodes.

    Returns the number of content nodes emitted. ``fallback_chapter_name``
    (the API's "CHAPTER N. NAME" for this page) is used only when the page
    prints no CHAPTER heading of its own. ``seen`` is the per-code set of
    structure ids already inserted, so title/subtitle nodes repeated on
    every chapter page are emitted once.
    """
    parser = _PageParser(code_node, code, code_name, page_url, seen if seen is not None else set())
    paras = soup.find_all("p")

    texts = []
    for p in paras:
        classes = p.get("class") or []
        text = _clean_text(p.get_text())
        if not text:
            continue
        texts.append((p, classes, text))

    has_chapter_heading = any(
        "center" in c and HEADING_RE.match(t) and t.upper().startswith("CHAPTER")
        for _p, c, t in texts
    )
    if not has_chapter_heading and fallback_chapter_name:
        num = _heading_number(fallback_chapter_name)
        if num and fallback_chapter_name.upper().startswith("CHAPTER"):
            # Open it after any title/subtitle headings that precede the first
            # section, so it nests under them.
            pending_fallback = (num.lower(), fallback_chapter_name)
        else:
            pending_fallback = None
    else:
        pending_fallback = None

    for p, classes, text in texts:
        if "center" in classes:
            if parser.heading(text):
                continue
            # Other centered lines (code banner, version notes, editorial
            # notes such as "The following article was held ...") are not
            # headings and are skipped.
            continue

        sec_match = SECTION_RE.match(text)
        if sec_match:
            if pending_fallback:
                parser.open_level("chapter", pending_fallback[0], pending_fallback[1])
                pending_fallback = None
            kind = "article" if sec_match.group(1) == "Art." else "section"
            raw_num = sec_match.group(2).rstrip(".")
            if kind == "section" and parser.cur_kind == "article":
                parser.promote_article()
            parser.start(kind, raw_num, text, p.get("id", ""))
            continue

        style = p.get("style", "")
        parser.add_body(text, indented="text-indent" in style)

    parser.flush()
    return parser.content_emitted


# ---------------------------------------------------------------------------
# Main traversal
# ---------------------------------------------------------------------------

def _scrape_page(entry: dict, code_node: Node, code: str, code_name: str, seen: set) -> int:
    """Fetch and parse one chapter page. Returns content nodes emitted."""
    name: str = (entry.get("name") or "").strip()
    page_url: str = (entry.get("url") or "").strip()
    if not _page_token(page_url, code):
        print(f"  [skip] unrecognised page url: name={name!r} url={page_url!r}", flush=True)
        return 0

    time.sleep(REQUEST_DELAY)
    try:
        html = _get(page_url, as_json=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  [error] failed to fetch {page_url}: {exc}", flush=True)
        return 0
    if not (html or "").strip():
        print(f"  [empty] {page_url} served no content", flush=True)
        return 0
    soup = BeautifulSoup(html, "html.parser")
    return _parse_page(soup, code_node, code, code_name, page_url,
                       fallback_chapter_name=name or None, seen=seen)


def scrape_code(corpus_node: Node, code: str, code_name: str) -> Tuple[int, int]:
    """Scrape every page of one TX code. Returns (pages_seen, sections_emitted)."""
    code_node_id = f"{corpus_node.node_id}/code={code.lower()}"
    code_node = Node(
        id=code_node_id,
        link=f"{STATUTE_ORIGIN}/?link={code}",
        top_level_title=code.lower(),
        node_type="structure",
        level_classifier="code",
        number=code.lower(),
        node_name=code_name,
        parent=corpus_node.node_id,
    )
    insert_node(code_node, TABLE_NAME, ignore_duplicate=True, debug_mode=True)

    print(f"[TX] Fetching page list for code={code} ({code_name})", flush=True)
    pages = _fetch_pages(code)
    print(f"[TX] Found {len(pages)} pages in {code}", flush=True)

    seen: set = set()
    emitted = 0
    for entry in pages:
        print(f"  > {entry.get('name', '?')!r}", flush=True)
        emitted += _scrape_page(entry, code_node, code, code_name, seen)
    return len(pages), emitted


def _run_code(corpus_node: Node, item: Tuple[str, str]) -> Tuple[str, str, Optional[str]]:
    """Scrape one code; mark it done only when it produced pages and sections."""
    code, name = item
    try:
        pages, sections = scrape_code(corpus_node, code, name)
    except Exception as e:  # noqa: BLE001
        return (code, "fail", str(e)[:200])
    if pages == 0 or sections == 0:
        print(f"[TX] {code}: {pages} pages, {sections} sections — not marking done", flush=True)
        return (code, "fail", "empty")
    _mark_title_done(code)
    return (code, "ok", None)


def _work_items(titles_done: set, only_codes: str) -> List[Tuple[str, str]]:
    """Codes to scrape this run: ``TX_CODES`` minus the resume set, then
    narrowed to ``only_codes`` (comma-separated, case-insensitive, from
    ``TX_ONLY_CODES``) when that is non-empty. Unknown codes are ignored.
    """
    work = [(code, name) for code, name in TX_CODES if code not in titles_done]
    wanted = {c.strip().upper() for c in only_codes.split(",") if c.strip()}
    if wanted:
        work = [(code, name) for code, name in work if code in wanted]
    return work


def main():
    """Walk all Texas codes in parallel.

    Each code is fully independent (separate code-node subtree, separate API
    chapter list, separate static HTML files), so we hand them to a
    ThreadPoolExecutor. Concurrency is controlled by ``VAQUILL_TITLE_WORKERS``
    (default 8). The HTTP layer (vaquill_pipeline.http_client) uses a shared
    keep-alive connection pool so parallel requests don't open new sockets.

    Resume: completed codes are persisted in ``state_tx_titles_done.txt`` and
    skipped on re-runs. Set ``VAQUILL_FORCE_RESCRAPE=1`` to override.
    ``TX_ONLY_CODES=PE,CV`` restricts the run to those codes.
    """
    corpus_node: Node = insert_jurisdiction_and_corpus_node(COUNTRY, JURISDICTION, CORPUS)

    titles_done = set() if os.environ.get("VAQUILL_FORCE_RESCRAPE") else _load_titles_done()
    if titles_done:
        print(
            f"[scrapeTX] resume: {len(titles_done)} codes already done: "
            f"{sorted(titles_done)}",
            flush=True,
        )

    work = _work_items(titles_done, os.environ.get("TX_ONLY_CODES", ""))

    workers = int(os.environ.get("VAQUILL_TITLE_WORKERS", "8"))
    print(
        f"[scrapeTX] running {len(work)} codes with {workers} parallel workers",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(_run_code, corpus_node, item) for item in work):
            code, status, err = fut.result()
            if status == "fail":
                print(f"[scrapeTX] code {code}: {status}: {err}", flush=True)
            else:
                print(f"[scrapeTX] code {code}: {status}", flush=True)


if __name__ == "__main__":
    main()
