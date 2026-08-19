"""
curspe.py — Curspe / JormunTL WordPress theme adapter
=======================================================
Handles curspe.com and any other site running the JormunTL WordPress theme.

Root cause of the bug this adapter fixes:
  The JormunTL theme uses btn-primary / btn-secondary CSS classes for chapter
  navigation instead of standard rel="next"/rel="prev" or class="next-chap"
  patterns. The generic selectors match the FIRST qualifying link on the page,
  which turns out to be the "Previous Chapter" (btn-secondary) button — so the
  crawler loops backward forever instead of advancing forward.

  Additionally, the breadcrumb and sidebar contain chapter links that can
  accidentally match generic "a[href*='chapter']" selectors.

Fix:
  1. Find the bottom navigation bar (the div.flex.justify-between after the
     chapter content).
  2. The RIGHT side of that bar always holds the "Next Chapter" btn-primary link.
  3. Fall back to scanning all <a> tags for text "Next Chapter" if the layout
     changes.

Detection:
  - "JormunTL" in HTML (theme stylesheet path)         +0.9
  - "curspe.com" in URL                                +1.0  (exact domain)
  - "btn-primary" + "btn-secondary" both present       +0.3  (nav pattern)
  - WordPress 6.x + Alpine.js + Lucide combo           +0.2

Content:
  The content is already in div.chapter-content so the standard selector pass
  in scraper_server.py handles it before this adapter is even called. This
  adapter's main job is fixing navigation.
"""

import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks, CONTENT_SELECTORS


class CurspeAdapter(BaseAdapter):
    name     = "curspe_jormuntl"
    priority = 100          # site-specific — always wins when it matches

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        score = 0.0

        # Exact domain match — maximum confidence
        if "curspe.com" in url:
            return 1.0

        # Theme fingerprint — works for any other site using JormunTL
        if "JormunTL" in html:
            score += 0.9

        # Navigation class pattern unique to this theme
        if "btn-primary" in html and "btn-secondary" in html:
            score += 0.3

        # Stack fingerprint: WordPress + Alpine.js + Lucide
        if "alpinejs" in html and "lucide" in html and "wp-content" in html:
            score += 0.2

        return min(score, 1.0)

    # ── Content extraction ─────────────────────────────────────────────────
    # The standard selector pass in scraper_server.py already handles
    # div.chapter-content correctly, so this method only runs if that fails.
    # It's here as a safety net in case the theme changes its class names.

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # Try the known content selectors for this theme
        for sel in [
            "div.chapter-content",
            "div.prose",
            "div.prose-lg",
            "div[class*='chapter-content']",
        ]:
            try:
                block = soup.select_one(sel)
                if block:
                    # Remove nav arrows that may be inside the content block
                    for noise in block.select("a.btn-primary, a.btn-secondary, div.flex.justify-between"):
                        noise.decompose()
                    text = clean_text(block.get_text(separator="\n"))
                    if len(text) > 200:
                        if log_fn:
                            log_fn(f"[Curspe] Extracted via '{sel}' ({len(text.split())} words)", "dim")
                        return strip_watermarks(text)
            except Exception:
                continue
        return None

    # ── Navigation — this is the core fix ─────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        """
        JormunTL navigation structure (bottom of page):

            <div class="flex justify-between items-center mt-8">
                <div class="flex-shrink-0">
                    <a href="...chapter-1..." class="btn-secondary ...">
                        <svg>...</svg>
                        <span>Previous Chapter</span>
                    </a>
                </div>
                <div class="flex-shrink-0">
                    <a href="...chapter-3..." class="btn-primary ...">
                        <span>Next Chapter</span>
                        <svg>...</svg>
                    </a>
                </div>
            </div>

        The btn-primary link is ALWAYS next, btn-secondary is ALWAYS prev.
        We must not use generic selectors here because btn-primary may also
        appear elsewhere on the page (header CTAs, etc.).
        """

        # Strategy 1: find the bottom nav wrapper and extract the btn-primary
        # link that is inside it (not anywhere else on the page).
        next_url = self._from_bottom_nav(soup, url, log_fn)
        if next_url:
            return next_url

        # Strategy 2: scan ALL anchors for exact text "Next Chapter" —
        # reliable even if the layout changes.
        next_url = self._from_link_text(soup, url, log_fn)
        if next_url:
            return next_url

        # Strategy 3: aria-label fallback
        for a in soup.find_all("a", href=True):
            label = (a.get("aria-label") or "").lower()
            if "next" in label and "chapter" in label:
                href = a["href"]
                if href and not href.startswith("#"):
                    if log_fn:
                        log_fn(f"[Curspe] Next via aria-label: {href}", "dim")
                    return urljoin(url, href)

        if log_fn:
            log_fn("[Curspe] Could not find next chapter link", "warn")
        return None

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        # btn-secondary in the bottom nav = previous chapter
        prev_url = self._prev_from_bottom_nav(soup, url)
        if prev_url:
            return prev_url

        for a in soup.find_all("a", href=True):
            spans = [s.get_text(strip=True).lower() for s in a.find_all("span")]
            full  = a.get_text(strip=True).lower()
            if "previous chapter" in full or "previous chapter" in " ".join(spans):
                href = a["href"]
                if href and not href.startswith("#"):
                    return urljoin(url, href)
        return None

    # ── Private helpers ────────────────────────────────────────────────────

    def _from_bottom_nav(self, soup, current_url: str, log_fn=None) -> str | None:
        """
        Find the bottom navigation bar (div.flex.justify-between.items-center)
        and return the href of the btn-primary link inside it.

        We specifically look for the nav that comes AFTER the chapter content,
        not any other flex-justify-between container on the page.
        """
        # The chapter content div
        content_div = (
            soup.select_one("div.chapter-content") or
            soup.select_one("div.prose-lg") or
            soup.select_one("div.prose")
        )

        # Walk siblings/parent structure to find the nav that follows content
        nav_containers = []

        if content_div:
            # Look for a sibling or cousin container after the content div
            parent = content_div.parent
            if parent:
                found_content = False
                for sibling in parent.children:
                    if sibling == content_div:
                        found_content = True
                        continue
                    if found_content and hasattr(sibling, "select"):
                        # Check if this sibling contains a btn-primary link
                        btn = sibling.select_one("a.btn-primary")
                        if btn:
                            nav_containers.append(sibling)
                            break

                # If not found as direct sibling, walk up one more level
                if not nav_containers:
                    grandparent = parent.parent
                    if grandparent:
                        found_parent = False
                        for sibling in grandparent.children:
                            if sibling == parent:
                                found_parent = True
                                continue
                            if found_parent and hasattr(sibling, "select"):
                                btn = sibling.select_one("a.btn-primary")
                                if btn:
                                    nav_containers.append(sibling)
                                    break

        # Fallback: find all justify-between divs and pick the one with
        # both btn-primary and btn-secondary (= navigation bar)
        if not nav_containers:
            for div in soup.select("div.flex.justify-between"):
                if div.select_one("a.btn-primary") and div.select_one("a.btn-secondary"):
                    nav_containers.append(div)

        for nav in nav_containers:
            btn = nav.select_one("a.btn-primary")
            if btn and btn.get("href"):
                href = btn["href"]
                if href.startswith("#") or href.startswith("javascript"):
                    continue
                # Sanity check: make sure it's not the same page
                full = urljoin(current_url, href)
                if full == current_url:
                    continue
                if log_fn:
                    log_fn(f"[Curspe] Next chapter (btn-primary nav): {full}", "dim")
                return full

        return None

    def _prev_from_bottom_nav(self, soup, current_url: str) -> str | None:
        for div in soup.select("div.flex.justify-between"):
            btn = div.select_one("a.btn-secondary")
            if btn and btn.get("href"):
                href = btn["href"]
                if href.startswith("#") or href.startswith("javascript"):
                    continue
                full = urljoin(current_url, href)
                if full != current_url:
                    return full
        return None

    def _from_link_text(self, soup, current_url: str, log_fn=None) -> str | None:
        """
        Scan every anchor for a <span>Next Chapter</span> child.
        More reliable than class-based matching when layout changes.
        """
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            # Check direct text and child spans
            spans     = [s.get_text(strip=True) for s in a.find_all("span")]
            full_text = a.get_text(strip=True)

            texts_to_check = spans + [full_text]
            for t in texts_to_check:
                if t.lower().strip() == "next chapter":
                    full = urljoin(current_url, href)
                    if full != current_url:
                        if log_fn:
                            log_fn(f"[Curspe] Next chapter (span text match): {full}", "dim")
                        return full
        return None

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        # JormunTL puts the chapter title in the h1 inside the content card
        # AND in the <title> tag. The h1 inside .chapter-content is a duplicate
        # of the card header h1, so we prefer the card header.
        card_header = soup.select_one("div.mb-8 > h1")
        if card_header:
            raw = clean_text(card_header.get_text(strip=True))
            if raw and len(raw) > 2:
                return raw

        # Fallback: page <title> tag, strip site name
        title_tag = soup.find("title")
        if title_tag:
            raw = clean_text(title_tag.get_text(strip=True))
            # Strip " – Curspe" suffix
            raw = re.sub(r"\s*[–\-]\s*Curspe\s*$", "", raw, flags=re.I)
            if raw and len(raw) > 2:
                return raw

        return None
