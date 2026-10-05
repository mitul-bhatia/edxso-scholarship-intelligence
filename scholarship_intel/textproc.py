"""HTML / PDF -> clean structured text, plus the quote-locator used for evidence grounding."""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from rapidfuzz import fuzz

logging.getLogger("pypdf").setLevel(logging.ERROR)

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "button", "select", "option", "nav", "footer", "aside", "template"}
_BLOCK = {"p", "div", "li", "ul", "ol", "tr", "table", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
          "br", "header", "dl", "dt", "dd", "blockquote", "pre", "tbody", "thead", "main", "figure", "figcaption", "caption"}
_NAV_HINT = re.compile(r"(^|[\s_-])(nav|navbar|menu|breadcrumb|sidebar|footer|cookie|skip|accessib|social|share|megamenu|dropdown)([\s_-]|$)", re.I)


@dataclass
class Link:
    url: str
    text: str
    context: str = ""


@dataclass
class Doc:
    title: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    needs_ocr: bool = False
    meta_description: str = ""


def _clean_lines(raw: str) -> str:
    lines = []
    for ln in raw.splitlines():
        ln = re.sub(r"[ \t ]+", " ", ln).strip(" |\t")
        if ln:
            lines.append(ln)
    # collapse accidental consecutive duplicates (menus rendered twice)
    out = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return "\n".join(out)


def _is_nav(tag: Tag) -> bool:
    if tag.name in ("body", "html", "main", "article"):
        return False
    if tag.get("role") in ("navigation", "banner", "contentinfo", "search"):
        return True
    ident = " ".join(filter(None, [" ".join(tag.get("class", []) or []), tag.get("id", "") or ""]))
    return bool(ident and _NAV_HINT.search(ident))


def html_to_doc(html: str, base_url: str) -> Doc:
    soup = BeautifulSoup(html, "lxml")
    doc = Doc()
    if soup.title and soup.title.string:
        doc.title = re.sub(r"\s+", " ", soup.title.string).strip()
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if md and md.get("content"):
        doc.meta_description = md["content"].strip()

    # Links first (from the whole page – navigation links are useful for discovery, not for evidence)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        ctx = ""
        for depth, parent in enumerate(a.find_parents(["tr", "li", "p", "div", "section", "td"])):
            if depth > 6:
                break
            t = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))
            if len(txt) + 10 <= len(t) <= 500:      # nearest container that is a plausible "row", not the whole page
                ctx = t
                break
        doc.links.append(Link(urljoin(base_url, href), txt, ctx))

    for t in soup.find_all(["script", "style", "noscript", "template"]):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    root = None
    for sel in ("main", "[role=main]", "#content", "#main-content", "#maincontent", ".main-content", ".content-area", "article"):
        cand = soup.select_one(sel)
        if cand is not None and len(cand.get_text(" ", strip=True)) > 400:
            root = cand
            break
    root = root or soup.body or soup

    out: list[str] = []
    stack = [iter(root.children)]
    # iterative DFS with block-aware newlines
    closers: list[bool] = []
    while stack:
        try:
            node = next(stack[-1])
        except StopIteration:
            stack.pop()
            if closers and closers.pop():
                out.append("\n")
            continue
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            out.append(str(node))
            continue
        if not isinstance(node, Tag):
            continue
        if node.name in _SKIP_TAGS or _is_nav(node):
            continue
        blk = node.name in _BLOCK
        if blk:
            out.append("\n")
        if node.name in ("td", "th"):
            out.append(" | ")
        if node.name in ("h1", "h2", "h3", "h4"):
            h = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            if h and len(doc.headings) < 60:
                doc.headings.append(h)
        stack.append(iter(node.children))
        closers.append(blk)
    doc.text = _clean_lines("".join(out))
    return doc


def pdf_to_doc(content: bytes, max_pages: int = 25) -> Doc:
    from pypdf import PdfReader

    doc = Doc()
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
    except Exception:
        doc.needs_ocr = True
        return doc
    parts = []
    n = min(len(reader.pages), max_pages)
    for i in range(n):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n".join(parts)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # hyphenated line breaks
    doc.text = _clean_lines(text)
    doc.needs_ocr = len(doc.text) < 40 * max(1, n)
    try:
        meta = reader.metadata
        mt = str(meta.title).strip() if meta and meta.title else ""
        # metadata titles are often file names ("Guidelines2025-modified (1)", "Microsoft Word - x.docx"): ignore those
        if len(mt.split()) >= 3 and not re.search(r"\.(docx?|pdf|indd)\b|microsoft word|modified|\(\d\)|untitled|final", mt, re.I):
            doc.title = mt
    except Exception:
        pass
    if not doc.title and doc.text:
        first = [ln for ln in doc.text.splitlines()[:12] if len(ln) > 8]
        doc.title = " ".join(first[:2])[:160]
    # Gov PDFs rarely carry real links through pypdf; pull URLs that appear in the text.
    for m in re.finditer(r"https?://[^\s)>\]\"']+", doc.text):
        doc.links.append(Link(m.group(0).rstrip(".,;"), "", ""))
    return doc


# --------------------------------------------------------------------------------------
# Quote locator – the heart of "no quote, no value".
# --------------------------------------------------------------------------------------
_DASH = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOT = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord(" "): " ", ord("•"): " ", ord(""): " "}


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    folded: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_DASH).translate(_QUOT).lower():
            if c.isspace():
                if prev_space:
                    continue
                folded.append(" ")
                idx.append(i)
                prev_space = True
            else:
                folded.append(c)
                idx.append(i)
                prev_space = False
    return "".join(folded), idx


def fold_text(s: str) -> str:
    return _fold_with_map(s)[0].strip()


@dataclass
class Match:
    start: int
    end: int
    score: float          # 100 = exact (after whitespace/case/dash folding)
    exact: bool


class Haystack:
    """A page text prepared for locating quotes; offsets returned refer to the ORIGINAL stored text."""

    def __init__(self, text: str):
        self.text = text
        self.folded, self.idx = _fold_with_map(text)

    def find(self, quote: str, min_ratio: float = 92.0) -> Match | None:
        q = fold_text(quote)
        if len(q) < 4:
            return None
        pos = self.folded.find(q)
        if pos >= 0:
            return Match(self.idx[pos], self.idx[pos + len(q) - 1] + 1, 100.0, True)
        if len(q) < 25 or not self.folded:
            return None
        al = fuzz.partial_ratio_alignment(q, self.folded, score_cutoff=min_ratio)
        if al is None or al.score < min_ratio:
            return None
        s, e = al.dest_start, max(al.dest_start, al.dest_end - 1)
        s = min(s, len(self.idx) - 1)
        e = min(e, len(self.idx) - 1)
        start, end = self.idx[s], self.idx[e] + 1
        # snap to word boundaries so a fuzzy alignment never starts/ends mid-word
        while start > 0 and not self.text[start - 1].isspace():
            start -= 1
        while end < len(self.text) and not self.text[end].isspace():
            end += 1
        return Match(start, end, float(al.score), False)

    def window(self, start: int, end: int, pad: int = 0) -> str:
        return self.text[max(0, start - pad): end + pad]


def sentence_around(text: str, start: int, end: int, max_len: int = 420) -> tuple[str, int, int]:
    """Expand [start,end) to the surrounding sentence/line (used by rule extractors for quotes)."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    le = len(text) if le < 0 else le
    # inside the line, tighten to sentence boundaries when the line is long
    seg_start, seg_end = ls, le
    if le - ls > max_len:
        prev = max(text.rfind(". ", ls, start), text.rfind("; ", ls, start))
        seg_start = prev + 2 if prev >= 0 else max(ls, start - max_len // 2)
        nxt = text.find(". ", end, le)
        seg_end = nxt + 1 if nxt >= 0 else min(le, end + max_len // 2)
    return text[seg_start:seg_end].strip(), seg_start, seg_end
