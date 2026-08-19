"""
brightnovels.py — Adapter for brightnovels.com
===============================================
BrightNovels is a Laravel + Inertia.js app. Every chapter page embeds
the full chapter data as JSON in the `data-page` attribute of <div id="app">.
No HTML content parsing needed — we extract directly from the JSON.

URL pattern:  https://brightnovels.com/series/{series-slug}/{chapter-slug}
Example:      https://brightnovels.com/series/the-villain-bought-the-female-lead/1

JSON structure (props):
  chapter.content    HTML string — full chapter body
  chapter.number     integer chapter number
  chapter.title      string or null
  chapter.slug       URL slug (e.g. "1", "prologue")
  chapter.is_premium bool
  isUnlocked         bool
  series.slug        series slug used in URL construction
  nextChapter        { id, slug, number, title } or null
  prevChapter        { id, slug, number, title } or null
  allChapters        [ { id, slug, number, name, title } ... ] — full ordered list

Premium + locked chapters have empty content; is_junk_page() handles these.
"""

import re
import json
import html as html_lib
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks

MAX_EMPTY_RUN = 5


class BrightNovelsAdapter(BaseAdapter):
    name     = "brightnovels"
    priority = 100

    def __init__(self):
        super().__init__()
        self._consecutive_empty = 0

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "brightnovels.com" not in url:
            return 0.0
        # Must be a chapter URL: /series/{slug}/{chapter-slug}
        if not re.search(r"/series/[^/]+/[^/]+", url):
            return 0.0
        # Inertia fingerprint
        if 'id="app"' in html and "data-page=" in html:
            return 1.0
        return 0.8

    # ── Inertia props extraction ───────────────────────────────────────────

    def _get_props(self, soup) -> dict | None:
        """Extract Inertia props from BeautifulSoup object."""
        app_div = soup.find(id="app")
        if not app_div:
            return None
        raw = app_div.get("data-page", "")
        if not raw:
            return None
        try:
            data = json.loads(html_lib.unescape(raw))
            return data.get("props", {})
        except Exception:
            return None

    def _get_props_from_html(self, html: str) -> dict | None:
        """Extract Inertia props directly from raw HTML string (no BS4 needed)."""
        m = re.search(r'id="app"[^>]*\sdata-page="([^"]+)"', html)
        if not m:
            m = re.search(r"id='app'[^>]*\sdata-page='([^']+)'", html)
        if not m:
            return None
        try:
            raw = html_lib.unescape(m.group(1))
            data = json.loads(raw)
            return data.get("props", {})
        except Exception:
            return None

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        props = self._get_props(soup)
        if not props:
            if log_fn:
                log_fn("[BrightNovels] Inertia data-page not found", "warn")
            self._consecutive_empty += 1
            return None

        # Cache on soup so find_next_url can reuse without re-parsing
        soup._bn_props = props

        chapter     = props.get("chapter", {})
        is_premium  = chapter.get("is_premium", False)
        is_unlocked = props.get("isUnlocked", True)
        content     = chapter.get("content") or ""
        ch_num      = chapter.get("number", "?")

        if is_premium and not is_unlocked:
            self._consecutive_empty += 1
            if log_fn:
                log_fn(
                    f"[BrightNovels] Ch.{ch_num} is premium/locked — skipping "
                    f"({self._consecutive_empty}/{MAX_EMPTY_RUN})", "warn"
                )
            return None

        if not content:
            self._consecutive_empty += 1
            if log_fn:
                log_fn(f"[BrightNovels] Ch.{ch_num} has no content", "warn")
            return None

        # Content is HTML — strip tags using BS4 (already imported in server)
        if "<" in content and ">" in content:
            from bs4 import BeautifulSoup as _BS
            content = _BS(content, "lxml").get_text(separator="\n")

        text = clean_text(content)
        text = strip_watermarks(text)

        if len(text.split()) >= 50:
            if log_fn:
                log_fn(f"[BrightNovels] Extracted {len(text.split())} words (Ch.{ch_num})", "dim")
            self._consecutive_empty = 0
            return text

        self._consecutive_empty += 1
        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def _build_url(self, base_url: str, series_slug: str, chapter_slug: str) -> str:
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}/series/{series_slug}/{chapter_slug}"

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        if self._consecutive_empty >= MAX_EMPTY_RUN:
            if log_fn:
                log_fn(
                    f"[BrightNovels] {self._consecutive_empty} consecutive locked/empty "
                    "chapters — stopping crawl", "warn"
                )
            return None

        props   = getattr(soup, "_bn_props", None) or self._get_props(soup)
        next_ch = props.get("nextChapter") if props else None
        series  = (props or {}).get("series", {})
        slug    = series.get("slug", "")

        if next_ch and slug:
            ch_slug = next_ch.get("slug") or str(next_ch.get("number", ""))
            if ch_slug:
                return self._build_url(url, slug, ch_slug)

        # Fallback: increment numeric slug
        m = re.search(r"^(.*?/)(\d+)(/?(?:\?.*)?)?$", url)
        if m:
            return f"{m.group(1)}{int(m.group(2)) + 1}"

        if log_fn:
            log_fn("[BrightNovels] End of series — no next chapter", "info")
        return None

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        props   = getattr(soup, "_bn_props", None) or self._get_props(soup)
        prev_ch = props.get("prevChapter") if props else None
        series  = (props or {}).get("series", {})
        slug    = series.get("slug", "")

        if prev_ch and slug:
            ch_slug = prev_ch.get("slug") or str(prev_ch.get("number", ""))
            if ch_slug:
                return self._build_url(url, slug, ch_slug)
        return None

    # ── Title ──────────────────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        props   = getattr(soup, "_bn_props", None) or self._get_props(soup)
        chapter = (props or {}).get("chapter", {})
        title   = (chapter.get("title") or "").strip()
        number  = chapter.get("number", fallback_num)
        if title:
            return f"Chapter {number}: {clean_text(title)}"
        return f"Chapter {number}"

    # ── Latest chapter detection (watcher) ────────────────────────────────

    def detect_latest_chapter(self, index_url: str, check_selector: str, session, log_fn=None):
        """
        BrightNovels embeds the full allChapters list on every chapter page.
        Fetch the given URL (any chapter page) and read the last entry.
        Returns (chapter_number, chapter_url) or None.
        """
        try:
            import requests as _req
            r = session.get(index_url, timeout=20)
            r.raise_for_status()
        except Exception as e:
            if log_fn:
                log_fn(f"[BrightNovels] detect fetch error: {e}", "warn")
            return None

        props = self._get_props_from_html(r.text)
        if not props:
            if log_fn:
                log_fn("[BrightNovels] detect: could not parse Inertia props", "warn")
            return None

        all_chapters = props.get("allChapters", [])
        series       = props.get("series", {})
        series_slug  = series.get("slug", "")

        if not all_chapters or not series_slug:
            return None

        # allChapters is ordered ascending — last entry is the latest free/unlocked
        # Filter to non-premium if possible, else use the very last entry
        free = [c for c in all_chapters if not c.get("is_premium", False)]
        latest = free[-1] if free else all_chapters[-1]

        num  = latest.get("number")
        slug = latest.get("slug", "")

        if num is None or not slug:
            return None

        p           = urlparse(index_url)
        chapter_url = f"{p.scheme}://{p.netloc}/series/{series_slug}/{slug}"

        if log_fn:
            log_fn(f"[BrightNovels] latest chapter: {num} → {chapter_url}", "dim")

        return int(num), chapter_url
