"""
madara.py — Madara / WordPress Manga adapter
=============================================
Handles WordPress sites using the Madara manga theme and similar
wp-manga setups. Uses the WordPress REST API and admin-ajax.php
fallbacks when the standard content selectors come up empty.

Confidence signals:
  - "wp-manga" in HTML                        +0.4
  - "madara" in HTML                          +0.3
  - "admin-ajax.php" in HTML                  +0.2
  - URL matches /series/<slug>/chapter-<N>    +0.4
"""

import re
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

from .base import BaseAdapter
from .utils import (
    clean_text, strip_watermarks, find_next_url_generic,
    find_prev_url_generic, CONTENT_SELECTORS,
)


class MadaraAdapter(BaseAdapter):
    name     = "madara"
    priority = 50

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        score = 0.0
        if "wp-manga" in html:         score += 0.4
        if "madara" in html:           score += 0.3
        if "admin-ajax.php" in html:   score += 0.2
        if re.search(r"/series/[^/]+/chapter-\d+", url, re.I): score += 0.4
        return min(score, 1.0)

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # Standard selector pass first
        soup2 = BeautifulSoup(html, "lxml")
        for tag in ["script", "style", "nav", "header", "footer", "aside",
                    "figure", "figcaption", "iframe", "ins", "noscript", "form", "button"]:
            for el in soup2.find_all(tag):
                el.decompose()

        for sel in CONTENT_SELECTORS:
            try:
                block = soup2.select_one(sel)
                if block:
                    text = clean_text(block.get_text(separator="\n"))
                    if len(text) > 200:
                        return strip_watermarks(text)
            except Exception:
                continue

        # WordPress REST API fallback
        if log_fn:
            log_fn("[Madara] Standard selectors empty — trying WP REST API", "info")
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        slug_m = (re.search(r"/series/[^/]+/(chapter-\d+)", url, re.I) or
                  re.search(r"/(chapter-\d+)/?$", url, re.I))
        slug = slug_m.group(1) if slug_m else None

        result = self._try_wp_rest(origin, slug, session, log_fn)
        if result:
            return result

        result = self._try_madara_ajax(origin, html, session, log_fn)
        return result

    def _try_wp_rest(self, origin: str, slug: str | None, session, log_fn=None) -> str | None:
        endpoints = [f"{origin}/wp-json/wp/v2/posts?slug={slug}&_fields=content"]
        if not slug:
            endpoints = [f"{origin}/wp-json/wp/v2/posts?per_page=1&_fields=content"]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        }

        for ep in endpoints:
            try:
                r = session.get(ep, headers=headers, timeout=15)
                if r.ok:
                    data = r.json()
                    posts = data if isinstance(data, list) else [data]
                    for post in posts:
                        raw = post.get("content", {}).get("rendered", "")
                        if raw and len(raw) > 200:
                            soup = BeautifulSoup(raw, "lxml")
                            text = clean_text(soup.get_text(separator="\n"))
                            if len(text) > 200:
                                if log_fn:
                                    log_fn(f"[Madara] WP REST extracted {len(text.split())} words", "dim")
                                return strip_watermarks(text)
            except Exception:
                continue
        return None

    def _try_madara_ajax(self, origin: str, html: str, session, log_fn=None) -> str | None:
        """
        POST to admin-ajax.php with action=manga_get_reading_content.
        Extract chapter_id from the page source.
        """
        chapter_id_m = re.search(r"chapter_id['\"]?\s*[:=]\s*['\"]?(\d+)", html)
        if not chapter_id_m:
            return None

        chapter_id = chapter_id_m.group(1)
        ajax_url   = f"{origin}/wp-admin/admin-ajax.php"
        headers    = {
            "User-Agent":   "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        payload = {
            "action":     "manga_get_reading_content",
            "chapter_id": chapter_id,
        }

        try:
            r = session.post(ajax_url, data=payload, headers=headers, timeout=15)
            if r.ok:
                data = r.json()
                raw  = data.get("data", "") or ""
                if raw:
                    soup = BeautifulSoup(raw, "lxml")
                    text = clean_text(soup.get_text(separator="\n"))
                    if len(text) > 200:
                        if log_fn:
                            log_fn(f"[Madara] AJAX extracted {len(text.split())} words", "dim")
                        return strip_watermarks(text)
        except Exception as e:
            if log_fn:
                log_fn(f"[Madara] AJAX error: {e}", "warn")
        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        return find_next_url_generic(soup, url)

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        return find_prev_url_generic(soup, url)

    # ── Latest chapter detection ───────────────────────────────────────────

    def detect_latest_chapter(self, index_url, check_selector, session, log_fn=None):
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = session.get(index_url, headers=headers, timeout=20)
            r.raise_for_status()
        except Exception as e:
            if log_fn:
                log_fn(f"[Madara] Index fetch failed: {e}", "err")
            return None

        soup = BeautifulSoup(r.text, "lxml")

        if check_selector:
            try:
                el = soup.select_one(check_selector)
                if el:
                    m = re.search(r"(\d+)", el.get_text())
                    if m:
                        return int(m.group(1)), urljoin(index_url, el.get("href", ""))
            except Exception:
                pass

        madara_selectors = [
            ".wp-manga-chapter a", ".chapter-list li a",
            ".listing-chapters_wrap li a", ".eph-num a",
        ]
        for sel in madara_selectors:
            links = soup.select(sel)
            candidates = []
            for a in links:
                txt  = a.get_text(strip=True)
                href = a.get("href", "")
                m    = (re.search(r"chapter[\s\-_#]?(\d+)", txt, re.I) or
                        re.search(r"chapter[\-_]?(\d+)", href, re.I))
                if m:
                    candidates.append((int(m.group(1)), urljoin(index_url, href)))
            if candidates:
                return max(candidates, key=lambda x: x[0])

        return None
