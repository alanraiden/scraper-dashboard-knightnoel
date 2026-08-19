"""
indra_translations.py — Indra Translations adapter
=====================================================
Handles indratranslations.com

Architecture: WordPress + Madara theme (wp-manga plugin).
All chapter data is fully server-side rendered — no AJAX needed.

Key observations:
  - Every chapter page embeds the FULL chapter list in the HTML:
      <ul class="main version-chap no-volumn">
        <li class="wp-manga-chapter free-chap">
          <a href="/series/{slug}/chapter-{N}-{title-slug}/">Chapter N: Title</a>
  - The next chapter URL is in a sticky bottom menu:
      <a class="free-chap" href="/series/{slug}/chapter-{N+1}-.../">
  - Content lives in <div class="text-left"> — already clean prose.
  - Chapter number is encoded in the URL: /chapter-{N}-{rest}/

URL structure:
  /series/{series-slug}/chapter-{number}-{title-slug}/

Navigation:
  Primary: <a class="free-chap"> in the sticky bottom reading menu.
  Fallback: parse the full chapter list (all chapters in page HTML),
            find current by URL, return next in sequence.
  This is more reliable than admin-ajax.php because no HTTP call is needed.

Latest chapter detection (watch mode):
  Read the full embedded chapter list, return the highest chapter number
  and its URL. No API call needed — it's all in the page.

Detection:
  - "indratranslations.com" in URL  → 1.0
  - "madara" in HTML + "Indra" in HTML → 0.9
  - "wp-manga-chapter" in HTML + "/series/" in URL → 0.7
"""

import re
from urllib.parse import urljoin, urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks


class IndraTranslationsAdapter(BaseAdapter):
    name     = "indra_translations"
    priority = 100

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "indratranslations.com" in url:
            return 1.0
        if "indra" in url.lower() and "translat" in url.lower():
            return 0.9
        return 0.0

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str,
                        session, log_fn=None) -> str | None:
        # Primary: div.text-left — the cleanest content container on Madara
        block = soup.find("div", class_="text-left")
        if block:
            text = clean_text(block.get_text(separator="\n"))
            if len(text.split()) > 100:
                if log_fn:
                    log_fn(f"[Indra] Extracted {len(text.split())} words via .text-left", "dim")
                return strip_watermarks(text)

        # Fallback: div.reading-content (parent of text-left)
        block = soup.find("div", class_="reading-content")
        if block:
            text = clean_text(block.get_text(separator="\n"))
            if len(text.split()) > 100:
                if log_fn:
                    log_fn(f"[Indra] Extracted {len(text.split())} words via .reading-content", "dim")
                return strip_watermarks(text)

        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str,
                      log_fn=None) -> str | None:
        # Strategy 1: the sticky bottom menu has a direct next-chapter link
        # The link has class="free-chap" and lives inside .reading-sticky-menu
        sticky = soup.find(class_="reading-sticky-menu")
        if sticky:
            next_a = sticky.find("a", class_="free-chap")
            if next_a and next_a.get("href"):
                next_url = next_a["href"]
                if next_url and "chapter" in next_url:
                    if log_fn:
                        m = re.search(r"/chapter-(\d+)-", next_url)
                        log_fn(f"[Indra] Next: Ch.{m.group(1) if m else '?'} (sticky menu)", "dim")
                    return next_url

        # Strategy 2: parse the full embedded chapter list
        # Every chapter page has all chapters in ul.main.version-chap
        return self._next_from_chapter_list(soup, url, log_fn)

    def find_prev_url(self, soup, url: str, html: str,
                      log_fn=None) -> str | None:
        current_num = self._chapter_num(url)
        if current_num is None or current_num <= 1:
            return None

        chapters = self._get_chapter_list(soup)
        if not chapters:
            return None

        # Find the chapter just before current
        prev = None
        for num, href in chapters:
            if num == current_num - 1:
                prev = href
                break
            elif num < current_num:
                prev = href  # keep the closest lower one

        return prev

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        # The chapter heading is inside .reading-content or .entry-title
        for sel in [
            "div.chapter-heading",
            "h1.entry-title",
            "div.reading-content h1",
            "div.reading-content h2",
            "div.reading-content h3",
        ]:
            el = soup.select_one(sel)
            if el:
                raw = clean_text(el.get_text(strip=True))
                if raw and len(raw) > 2:
                    return raw

        # Fallback: page <title> tag — strip site name
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text(strip=True))
            # "Chapter N: Title - Indra Translations"
            raw = re.sub(r"\s*[-–|]\s*Indra\s+Translations\s*$", "", raw, flags=re.I)
            if raw and len(raw) > 2:
                return raw

        return None

    # ── Latest chapter detection (watch mode) ──────────────────────────────

    def detect_latest_chapter(self, index_url, check_selector,
                               session, log_fn=None):
        """
        The chapter list is embedded on every page — fetch the series index
        page and parse it to find the highest chapter number.
        """
        from bs4 import BeautifulSoup
        import requests as _req

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        }

        try:
            r = session.get(index_url, headers=_headers, timeout=20)
            if not r.ok:
                return None
            page_soup = BeautifulSoup(r.text, "lxml")
        except Exception as e:
            if log_fn:
                log_fn(f"[Indra] Could not fetch index page: {e}", "warn")
            return None

        chapters = self._get_chapter_list(page_soup)
        if not chapters:
            return None

        latest_num, latest_url = max(chapters, key=lambda x: x[0])
        if log_fn:
            log_fn(f"[Indra] Latest chapter: {latest_num}", "dim")
        return latest_num, latest_url

    # ── Private helpers ────────────────────────────────────────────────────

    def _chapter_num(self, url: str) -> int | None:
        """Extract the chapter number from a URL like /chapter-{N}-{slug}/"""
        clean = url.split("?")[0].rstrip("/")
        m = re.search(r"/chapter-(\d+)-", clean)
        if m:
            return int(m.group(1))
        return None

    def _get_chapter_list(self, soup) -> list[tuple[int, str]]:
        """
        Parse the full embedded chapter list from the page HTML.
        Returns list of (chapter_num, href) sorted ascending.
        """
        results = []

        # Primary container: ul.main.version-chap
        ul = soup.find("ul", class_=lambda c: c and "version-chap" in " ".join(c) if c else False)
        if not ul:
            # Fallback: listing-chapters_wrap
            wrap = soup.find(class_="listing-chapters_wrap")
            if wrap:
                ul = wrap.find("ul")

        if not ul:
            return results

        for a in ul.find_all("a", href=True):
            href = a["href"]
            if "/chapter-" not in href:
                continue
            m = re.search(r"/chapter-(\d+)-", href)
            if m:
                results.append((int(m.group(1)), href))

        # Deduplicate and sort ascending
        seen = {}
        for num, href in results:
            if num not in seen:
                seen[num] = href
        return sorted(seen.items())

    def _next_from_chapter_list(self, soup, current_url: str,
                                log_fn=None) -> str | None:
        """Find the next chapter URL by scanning the full embedded chapter list."""
        current_num = self._chapter_num(current_url)
        if current_num is None:
            return None

        chapters = self._get_chapter_list(soup)  # sorted ascending
        if not chapters:
            return None

        # Find the chapter immediately after current
        for num, href in chapters:
            if num == current_num + 1:
                if log_fn:
                    log_fn(f"[Indra] Next: Ch.{num} (chapter list)", "dim")
                return href

        # No exact match — find the smallest chapter > current
        for num, href in chapters:
            if num > current_num:
                if log_fn:
                    log_fn(f"[Indra] Next: Ch.{num} (closest after {current_num})", "dim")
                return href

        return None
