"""
readrift.py — ReadRift.net adapter
====================================
Handles readrift.net — a Nuxt 3 / PrimeVue novel-reading site.

Site fingerprint
----------------
  - Domain:      readrift.net
  - Framework:   Nuxt 3  (/_nuxt/ assets, SSR payload <script type="application/json">)
  - UI library:  PrimeVue (class="p-button p-component …")
  - Editor:      TipTap (class="tiptap mb-2") — chapter text lives here
  - URL pattern: /book/chapter/{numeric_id}

Content
-------
  Chapter prose is SSR-rendered inside a single <div class="tiptap mb-2">.
  All paragraphs are <p> children of that div — no lazy-loading needed.
  The TipTap editor wrapper never contains nav chrome, so we can grab it
  directly without any post-hoc noise removal.

Navigation
----------
  Next / Previous buttons are <a class="p-button p-component …"> tags whose
  visible text is exactly "Next" or "Prev".  The href is already an absolute
  path: /book/chapter/{id}.  Chapter 1 (or any first chapter of a series)
  has no "Prev" link — the button simply doesn't render.

Title extraction
----------------
  - Chapter label  → <h1>  (e.g. "Chapter 1")
  - Novel name     → <div class="text-2xl"> sibling inside the same
                     <div class="text-center mb-10"> container
  Combined title:  "Chapter 1 – I Regressed"
"""

import re
from urllib.parse import urljoin

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks


class ReadRiftAdapter(BaseAdapter):
    name     = "readrift"
    priority = 100          # exact domain match — always wins

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        # Exact domain — maximum confidence, skip remaining checks
        if "readrift.net" in url:
            return 1.0

        score = 0.0

        # Nuxt 3 SSR payload present
        if "_nuxt" in html:
            score += 0.3

        # TipTap editor wrapper (unique combination with PrimeVue)
        if "tiptap" in html and "p-button" in html and "p-component" in html:
            score += 0.4

        # /book/chapter/ URL pattern
        if re.search(r"/book/chapter/\d+", url):
            score += 0.3

        return min(score, 1.0)

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        # Primary: TipTap div — always present on chapter pages
        tiptap = soup.find("div", class_=lambda c: c and "tiptap" in c)
        if tiptap:
            text = clean_text(tiptap.get_text(separator="\n"))
            if len(text.split()) > 50:
                if log_fn:
                    log_fn(f"[ReadRift] Extracted via tiptap div ({len(text.split())} words)", "dim")
                return strip_watermarks(text)

        # Fallback: novel-container that holds the min-height reading area
        container = soup.find("div", class_=lambda c: c and "min-h" in " ".join(c or []))
        if container:
            text = clean_text(container.get_text(separator="\n"))
            if len(text.split()) > 50:
                if log_fn:
                    log_fn(f"[ReadRift] Extracted via min-h container ({len(text.split())} words)", "dim")
                return strip_watermarks(text)

        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        """
        ReadRift renders navigation as:
            <a class="p-button p-component w-full max-w-[150px] mb-2"
               href="/book/chapter/4541">Next</a>

        There are two identical copies of each nav link on the page
        (one above, one below the chapter). We grab the first match.
        """
        for a in soup.find_all("a", href=True):
            cls = " ".join(a.get("class") or [])
            if "p-button" not in cls:
                continue
            text = a.get_text(strip=True).lower()
            href = a["href"]
            if text == "next" and re.search(r"/book/chapter/\d+", href):
                full = urljoin(url, href)
                if log_fn:
                    log_fn(f"[ReadRift] Next chapter: {full}", "dim")
                return full

        # Fallback: any anchor whose text is "Next" and href matches the pattern
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() == "next":
                href = a["href"]
                if re.search(r"/book/chapter/\d+", href):
                    full = urljoin(url, href)
                    if log_fn:
                        log_fn(f"[ReadRift] Next chapter (fallback text match): {full}", "dim")
                    return full

        if log_fn:
            log_fn("[ReadRift] Could not find next chapter link", "warn")
        return None

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        for a in soup.find_all("a", href=True):
            cls = " ".join(a.get("class") or [])
            if "p-button" not in cls:
                continue
            text = a.get_text(strip=True).lower()
            href = a["href"]
            if text in ("prev", "previous") and re.search(r"/book/chapter/\d+", href):
                return urljoin(url, href)

        # Fallback text match
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True).lower() in ("prev", "previous"):
                href = a["href"]
                if re.search(r"/book/chapter/\d+", href):
                    return urljoin(url, href)

        return None

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        """
        Title block structure:
            <div class="text-center mb-10">
                <h1>Chapter 1</h1>
                <div class="text-2xl">I Regressed</div>
            </div>

        We combine both parts: "Chapter 1 – I Regressed"
        """
        title_container = soup.find(
            "div", class_=lambda c: c and "text-center" in c and "mb-10" in c
        )
        if title_container:
            h1 = title_container.find("h1")
            novel_div = title_container.find(
                "div", class_=lambda c: c and "text-2xl" in c
            )
            chapter_label = clean_text(h1.get_text(strip=True)) if h1 else f"Chapter {fallback_num}"
            novel_name    = clean_text(novel_div.get_text(strip=True)) if novel_div else ""

            if novel_name:
                return f"{chapter_label} – {novel_name}"
            if chapter_label:
                return chapter_label

        # Plain h1 fallback
        h1 = soup.find("h1")
        if h1:
            raw = clean_text(h1.get_text(strip=True))
            if raw and len(raw) > 1:
                return raw

        return None
