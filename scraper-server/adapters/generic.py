"""
generic.py — Generic / Static HTML adapter
===========================================
The last-resort adapter. Tries standard CSS content selectors,
falls back to the largest <div> on the page, and finally scans
all <script> blocks for embedded JSON content.

This adapter always returns a low confidence score (0.1) so it
only wins when no more specific adapter matches. It should always
be able to produce *something* even if it's not perfect.
"""

import re
import json

from .base import BaseAdapter
from .utils import (
    clean_text, strip_watermarks, collect_text_values,
    pick_best_candidate, find_next_url_generic, find_prev_url_generic,
    CONTENT_SELECTORS,
)


class GenericAdapter(BaseAdapter):
    name     = "generic"
    priority = 0   # always lowest — only wins if nothing else matches

    def can_handle(self, url: str, html: str) -> float:
        # Always available as fallback, but at minimum score
        return 0.1

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # Remove noise tags first
        from bs4 import BeautifulSoup
        soup2 = BeautifulSoup(html, "lxml")
        for tag in ["script", "style", "nav", "header", "footer", "aside",
                    "figure", "figcaption", "iframe", "ins", "noscript", "form", "button"]:
            for el in soup2.find_all(tag):
                el.decompose()

        # Standard selector pass
        for sel in CONTENT_SELECTORS:
            try:
                block = soup2.select_one(sel)
                if block:
                    text = clean_text(block.get_text(separator="\n"))
                    if len(text) > 200:
                        if log_fn:
                            log_fn(f"[Generic] Matched selector: {sel}", "dim")
                        return strip_watermarks(text)
            except Exception:
                continue

        # Largest <div> fallback
        divs = soup2.find_all("div")
        if divs:
            biggest = max(divs, key=lambda d: len(d.get_text()))
            text    = clean_text(biggest.get_text(separator="\n"))
            if len(text) > 200:
                if log_fn:
                    log_fn("[Generic] Using largest div as content", "dim")
                return strip_watermarks(text)

        # Script JSON scan — last resort
        result = self._scan_scripts(html, log_fn)
        return result

    def _scan_scripts(self, html: str, log_fn=None) -> str | None:
        """Scan all <script> blocks for any JSON containing long prose strings."""
        if log_fn:
            log_fn("[Generic] Scanning script tags for embedded JSON", "dim")

        best = ""
        pattern = re.compile(r'<script[^>]*>(\s*[\[{].{150,}[\]}]\s*)</script>', re.S)
        for m in pattern.finditer(html):
            block = m.group(1).strip()
            try:
                data       = json.loads(block)
                candidates = collect_text_values(data)
                if candidates:
                    local_best = max(candidates, key=len)
                    if len(local_best) > len(best):
                        best = local_best
            except Exception:
                # Try to find a JSON substring
                for jm in re.finditer(r'(\{.{100,}\}|\[.{100,}\])', block, re.S):
                    try:
                        data       = json.loads(jm.group(1))
                        candidates = collect_text_values(data)
                        if candidates:
                            local_best = max(candidates, key=len)
                            if len(local_best) > len(best):
                                best = local_best
                    except Exception:
                        continue

        if best:
            return pick_best_candidate([best], log_fn, "Generic script scan")
        return None

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        return find_next_url_generic(soup, url)

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        return find_prev_url_generic(soup, url)
