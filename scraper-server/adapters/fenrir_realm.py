"""
fenrir_realm.py — Fenrir Realm adapter (v3)
=============================================
Handles fenrirealm.com (and fenrirscans.com).

Changes in v3
-------------
The site's SvelteKit kit.start() data payload changed from JSON-style
quoted keys to unquoted JavaScript object keys:

  OLD (v2):  "seriesSlug": "my-series",  "chapterData": { "index": 1 }
  NEW (v3):  seriesSlug: "my-series",    chapterData: { ... number: 2 }

This broke _extract_sveltekit_data entirely because both Strategy 1
(bracket-match + JSON.parse, fails on unquoted keys) and Strategy 2
(regex searching for "seriesSlug" with quotes) missed all matches.

When data extraction fails the adapter falls back to the URL regex for
navigation — which works when the URL ends cleanly in a number.  But the
URL regex is a last resort and can fail on edge cases (redirects, trailing
slash variants, Cloudflare challenges on ch.1 pages), causing the crawler
to stop after the first chapter.

v3 fixes
--------
  1. _extract_sveltekit_data gains Strategy 3: unquoted-key regex that
     matches both the old quoted format AND the new bare-key format.
  2. The chapter's number/index/locked fields now correctly scan the
     SECOND occurrence of the chapter data block (title/number/locked
     live after the giant content string, not inside chapterData{}).
  3. _get_slug_and_index prefers number over index for the chapter
     position (number = human-visible chapter number; index = 0-based
     position in the DB which is always number-1).
  4. is_locked checks the unquoted locked: { price: N } pattern too.
  5. _chapter_id_from_html now scans for the unquoted `id:` form.
  6. extract_title prefers the richer `name` field from the data
     payload (e.g. "Chapter 2 - Inner Administrator Jin Yeomyung (2)")
     over the bare <title> tag.
"""

import re
import json
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks, extract_tiptap_doc


_LOCKED_SIGNALS = [
    "Login to Read",
    "Unlock Chapter",
    "buy this chapter",
    "purchase chapter",
    "login_required",
]


class FenrirRealmAdapter(BaseAdapter):
    name     = "fenrir_realm"
    priority = 100

    def can_handle(self, url: str, html: str) -> float:
        if "fenrirealm.com" in url or "fenrirscans.com" in url:
            return 1.0
        if re.search(r"fenrir", html, re.I):
            return 0.8
        # Both old quoted and new unquoted key formats
        if (("seriesSlug" in html or "series_slug" in html)
                and "chapterData" in html
                and "_app/immutable" in html):
            return 0.7
        return 0.0

    # ── Lock detection ─────────────────────────────────────────────────────

    def is_locked(self, html: str) -> bool:
        for signal in _LOCKED_SIGNALS:
            if signal.lower() in html.lower():
                return True
        data = self._extract_sveltekit_data(html)
        if data:
            chapter = data.get("chapterData", {})
            locked  = chapter.get("locked", {})
            content = chapter.get("content", "")
            price   = locked.get("price", 0) if isinstance(locked, dict) else 0
            if price and price > 0 and not content:
                return True
            if not content and ('id="reader-area-' in html or "content-area" in html):
                return True
        else:
            # Fallback: scan raw HTML for unquoted locked price
            m = re.search(r'locked:\s*\{\s*price:\s*([1-9]\d*)', html)
            if m and int(m.group(1)) > 0:
                # Only treat as locked if content-area is absent or empty
                content_div_present = bool(re.search(r'class=["\'][^"\']*content-area', html))
                if not content_div_present:
                    return True
        return False

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        if self.is_locked(html):
            if log_fn:
                num   = self._get_chapter_number_from_data(html)
                label = f"Ch.{num}" if num else "Chapter"
                log_fn(f"[Fenrir] {label} is locked (paywall) -- skipping", "warn")
            return None

        # Primary: rendered HTML div (fastest, always correct on unlocked pages)
        ch_id       = self._chapter_id_from_html(html)
        content_div = None
        if ch_id:
            content_div = soup.select_one(f"div#reader-area-{ch_id}")
        if not content_div:
            content_div = soup.select_one("div[id^='reader-area-']")
        if not content_div:
            content_div = soup.select_one("div.content-area")

        if content_div:
            text = clean_text(content_div.get_text(separator="\n"))
            if len(text) > 200:
                if log_fn:
                    log_fn(f"[Fenrir] Extracted from rendered HTML ({len(text.split())} words)", "dim")
                return strip_watermarks(text)

        # Fallback: TipTap JSON embedded in data payload
        data = self._extract_sveltekit_data(html)
        if data:
            raw = data.get("chapterData", {}).get("content", "")
            if raw and raw != "__present__":
                try:
                    doc = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(doc, dict) and doc.get("type") == "doc":
                        text = clean_text(extract_tiptap_doc(doc))
                        if len(text) > 200:
                            if log_fn:
                                log_fn(f"[Fenrir] Extracted from TipTap doc ({len(text.split())} words)", "dim")
                            return strip_watermarks(text)
                except Exception:
                    pass

        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        series_slug, current_index = self._get_slug_and_index(html, url)
        if series_slug and current_index is not None:
            parsed   = urlparse(url)
            next_idx = int(current_index) + 1
            next_url = f"{parsed.scheme}://{parsed.netloc}/series/{series_slug}/{next_idx}"
            if log_fn:
                log_fn(f"[Fenrir] Next: {current_index} -> {next_idx}", "dim")
            return next_url
        return self._fallback_url_increment(url, log_fn)

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        series_slug, current_index = self._get_slug_and_index(html, url)
        if series_slug and current_index is not None and int(current_index) > 1:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}/series/{series_slug}/{int(current_index)-1}"
        return None

    # ── Title ──────────────────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        # Prefer the rich name from data payload — includes chapter number + title
        data = self._extract_sveltekit_data(soup.__str__() if soup else "")
        # extract_title is called with soup only, not html — but the soup was
        # built from html, so str(soup) works as a proxy.  We also call this
        # privately with the raw html when available (see _extract_title_from_html).
        if data:
            name = data.get("chapterData", {}).get("name", "")
            if name and len(name) > 2:
                return clean_text(name)

        title_tag = soup.find("title")
        if title_tag:
            raw   = clean_text(title_tag.get_text(strip=True))
            raw   = re.sub(r"\s*[-–]\s*Fenrir Realm\s*$", "", raw, flags=re.I)
            parts = re.split(r"\s*[-–]\s*", raw)
            for part in reversed(parts):
                if re.search(r"chapter\s*\d+", part, re.I) or re.search(r"^\d+$", part.strip()):
                    return part.strip()
            if raw and len(raw) > 2:
                return raw

        h2 = soup.select_one("div.chapter-view h2")
        if h2:
            return clean_text(h2.get_text(strip=True))

        return None

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_slug_and_index(self, html: str, url: str) -> tuple:
        data = self._extract_sveltekit_data(html)
        if data:
            chapter     = data.get("chapterData", {})
            series_slug = (data.get("seriesSlug")
                           or data.get("seriesData", {}).get("slug", ""))
            # Prefer `number` (human chapter number) over `index` (0-based DB position)
            idx = chapter.get("number") or chapter.get("index")
            if idx is None:
                try:
                    idx = int(chapter.get("slug") or 0) or None
                except (ValueError, TypeError):
                    idx = None
            if series_slug and idx is not None:
                return series_slug, idx
        m = re.search(r"/series/([^/]+)/(\d+)/?$", url)
        if m:
            return m.group(1), int(m.group(2))
        return None, None

    def _extract_sveltekit_data(self, html: str) -> dict | None:
        if not html:
            return None

        # ── Strategy 1: bracket-match + JSON.parse (old quoted-key format) ──
        m = re.search(r'node_ids\s*:\s*\[[^\]]+\]\s*,\s*data\s*:\s*(\[)', html, re.S)
        if m:
            start  = m.end() - 1
            depth  = 0
            end    = start
            in_str = False
            escape = False
            for i, ch in enumerate(html[start:], start):
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_str:
                    escape = True
                    continue
                if ch == '"' and not escape:
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    arr = json.loads(html[start:end])
                    for item in arr:
                        if isinstance(item, dict):
                            d = item.get("data", {})
                            if isinstance(d, dict) and "chapterData" in d:
                                return d
                except Exception:
                    pass

        # ── Strategy 2: old quoted-key regex (pre-v3 site format) ──
        slug_m = re.search(r'"seriesSlug"\s*:\s*"([^"]+)"', html)
        if not slug_m:
            slug_m = re.search(r'"seriesData"[^}]{0,200}"slug"\s*:\s*"([^"]+)"', html, re.S)
        if slug_m:
            series_slug = slug_m.group(1)
            cd_pos = html.find('"chapterData"')
            if cd_pos != -1:
                chunk    = html[cd_pos:cd_pos + 3000]
                result   = self._parse_chapter_chunk_quoted(chunk)
                result["seriesSlug"] = series_slug
                return result

        # ── Strategy 3: new unquoted JS key format (v3 site update) ──
        #
        # The new format looks like:
        #   seriesSlug: "my-series",
        #   chapterData: {
        #       id: 55779,
        #       slug: "2",
        #       name: "Chapter 2 - ...",
        #       content: "{\"type\":\"doc\",...}",
        #       ...
        #   }
        # Then further down (after the huge content string):
        #   title: "Inner Administrator Jin Yeomyung (2)",
        #   number: 2,
        #   index: 1,
        #   locked: { price: 0, unlocked_at: null },
        #
        slug_m = re.search(r'\bseriesSlug:\s*"([^"]+)"', html)
        if not slug_m:
            # Also try seriesData: { ... slug: "..." }
            slug_m = re.search(r'\bseriesData:\s*\{[^}]{0,400}?\bslug:\s*"([^"]+)"', html, re.S)
        if not slug_m:
            return None

        series_slug = slug_m.group(1)

        cd_pos = html.find("chapterData:")
        if cd_pos == -1:
            return None

        # The chapterData block starts here; scan forward to get id/slug/name/content
        cd_chunk = html[cd_pos:cd_pos + 500]
        id_m     = re.search(r'\bid:\s*(\d{4,})', cd_chunk)
        cslug_m  = re.search(r'\bslug:\s*"(\d+[^"]*)"', cd_chunk)
        name_m   = re.search(r'\bname:\s*"([^"]+)"', cd_chunk)

        # content is a large escaped JSON string — detect its presence
        has_content = bool(re.search(r'\bcontent:\s*"\{', cd_chunk))

        # number/locked/index live AFTER the content string (which can be 50KB+).
        # Scan from after the chapterData open brace to find them.
        # We locate them by searching the region between chapterData and the
        # next top-level block (seriesData or end of data array).
        search_region = html[cd_pos:cd_pos + 70000]

        number_m    = re.search(r'\bnumber:\s*(\d+)', search_region)
        index_m     = re.search(r'\bindex:\s*(\d+)', search_region)
        locked_m    = re.search(r'\blocked:\s*\{\s*price:\s*(\d+)', search_region)
        price       = int(locked_m.group(1)) if locked_m else 0

        return {
            "seriesSlug": series_slug,
            "chapterData": {
                "id":      int(id_m.group(1))      if id_m      else None,
                "slug":    cslug_m.group(1)         if cslug_m   else None,
                "name":    name_m.group(1)          if name_m    else None,
                "number":  int(number_m.group(1))  if number_m  else None,
                "index":   int(index_m.group(1))   if index_m   else None,
                "locked":  {"price": price},
                "content": "__present__" if has_content else "",
            },
        }

    def _parse_chapter_chunk_quoted(self, chunk: str) -> dict:
        """Parse chapterData fields from old quoted-key format."""
        index_m  = re.search(r'"index"\s*:\s*(\d+)', chunk)
        number_m = re.search(r'"number"\s*:\s*(\d+)', chunk)
        cslug_m  = re.search(r'"slug"\s*:\s*"(\d+[^"]*)"', chunk)
        name_m   = re.search(r'"name"\s*:\s*"([^"]+)"', chunk)
        id_m     = re.search(r'"id"\s*:\s*(\d{4,})', chunk)
        locked_m = re.search(r'"locked"\s*:\s*\{[^}]*"price"\s*:\s*(\d+)', chunk)
        price    = int(locked_m.group(1)) if locked_m else 0
        has_content = bool(re.search(r'"content"\s*:\s*"\{[^"]{20,}', chunk))
        return {
            "chapterData": {
                "index":   int(index_m.group(1))  if index_m  else None,
                "number":  int(number_m.group(1)) if number_m else None,
                "slug":    cslug_m.group(1)        if cslug_m  else None,
                "name":    name_m.group(1)         if name_m   else None,
                "id":      int(id_m.group(1))      if id_m     else None,
                "locked":  {"price": price},
                "content": "__present__" if has_content else "",
            },
        }

    def _get_chapter_number_from_data(self, html: str) -> int | None:
        data = self._extract_sveltekit_data(html)
        if data:
            ch = data.get("chapterData", {})
            return ch.get("number") or ch.get("index")
        # Raw fallback for unquoted format when data extraction fails
        m = re.search(r'\bnumber:\s*(\d+)', html)
        return int(m.group(1)) if m else None

    def _chapter_id_from_html(self, html: str) -> str:
        # Try unquoted form first (new format), then quoted (old format)
        m = re.search(r'\bid:\s*(\d{4,})', html)
        if not m:
            m = re.search(r'"id"\s*:\s*(\d{4,})', html)
        return m.group(1) if m else ""

    def _fallback_url_increment(self, url: str, log_fn=None) -> str | None:
        m = re.search(r"^(.*/)(\d+)(/?)$", url)
        if m:
            nxt = f"{m.group(1)}{int(m.group(2))+1}{m.group(3)}"
            if log_fn:
                log_fn(f"[Fenrir] Next (URL fallback): {nxt}", "dim")
            return nxt
        return None
