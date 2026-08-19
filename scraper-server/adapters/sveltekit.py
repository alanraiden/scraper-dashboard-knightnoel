"""
sveltekit.py — SvelteKit adapter
=================================
Handles SvelteKit sites that embed chapter content in:
  1. <script type="application/json"> tags (TipTap/ProseMirror doc tree)
  2. SvelteKit __data.json endpoint
  3. Generic inline JSON blobs

Also handles the Fenrir Realm-style TipTap document format where
content is stored as a stringified JSON doc rather than a flat string.

Confidence signals:
  - "_app/immutable" in HTML              +0.4
  - "__sveltekit" in HTML                 +0.3
  - "__data.json" in HTML                 +0.2
  - <script type="application/json">      +0.2
"""

import re
import json
from urllib.parse import urlparse, urljoin

from .base import BaseAdapter
from .utils import (
    clean_text, strip_watermarks, collect_text_values,
    pick_best_candidate, extract_tiptap_doc,
    find_next_url_generic, find_prev_url_generic,
    infer_next_url_from_pattern,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


class SvelteKitAdapter(BaseAdapter):
    name     = "sveltekit"
    priority = 60

    def can_handle(self, url: str, html: str) -> float:
        score = 0.0
        if "_app/immutable" in html:                            score += 0.4
        if "__sveltekit" in html or "sveltekit:data" in html:  score += 0.3
        if "__data.json" in html:                               score += 0.2
        if re.search(r'<script[^>]+type=["\']application/json["\']', html): score += 0.2
        return min(score, 1.0)

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        if log_fn:
            log_fn("[SvelteKit] Detected — trying data extraction", "info")

        # Strategy 1: TipTap/ProseMirror doc inside <script type="application/json">
        result = self._try_tiptap(html, log_fn)
        if result:
            return result

        # Strategy 2: Generic JSON inside <script type="application/json">
        result = self._try_json_scripts(html, log_fn)
        if result:
            return result

        # Strategy 3: __data.json endpoint
        result = self._try_data_endpoint(url, session, log_fn)
        return result

    # ── TipTap extraction ──────────────────────────────────────────────────

    def _try_tiptap(self, html: str, log_fn=None) -> str | None:
        script_pat = re.compile(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            re.S,
        )
        best_text = ""

        for m in script_pat.finditer(html):
            raw = m.group(1).strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            doc = self._find_tiptap_doc(data)
            if doc:
                text = clean_text(extract_tiptap_doc(doc))
                if len(text.split()) > 50 and len(text) > len(best_text):
                    best_text = text

        if not best_text:
            # Try stringified TipTap inside a regular inline script
            str_pat = re.compile(r'"content"\s*:\s*"(\{[^"]{200,}\})"', re.S)
            for m in str_pat.finditer(html):
                try:
                    unescaped = bytes(m.group(1), "utf-8").decode("unicode_escape", errors="replace")
                    doc = json.loads(unescaped)
                    if isinstance(doc, dict) and doc.get("type") == "doc":
                        text = clean_text(extract_tiptap_doc(doc))
                        if len(text.split()) > 50 and len(text) > len(best_text):
                            best_text = text
                except Exception:
                    continue

        if best_text and len(best_text) > 100:
            if log_fn:
                log_fn(f"[SvelteKit/TipTap] Extracted {len(best_text.split())} words", "dim")
            return strip_watermarks(best_text)
        return None

    def _find_tiptap_doc(self, node, depth: int = 0):
        """Recursively find a TipTap {type:'doc', content:[...]} node."""
        if depth > 8:
            return None
        if isinstance(node, str) and len(node) > 50:
            try:
                inner = json.loads(node)
                r = self._find_tiptap_doc(inner, depth + 1)
                if r:
                    return r
            except Exception:
                pass
            return None
        if isinstance(node, dict):
            if node.get("type") == "doc" and isinstance(node.get("content"), list):
                return node
            for key in ("content", "chapterContent", "chapter_content", "body", "data"):
                if key in node:
                    r = self._find_tiptap_doc(node[key], depth + 1)
                    if r:
                        return r
            for v in node.values():
                r = self._find_tiptap_doc(v, depth + 1)
                if r:
                    return r
        if isinstance(node, list):
            for item in node:
                r = self._find_tiptap_doc(item, depth + 1)
                if r:
                    return r
        return None

    # ── Generic JSON script extraction ────────────────────────────────────

    def _try_json_scripts(self, html: str, log_fn=None) -> str | None:
        pattern = re.compile(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
            re.S,
        )
        for m in pattern.finditer(html):
            try:
                data       = json.loads(m.group(1))
                candidates = collect_text_values(data)
                result     = pick_best_candidate(candidates, log_fn, "SvelteKit inline JSON")
                if result:
                    return result
            except Exception:
                continue
        return None

    # ── __data.json endpoint ───────────────────────────────────────────────

    def _try_data_endpoint(self, url: str, session, log_fn=None) -> str | None:
        parsed   = urlparse(url)
        data_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/__data.json"
        try:
            if log_fn:
                log_fn(f"[SvelteKit] Trying __data.json: {data_url}", "dim")
            r = session.get(data_url, headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                candidates = collect_text_values(r.json())
                return pick_best_candidate(candidates, log_fn, "SvelteKit __data.json")
        except Exception:
            pass
        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        result = find_next_url_generic(soup, url)
        if result:
            return result
        return infer_next_url_from_pattern(url, soup, log_fn)

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        return find_prev_url_generic(soup, url)

    def extract_title(self, soup, fallback_num: int) -> str | None:
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text(strip=True))
            if raw and len(raw) > 2:
                return raw
        return None
