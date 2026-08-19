"""
nextjs_rsc.py — Next.js RSC (React Server Components) adapter
==============================================================
Handles Next.js sites that use the self.__next_f.push() RSC streaming
payload format. Used by Dreamy Translations and any other site built on
Next.js App Router with React Server Components.

Also handles classic Next.js __NEXT_DATA__ and common JSON API endpoints
as fallbacks.

Confidence signals:
  - "self.__next_f" in HTML (RSC payload)     → 0.9  (very specific)
  - "__NEXT_DATA__" in HTML                   → 0.7
  - "_next/static" in HTML                    → 0.5
"""

import re
import json
from urllib.parse import urlparse, urljoin

from .base import BaseAdapter
from .utils import (
    clean_text, strip_watermarks, walk_json_for_text,
    collect_text_values, pick_best_candidate,
    find_next_url_generic, infer_next_url_from_pattern,
    CONTENT_KEYS,
)

_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class NextJsRscAdapter(BaseAdapter):
    name     = "nextjs_rsc"
    priority = 70   # above generic Next.js — this is more specific

    def can_handle(self, url: str, html: str) -> float:
        if "self.__next_f" in html:
            return 0.9
        return 0.0


    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # ── Strategy 1: rendered HTML article (Dreamy Translations layout) ──
        # Content is already in the rendered HTML inside article.chapter-content
        article = soup.find("article", class_="chapter-content")
        if article:
            # Remove any script/button/nav elements
            for tag in article.find_all(["script", "style", "button", "nav"]):
                tag.decompose()
            paragraphs = article.find_all("div", class_="paragraph")
            if paragraphs:
                text = clean_text("\n\n".join(p.get_text(separator=" ", strip=True) for p in paragraphs))
            else:
                text = clean_text(article.get_text(separator="\n", strip=True))
            if len(text) > 100:
                if log_fn:
                    log_fn(f"[Next.js RSC] Extracted {len(text.split())} words from rendered HTML", "dim")
                return strip_watermarks(text)

        # ── Strategy 2: RSC push payload scan ────────────────────────────────
        if log_fn:
            log_fn("[Next.js RSC] Detected RSC push payload — scanning blocks", "info")

        push_pattern = re.compile(
            r'self\.__next_f\.push\(\[1\s*,\s*"((?:[^"\\]|\\.)*)"\]\)',
            re.S,
        )

        def unescape(s):
            return s.encode("utf-8").decode("unicode_escape", errors="replace")

        best_text = ""

        for m in push_pattern.finditer(html):
            raw = m.group(1)
            try:
                payload = unescape(raw)
            except Exception:
                payload = raw

            for line in payload.splitlines():
                colon_idx = line.find(":")
                if colon_idx == -1:
                    continue
                body = line[colon_idx + 1:].strip()
                if not body:
                    continue

                try:
                    obj   = json.loads(body)
                    found = walk_json_for_text(obj)
                    if found and len(found) > len(best_text):
                        best_text = found
                except Exception:
                    # Not JSON — check if it's a raw prose blob
                    if len(body) > 400 and "\n" in body:
                        cleaned = re.sub(r"<[^>]+>", " ", body)
                        cleaned = clean_text(cleaned)
                        if len(cleaned.split()) > 80 and len(cleaned) > len(best_text):
                            best_text = cleaned

        if best_text and len(best_text) > 100:
            if log_fn:
                log_fn(f"[Next.js RSC] Extracted {len(best_text.split())} words", "dim")
            return strip_watermarks(best_text)
        return None

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        parsed   = urlparse(url)
        slug_m   = re.search(r"/novel/([^/]+)/chapter/\d+", parsed.path)

        # ── RSC payload: nextChapter.index ───────────────────────────────────
        if slug_m and "self.__next_f" in html:
            novel_slug = slug_m.group(1)
            next_m     = re.search(r'nextChapter[^0-9]{1,30}(\d+)', html, re.S)
            if next_m:
                next_idx  = next_m.group(1)
                candidate = f"{parsed.scheme}://{parsed.netloc}/novel/{novel_slug}/chapter/{next_idx}"
                if log_fn:
                    log_fn(f"[Next.js RSC] Next chapter URL: {candidate}", "dim")
                return candidate

        # ── HTML link: text starts with "Next" (Dreamy layout) ──────────────
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if text.startswith("next"):
                href = a["href"]
                if not href.startswith("#") and not href.startswith("javascript:"):
                    full = f"{parsed.scheme}://{parsed.netloc}{href}" if href.startswith("/") else href
                    if log_fn:
                        log_fn(f"[Next.js RSC] Next chapter URL (HTML link): {full}", "dim")
                    return full

        result = find_next_url_generic(soup, url)
        if result:
            return result
        return infer_next_url_from_pattern(url, soup, log_fn)

    def extract_title(self, soup, fallback_num: int) -> str | None:
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text(strip=True))
            raw = re.sub(r"\s*[-–|]\s*Dreamy Translations.*$", "", raw, flags=re.I)
            if raw and len(raw) > 2:
                return raw
        return None


class NextJsDataAdapter(BaseAdapter):
    """
    Classic Next.js: extracts content from the __NEXT_DATA__ JSON blob
    and falls back to common JSON API endpoints.
    """
    name     = "nextjs_data"
    priority = 60

    def can_handle(self, url: str, html: str) -> float:
        if "self.__next_f" in html:
            return 0.0   # let NextJsRscAdapter handle it
        if "__NEXT_DATA__" in html:
            return 0.7
        if "_next/static" in html:
            return 0.5
        return 0.0

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        if log_fn:
            log_fn("[Next.js] Trying __NEXT_DATA__ extraction", "info")

        result = self._from_next_data(html, url, session, log_fn)
        if result:
            return result

        result = self._from_api(url, session, log_fn)
        return result

    def _from_next_data(self, html, url, session, log_fn=None) -> str | None:
        pattern = r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>'
        m = re.search(pattern, html, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except Exception as e:
            if log_fn:
                log_fn(f"[Next.js] JSON parse error: {e}", "dim")
            return None

        page_props = data.get("props", {}).get("pageProps", {})
        candidates = []
        for key in CONTENT_KEYS:
            if key in page_props:
                val = page_props[key]
                if isinstance(val, str) and len(val) > 200:
                    candidates.append(val)
                else:
                    candidates.extend(collect_text_values(val))
        if not candidates:
            candidates = collect_text_values(page_props)
        if not candidates:
            candidates = collect_text_values(data)

        return pick_best_candidate(candidates, log_fn, "Next.js __NEXT_DATA__")

    def _from_api(self, url, session, log_fn=None) -> str | None:
        parsed  = urlparse(url)
        origin  = f"{parsed.scheme}://{parsed.netloc}"
        path    = parsed.path.rstrip("/")

        candidates = [
            f"{origin}/api/chapter?url={url}",
            f"{origin}/api/chapters{path}",
            f"{origin}/api{path}",
            f"{url.rstrip('/')}.json",
            f"{url.rstrip('/')}?format=json",
        ]
        for api_url in candidates:
            try:
                if log_fn:
                    log_fn(f"[Next.js API] Trying: {api_url}", "dim")
                r = session.get(api_url,
                                headers={**_HEADERS, "Accept": "application/json"},
                                timeout=10)
                if r.status_code != 200:
                    continue
                if "json" not in r.headers.get("content-type", ""):
                    continue
                texts  = collect_text_values(r.json())
                result = pick_best_candidate(texts, log_fn, "Next.js API")
                if result:
                    return result
            except Exception:
                continue
        return None

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        result = find_next_url_generic(soup, url)
        if result:
            return result
        return infer_next_url_from_pattern(url, soup, log_fn)
