"""
revenger_novel.py — Revenger Novel adapter
===========================================
Handles revengernovel.com

Architecture: plain static HTML + jQuery + Tailwind. No JS framework.
Content and navigation are fully rendered server-side in the HTML.

URL structure:
    /series/{novel-slug}/{chapter-id}/chapter-{chapter-number}

    - novel-slug   : the series slug (e.g. a-knight-who-eternally-regresses)
    - chapter-id   : a numeric database ID that is NOT the chapter number
                     (e.g. chapter 1 has id=54, chapter 2 has id=55)
    - chapter-number : the display/upload number (1, 2, 3...)

Navigation:
    Two <button> elements carry data attributes with the next/prev chapter info:
        <button id="nextBtn"
                data-next-chapter-id="55"
                data-next-chapter-number="2">
        <button id="prevBtn"
                data-prev-chapter-id=""        ← empty string when no prev
                data-prev-chapter-number="0">  ← 0 when no prev

    The JS on the page builds the URL from a template embedded in a <script>:
        const chapterRouteUrl =
            'https://revengernovel.com/series/{slug}/:chapterId/chapter-:chapterNumber';

    We replicate that logic in Python.

Content:
    Lives in <div class="content-wrapper"> — clean prose, already stripped
    of nav chrome by the server. No JS rendering needed.

Title:
    <div class="chapter-title"> or <h1> — typically "Chapter N : Title Text"
    We normalise the duplicate prefix.

Detection:
    - "revengernovel.com" in URL           → 1.0
    - "chapterRouteUrl" in HTML            → 0.9  (unique to this site's JS)
    - "chapter-nav-btn" in HTML            → 0.5  (less specific)
"""

import re
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks


class RevengerNovelAdapter(BaseAdapter):
    name     = "revenger_novel"
    priority = 100

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "revengernovel.com" in url:
            return 1.0
        if "chapterRouteUrl" in html:
            return 0.9
        if "chapter-nav-btn" in html and "navigateToChapter" in html:
            return 0.7
        return 0.0

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # Primary: div.content-wrapper
        block = soup.find("div", class_="content-wrapper")
        if block:
            text = clean_text(block.get_text(separator="\n"))
            if len(text) > 100:
                if log_fn:
                    log_fn(f"[Revenger] Extracted {len(text.split())} words", "dim")
                return strip_watermarks(text)

        # Fallback: any div with 'content' in class
        for div in soup.find_all("div", class_=True):
            cls = " ".join(div.get("class", []))
            if "content" in cls and "modal" not in cls and "comment" not in cls:
                text = clean_text(div.get_text(separator="\n"))
                if len(text) > 300:
                    if log_fn:
                        log_fn(f"[Revenger] Extracted via fallback '{cls}' ({len(text.split())} words)", "dim")
                    return strip_watermarks(text)

        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        slug         = self._slug_from_page(soup, html, url)
        next_btn     = soup.find("button", id="nextBtn")
        if not next_btn or not slug:
            return None

        next_id  = next_btn.get("data-next-chapter-id", "").strip()
        next_num = next_btn.get("data-next-chapter-number", "").strip()

        # Empty id or "0" means end of series
        if not next_id or next_id == "0":
            if log_fn:
                log_fn("[Revenger] No next chapter (end of series)", "info")
            return None

        parsed   = urlparse(url)
        next_url = f"{parsed.scheme}://{parsed.netloc}/series/{slug}/{next_id}/chapter-{next_num}"
        if log_fn:
            log_fn(f"[Revenger] Next: ch.{next_num} (id={next_id})", "dim")
        return next_url

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        slug     = self._slug_from_page(soup, html, url)
        prev_btn = soup.find("button", id="prevBtn")
        if not prev_btn or not slug:
            return None

        prev_id  = prev_btn.get("data-prev-chapter-id", "").strip()
        prev_num = prev_btn.get("data-prev-chapter-number", "").strip()

        if not prev_id or prev_id == "" or prev_num == "0":
            return None

        parsed   = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/series/{slug}/{prev_id}/chapter-{prev_num}"

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        # Try the dedicated chapter-title element first
        title_el = soup.find(class_="chapter-title") or soup.find("h1")
        if title_el:
            raw = clean_text(title_el.get_text(strip=True))
            # The site often has "Chapter N : Chapter N" or "Chapter N : Actual Title"
            # Normalise by deduplicating the prefix
            raw = re.sub(r"^(Chapter\s+\d+)\s*[:\-–]\s*\1$", r"\1", raw, flags=re.I)
            # Strip leading "Chapter N : " if what follows is just the same
            raw = re.sub(r"^Chapter\s+\d+\s*[:\-–]\s*", "", raw, flags=re.I).strip() or raw
            if raw and len(raw) > 1:
                return raw

        return None

    # ── Private helpers ────────────────────────────────────────────────────

    def _slug_from_page(self, soup, html: str, url: str) -> str | None:
        """
        Extract the novel slug. Three sources in priority order:
        1. The chapterRouteUrl template embedded in a <script> tag
        2. The current URL path
        3. None
        """
        # Source 1: script template (most reliable)
        m = re.search(r"chapterRouteUrl\s*=\s*['\"]https?://[^/]+/series/([^/]+)/", html)
        if m:
            return m.group(1)

        # Source 2: current URL /series/{slug}/{id}/chapter-{num}
        m2 = re.search(r"/series/([^/]+)/\d+/chapter-\d+", url)
        if m2:
            return m2.group(1)

        return None
