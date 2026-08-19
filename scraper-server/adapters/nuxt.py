"""
nuxt.py — Nuxt.js adapter
==========================
Handles Nuxt 2 (window.__NUXT__) and Nuxt 3 (__NUXT_DATA__ script tag
or _payload.json endpoint) sites.

Confidence signals:
  - "__NUXT_DATA__" in HTML    +0.5
  - "__nuxt" in HTML           +0.3
  - "_nuxt/" in HTML           +0.3
"""

import re
import json
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import (
    clean_text, strip_watermarks, collect_text_values,
    pick_best_candidate, find_next_url_generic,
    infer_next_url_from_pattern,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


class NuxtAdapter(BaseAdapter):
    name     = "nuxt"
    priority = 60

    def can_handle(self, url: str, html: str) -> float:
        score = 0.0
        if "__NUXT_DATA__" in html:  score += 0.5
        if "__nuxt" in html:         score += 0.3
        if "_nuxt/" in html:         score += 0.3
        return min(score, 1.0)

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        if log_fn:
            log_fn("[Nuxt] Detected — trying data extraction", "info")

        # Strategy 1: window.__NUXT__ inline script (Nuxt 2)
        result = self._try_inline(html, log_fn)
        if result:
            return result

        # Strategy 2: <script id="__NUXT_DATA__"> (Nuxt 3)
        result = self._try_nuxt3_script(html, log_fn)
        if result:
            return result

        # Strategy 3: _payload.json endpoint
        result = self._try_payload(url, session, log_fn)
        return result

    def _try_inline(self, html: str, log_fn=None) -> str | None:
        pattern = re.compile(
            r'<script[^>]*>\s*window\.__NUXT__\s*=\s*(\{.+?\})\s*</script>',
            re.S,
        )
        m = re.search(pattern, html)
        if not m:
            return None
        try:
            data       = json.loads(m.group(1))
            candidates = collect_text_values(data)
            return pick_best_candidate(candidates, log_fn, "Nuxt window.__NUXT__")
        except Exception:
            return None

    def _try_nuxt3_script(self, html: str, log_fn=None) -> str | None:
        pattern = re.compile(
            r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
            re.S,
        )
        m = re.search(pattern, html)
        if not m:
            return None
        try:
            data       = json.loads(m.group(1))
            candidates = collect_text_values(data)
            return pick_best_candidate(candidates, log_fn, "Nuxt __NUXT_DATA__")
        except Exception:
            return None

    def _try_payload(self, url: str, session, log_fn=None) -> str | None:
        parsed      = urlparse(url)
        payload_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            f"{parsed.path.rstrip('/')}/_payload.json"
        )
        try:
            if log_fn:
                log_fn(f"[Nuxt] Trying _payload.json: {payload_url}", "dim")
            r = session.get(payload_url, headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                candidates = collect_text_values(r.json())
                return pick_best_candidate(candidates, log_fn, "Nuxt _payload.json")
        except Exception:
            pass
        return None

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        result = find_next_url_generic(soup, url)
        if result:
            return result
        return infer_next_url_from_pattern(url, soup, log_fn)
