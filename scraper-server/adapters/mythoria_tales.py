"""
mythoria_tales.py — Mythoria Tales adapter (v2)
================================================
Handles www.mythoriatales.com

Fixes in v2:
  1. INFINITE LOOP — tracks consecutive premium/empty chapters and returns
     None from find_next_url after MAX_EMPTY_RUN consecutive failures,
     stopping the crawl cleanly.
  2. reset_state() added for clean per-job state.

Architecture (unchanged from v1):
  - Next.js App Router, fully client-side rendered — HTML shell has no content.
  - Content fetched via REST API:
      GET https://api.mythoriatales.com/chapter/public/series/{slug}/chapter/{number}
  - nextChapter / prevChapter returned directly in API response.
  - Premium chapters have isPremium=true and content=null — skipped.
"""

import re
from urllib.parse import urlparse

import requests as _requests

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks

API_BASE = "https://api.mythoriatales.com"

# Stop crawl after this many consecutive premium/empty chapters
MAX_EMPTY_RUN = 5

_HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":       "application/json",
    "Referer":      "https://www.mythoriatales.com/",
    "Origin":       "https://www.mythoriatales.com",
}


class MythoriaAdapter(BaseAdapter):
    name     = "mythoria_tales"
    priority = 100

    def __init__(self):
        super().__init__()
        self._consecutive_empty = 0

    def reset_state(self):
        """Called by scraper_server at the start of each new job."""
        self._consecutive_empty = 0

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "mythoriatales.com" in url:
            return 1.0
        if "mythoriatales" in html:
            return 0.9
        return 0.0

    # ── Core: fetch from API ───────────────────────────────────────────────

    def _fetch_chapter_api(self, series_slug: str, chapter_num: int | str,
                            session, log_fn=None) -> dict | None:
        endpoint = f"{API_BASE}/chapter/public/series/{series_slug}/chapter/{chapter_num}"
        if log_fn:
            log_fn(f"[Mythoria] API: {endpoint}", "dim")
        try:
            r = session.get(endpoint, headers=_HEADERS, timeout=20)
            if r.status_code == 404:
                if log_fn:
                    log_fn(f"[Mythoria] 404 — chapter {chapter_num} not found", "warn")
                return None
            if not r.ok:
                if log_fn:
                    log_fn(f"[Mythoria] API returned HTTP {r.status_code}", "warn")
                return None
            body = r.json()
            if not body.get("success"):
                err = body.get("error", {})
                if log_fn:
                    log_fn(f"[Mythoria] API error: {err.get('message', 'unknown')}", "warn")
                return None
            return body.get("data")
        except Exception as e:
            if log_fn:
                log_fn(f"[Mythoria] API request failed: {e}", "err")
            return None

    def _parse_url(self, url: str) -> tuple[str | None, str | None]:
        m = re.search(r"/series/([^/]+)/chapter/([^/?#]+)", url)
        if m:
            return m.group(1), m.group(2)
        return None, None

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        series_slug, chapter_num = self._parse_url(url)
        if not series_slug or not chapter_num:
            if log_fn:
                log_fn("[Mythoria] Could not parse series slug / chapter number from URL", "warn")
            return None

        data = self._fetch_chapter_api(series_slug, chapter_num, session, log_fn)
        if not data:
            self._consecutive_empty += 1
            if log_fn:
                log_fn(f"[Mythoria] No API data (empty run: {self._consecutive_empty}/{MAX_EMPTY_RUN})", "warn")
            return None

        # Cache so find_next_url can reuse without a second API call
        soup._mythoria_api_data = data

        chapter = data.get("chapter", {})

        # Premium / locked chapter
        if chapter.get("isPremium") and not chapter.get("content"):
            self._consecutive_empty += 1
            if log_fn:
                log_fn(
                    f"[Mythoria] Ch.{chapter_num} is premium — skipping "
                    f"({self._consecutive_empty}/{MAX_EMPTY_RUN})", "warn"
                )
            return None

        content = chapter.get("content", "")
        if not content:
            self._consecutive_empty += 1
            if log_fn:
                log_fn(f"[Mythoria] Ch.{chapter_num} has no content (empty run: {self._consecutive_empty}/{MAX_EMPTY_RUN})", "warn")
            return None

        # Strip HTML tags if present
        if "<" in content and ">" in content:
            from bs4 import BeautifulSoup
            content = BeautifulSoup(content, "lxml").get_text(separator="\n")

        text = clean_text(content)
        if len(text) > 100:
            if log_fn:
                log_fn(f"[Mythoria] Extracted {len(text.split())} words via API", "dim")
            self._consecutive_empty = 0
            return strip_watermarks(text)

        self._consecutive_empty += 1
        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        # Stop if too many consecutive premium/empty chapters
        if self._consecutive_empty >= MAX_EMPTY_RUN:
            if log_fn:
                log_fn(
                    f"[Mythoria] {self._consecutive_empty} consecutive premium/empty chapters "
                    f"— stopping crawl", "warn"
                )
            return None

        series_slug, chapter_num = self._parse_url(url)
        if not series_slug or not chapter_num:
            return None

        data = getattr(soup, '_mythoria_api_data', None)
        if data is None:
            session = _requests.Session()
            data    = self._fetch_chapter_api(series_slug, chapter_num, session, log_fn)
        if not data:
            return self._increment_url(url, log_fn)

        next_ch = data.get("nextChapter")
        if next_ch and next_ch.get("chapterNumber") is not None:
            next_num = next_ch["chapterNumber"]
            parsed   = urlparse(url)
            next_url = f"{parsed.scheme}://{parsed.netloc}/series/{series_slug}/chapter/{next_num}"
            if log_fn:
                log_fn(f"[Mythoria] Next chapter: {next_num}", "dim")
            return next_url

        if log_fn:
            log_fn("[Mythoria] No next chapter in API response — end of series", "info")
        return None

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        series_slug, chapter_num = self._parse_url(url)
        if not series_slug or not chapter_num:
            return None

        data = getattr(soup, '_mythoria_api_data', None)
        if data is None:
            session = _requests.Session()
            data    = self._fetch_chapter_api(series_slug, chapter_num, session, log_fn)
        if not data:
            return None

        prev_ch = data.get("prevChapter")
        if prev_ch and prev_ch.get("chapterNumber") is not None:
            prev_num = prev_ch["chapterNumber"]
            parsed   = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}/series/{series_slug}/chapter/{prev_num}"
        return None

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text(strip=True))
            raw = re.sub(r"\s*[-–|]\s*Mythoria\s*$", "", raw, flags=re.I)
            if raw and raw.lower() not in ("mythoria", ""):
                return raw
        return None

    # ── Private ────────────────────────────────────────────────────────────

    def _increment_url(self, url: str, log_fn=None) -> str | None:
        m = re.search(r"^(.*?/chapter/)(\d+)(/?(?:\?.*)?)?$", url)
        if m:
            nxt = f"{m.group(1)}{int(m.group(2)) + 1}"
            if log_fn:
                log_fn(f"[Mythoria] Next (URL fallback): {nxt}", "dim")
            return nxt
        return None
