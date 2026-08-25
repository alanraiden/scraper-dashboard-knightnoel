"""
utils.py — Shared scraping utilities
=====================================
Text cleaning, watermark stripping, chapter number inference,
and the shared selector lists.  Import from here rather than
duplicating across adapters.
"""

import re
import json
from urllib.parse import urljoin


# ── Content selectors (tried in order) ───────────────────────────────────────

CONTENT_SELECTORS = [
    "div.reading-content", "div.text-left", "div#chapter-content",
    "div.chapter-content", "div.entry-content", "article.post-content",
    "div.chapter-body", "div#content", "div.storytext", "div.chapter",
    "div.post-content", "div.main-content", "div#novel-content",
    "div.chapter-inner-content", "div[class*='chapter-content']",
    "div[class*='chapter-text']",
]

NEXT_SELECTORS = [
    "a.next_page", "a[rel='next']", "a.next-chap", "a#next_chap",
    "a.btn-next", "a[title*='Next']", "a[title*='next']",
    "a[class*='next']", ".nav-next a", ".chapter-nav .next a",
    "a[href*='chapter']:not([class*='prev'])",
    "[class*='next-chapter'] a", "[class*='nextChapter'] a",
    "[class*='chapter-next'] a", "[id*='next-chapter']",
    "[data-testid*='next'] a", "[aria-label*='Next chapter']",
    "[aria-label*='next chapter']",
]

PREV_SELECTORS = [
    "a.prev_page", "a[rel='prev']", "a.prev-chap", "a#prev_chap",
    "a.btn-prev", "a[title*='Prev']", "a[title*='prev']", "a[title*='Previous']",
    "a[class*='prev']", ".nav-prev a", ".chapter-nav .prev a",
    "[class*='prev-chapter'] a", "[class*='prevChapter'] a",
    "[class*='chapter-prev'] a", "[id*='prev-chapter']",
    "[data-testid*='prev'] a", "[aria-label*='Previous chapter']",
    "[aria-label*='previous chapter']",
]


# ── Watermark patterns ────────────────────────────────────────────────────────

JUNK_PATTERNS = [
    r"^prev(ious)?\s+chapter$", r"^next\s+chapter$",
    r"^←\s*(prev|previous|back)", r"^(next|forward)\s*→",
    r"^chapter\s+navigation", r"^(prev|previous|next)\s*$",
    r"use arrow keys", r"\(or\s+a\s*/\s*d\)",
    r"^add\s+to\s+(library|bookmarks?|reading\s+list)$",
    r"^\d+\s+comments?$", r"^comments?$", r"^reply$", r"^like$",
    r"^rate\s+this\s+(chapter|novel)$", r"^table\s+of\s+contents?$",
    r"translated\s+by", r"translation\s+by", r"translator[:\s]",
    r"t\.?l\.?\s*note", r"tl\s*note", r"to\s+support\s+us",
    r"support\s+the\s+(translation|translator|author)",
    r"visit\s+our\s+(website|site|page)",
    r"read\s+(more|ahead|the\s+latest)\s+(at|on)",
    r"https?://", r"\w+\.(com|net|org|io|xyz|online|site)\b",
    r"patreon\.com", r"ko-?fi\.com", r"buy\s+me\s+a\s+coffee",
    r"if\s+you('re|\s+are)\s+reading\s+this",
    r"this\s+chapter\s+was\s+(stolen|scraped|taken)",
    r"join\s+our\s+(discord|group|server)", r"discord\.gg/",
    r"^[\-_\*=~]{3,}$",
]

BLOCK_MARKERS = [
    r"(translator|tl|editor|proofreader)'?s?\s+note",
    r"t\.?l\.?\s*note",
    r"note\s+from\s+(the\s+)?(translator|editor)",
]

# ── Paywall / UI page detection ───────────────────────────────────────────────
# These phrases appear on locked chapter pages, login walls, "back to novel"
# pages, and reading-settings UI pages — NOT in real chapter content.
# is_junk_page() uses these to reject the entire page before upload.

_PAYWALL_SIGNALS = [
    # Lock / premium indicators
    r"🔒\s*premium\s+chapter",
    r"premium\s+chapter",
    r"this\s+is\s+a\s+premium\s+chapter",
    r"unlock\s+this\s+chapter",
    r"unlock\s+chapter",
    r"chapter\s+is\s+locked",
    r"locked\s+chapter",
    r"becomes\s+free\s+in",
    r"available\s+in\s+\d+\s+day",
    r"free\s+in\s+\d+\s+(hour|day|minute)",

    # Login / auth walls
    r"login\s+or\s+create\s+an\s+account",
    r"log\s+in\s+to\s+(read|unlock|access|view)",
    r"sign\s+in\s+to\s+(read|unlock|continue)",
    r"create\s+an\s+account\s+to",
    r"you\s+need\s+to\s+be\s+logged\s+in",
    r"please\s+log\s+in",
    r"not\s+a\s+member\?",
    r"join\s+us\s*🔓",
    r"unlock\s+all\s+chapters",
    r"get\s+instant\s+access",
    r"join\s+now\s*🔓",
    r"members?\s+only",
    r"subscribe\s+to\s+(read|unlock|access)",
    r"patreon\s+subscription",
    r"unlock\s+(with|using)\s+(coins?|points?|patreon)",
    r"want\s+to\s+read\s+other\s+chapters",
    r"back\s+to\s+novel",

    # Reading-settings / UI chrome pages
    r"customize\s+reading\s+experience",
    r"reading\s+settings",
    r"font\s+size.*font\s+family",
    r"font\s+family.*font\s+size",
    r"^theme$",

    # Navigation-only / error pages
    r"^page\s+not\s+found$",
    r"^404",
    r"^403",
    r"chapter\s+not\s+found",
    r"this\s+chapter\s+does\s+not\s+exist",

    # Knight Novel / CMS placeholder text
    # Emitted by KN when a chapter page exists but has no real content yet.
    r"this\s+is\s+placeholder\s+text",
    r"real\s+chapter\s+content\s+will\s+appear\s+here",
    r"added\s+via\s+the\s+admin\s+panel",
    r"manual\s+entry\s+or\s+bulk\s+import",
    r"MONGODB_URI\s+is\s+configured",
]

_compiled_paywall = [re.compile(p, re.I) for p in _PAYWALL_SIGNALS]


def is_junk_page(text: str, min_words: int = 150) -> tuple[bool, str]:
    """
    Return (is_junk, reason) for a block of extracted text.

    A page is considered junk if:
      1. It contains a paywall/login/UI signal phrase, OR
      2. It has fewer than min_words words after cleaning.

    The default min_words=150 is intentionally conservative — a very short
    chapter might have 200 words, so 150 gives a comfortable margin while
    still catching pure UI pages (which typically have 20-60 words).

    Pass min_words=0 to disable the word-count check and only use signals.
    """
    if not text:
        return True, "empty content"

    # Check paywall/UI signals — scan first 2000 chars since these phrases
    # always appear near the top of a junk page
    sample = text[:2000]
    for pattern in _compiled_paywall:
        m = pattern.search(sample)
        if m:
            return True, f"paywall/UI signal: '{m.group(0).strip()}'"

    # Word count check
    if min_words > 0:
        wc = len(text.split())
        if wc < min_words:
            return True, f"too short ({wc} words, minimum {min_words})"

    return False, ""

_compiled_junk   = [re.compile(p, re.I) for p in JUNK_PATTERNS]
_compiled_blocks = [re.compile(p, re.I) for p in BLOCK_MARKERS]


# ── Text helpers ──────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(l.strip() for l in text.splitlines()).strip()


def strip_watermarks(text: str, extra_patterns: list = None) -> str:
    """
    Remove watermark lines from content.
    Pass extra_patterns (list of compiled re patterns) for job-specific phrases.
    """
    junk = _compiled_junk + (extra_patterns or [])
    lines = text.splitlines()
    out   = []
    skip  = False
    for line in lines:
        s = line.strip()
        if any(p.search(s) for p in _compiled_blocks): skip = True
        if skip and s == "":                            skip = False; continue
        if skip:                                        continue
        if s and any(p.search(s) for p in junk):       continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def infer_chapter_number(title: str, fallback: int) -> int:
    patterns = [
        r"chapter[\s\-_#]?(\d+)",
        r"ch[\s\-_.]?(\d+)",
        r"#(\d+)",
        r"ep(?:isode)?[\s\-_.]?(\d+)",
        r"(\d+)",
    ]
    for p in patterns:
        m = re.search(p, title, re.I)
        if m:
            return int(m.group(1))
    return fallback


# ── JSON tree walker ──────────────────────────────────────────────────────────

CONTENT_KEYS = [
    "content", "body", "text", "chapter", "chapterContent",
    "chapter_content", "chapterBody", "novelContent", "story",
    "passage", "rawContent", "html", "rendered",
]


def walk_json_for_text(obj, depth: int = 0, min_words: int = 80) -> str | None:
    """
    Recursively search an arbitrary JSON structure for a long prose string.
    Returns the first string that looks like chapter content, or None.
    """
    if depth > 12:
        return None
    if isinstance(obj, str) and len(obj) > 300:
        cleaned = re.sub(r"<[^>]+>", " ", obj)
        cleaned = clean_text(cleaned)
        if len(cleaned.split()) >= min_words:
            return cleaned
    if isinstance(obj, dict):
        for key in CONTENT_KEYS:
            if key in obj:
                r = walk_json_for_text(obj[key], depth + 1, min_words)
                if r:
                    return r
        for v in obj.values():
            r = walk_json_for_text(v, depth + 1, min_words)
            if r:
                return r
    if isinstance(obj, list):
        for item in obj:
            r = walk_json_for_text(item, depth + 1, min_words)
            if r:
                return r
    return None


def pick_best_candidate(candidates: list[str], log_fn=None, source: str = "") -> str | None:
    """Pick the longest candidate string, parse HTML if needed, strip watermarks."""
    if not candidates:
        return None
    best = max(candidates, key=len)
    if "<p>" in best or "<br" in best or "<div" in best:
        from bs4 import BeautifulSoup
        best = clean_text(BeautifulSoup(best, "lxml").get_text(separator="\n"))
    else:
        best = clean_text(best)
    if len(best) > 100:
        if log_fn:
            log_fn(f"[{source}] Extracted {len(best.split())} words", "dim")
        return strip_watermarks(best)
    return None


def collect_text_values(val, depth: int = 0) -> list[str]:
    """
    Collect all long text strings from an arbitrary JSON structure.
    Returns a flat list — caller picks the best one.
    """
    if depth > 8:
        return []
    texts = []
    if isinstance(val, str):
        v = val.strip()
        if len(v) > 80 and not v.startswith("http") and not v.startswith("/"):
            texts.append(v)
    elif isinstance(val, list):
        for item in val:
            texts.extend(collect_text_values(item, depth + 1))
    elif isinstance(val, dict):
        for key in CONTENT_KEYS:
            if key in val:
                texts.extend(collect_text_values(val[key], depth + 1))
        for k, v in val.items():
            if k not in CONTENT_KEYS:
                texts.extend(collect_text_values(v, depth + 1))
    return texts


# ── TipTap / ProseMirror ──────────────────────────────────────────────────────

def extract_tiptap_doc(node, depth: int = 0) -> str:
    """
    Recursively extract plain text from a TipTap/ProseMirror JSON document.
    Returns a newline-separated string.
    """
    if depth > 20 or not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    children  = node.get("content") or []
    if node_type == "text":
        return node.get("text", "")
    parts  = [extract_tiptap_doc(child, depth + 1) for child in children]
    joined = "".join(parts)
    if node_type in ("paragraph", "heading", "blockquote", "listItem",
                     "bulletList", "orderedList", "horizontalRule"):
        return joined + "\n"
    return joined


# ── Generic next/prev URL finders (shared fallback) ──────────────────────────

def find_next_url_generic(soup, current_url: str) -> str | None:
    for sel in NEXT_SELECTORS:
        try:
            a = soup.select_one(sel)
            if a and a.get("href"):
                href = a["href"]
                if href.startswith("#") or href.startswith("javascript:"):
                    continue
                return urljoin(current_url, href)
        except Exception:
            continue
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True).lower()
        if txt in ("next chapter", "next", "next chap", "next →", "next >", ">"):
            href = a["href"]
            if not href.startswith("#") and not href.startswith("javascript:"):
                return urljoin(current_url, href)
    return None


def find_prev_url_generic(soup, current_url: str) -> str | None:
    for sel in PREV_SELECTORS:
        try:
            a = soup.select_one(sel)
            if a and a.get("href"):
                href = a["href"]
                if href.startswith("#") or href.startswith("javascript:"):
                    continue
                return urljoin(current_url, href)
        except Exception:
            continue
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True).lower()
        if txt in ("previous chapter", "previous", "prev chapter", "prev chap", "← prev", "< prev", "<"):
            href = a["href"]
            if not href.startswith("#") and not href.startswith("javascript:"):
                return urljoin(current_url, href)
    return None


def infer_next_url_from_pattern(current_url: str, soup, log_fn=None) -> str | None:
    """
    For JS-rendered sites: infer the next chapter URL from the URL pattern.
    e.g. /chapter-1/ → /chapter-2/
    """
    m = re.search(r"(chapter[-_])([0-9]+)(/?(?:[^/]*)?)$", current_url, re.I)
    if m:
        next_num  = int(m.group(2)) + 1
        candidate = current_url[:m.start()] + m.group(1) + str(next_num) + m.group(3)
        if log_fn:
            log_fn(f"[pattern] Inferred next URL: {candidate}", "dim")
        return candidate

    m = re.search(r"/(chapters?|ch)/([0-9]+)(/?[^/]*)$", current_url, re.I)
    if m:
        next_num  = int(m.group(2)) + 1
        candidate = current_url[:m.start()] + f"/{m.group(1)}/{next_num}{m.group(3)}"
        if log_fn:
            log_fn(f"[pattern] Inferred next URL: {candidate}", "dim")
        return candidate

    # Scan all chapter-like links on the page, find current, return next
    all_hrefs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"chapter|ch[-_/][0-9]", href, re.I):
            full = urljoin(current_url, href)
            if full not in all_hrefs:
                all_hrefs.append(full)

    if current_url in all_hrefs:
        idx = all_hrefs.index(current_url)
        if idx + 1 < len(all_hrefs):
            candidate = all_hrefs[idx + 1]
            if log_fn:
                log_fn(f"[pattern] Found next in chapter list: {candidate}", "dim")
            return candidate

    return None
