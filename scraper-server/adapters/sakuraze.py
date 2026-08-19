"""
sakuraze.py — Sakuraze (sakuraze.vercel.app) adapter
======================================================
Sakuraze is a fully client-side React SPA (Vite + React Router, hosted on
Vercel).  The HTML served to scrapers is just:

    <body><div id="root"></div></body>

There is NO server-side rendering — all content is fetched at runtime from
a Supabase backend via the public REST API.  BeautifulSoup cannot extract
anything from the raw HTML.

Strategy
--------
This adapter bypasses HTML parsing entirely.  Instead, it talks directly to
the Supabase REST API using the public anon key embedded in the site's JS
bundle.  The flow mirrors exactly what the browser does:

  1. GET /rest/v1/novels?slug=eq.{slug}&select=id,title,...  → novel_id
  2. GET /rest/v1/chapters?novel_id=eq.{novel_id}
                          &chapter_number=eq.{num}
                          &select=id,title,content,chapter_number,...
                          → chapter row

Next/prev navigation is derived by incrementing/decrementing chapter_number
and constructing the canonical URL — no HTML link parsing needed.

Detection
---------
  - "sakuraze.vercel.app" in URL                   → 1.0 (exact)
  - "sakuraze" anywhere in HTML (meta/title)        → 0.7
  - Supabase project ID "hlzjslwrhabsxdskinwd"      → 0.9

URL pattern:  /novel/{slug}/chapter/{number}
"""

import re
import json
from urllib.parse import urljoin

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks

# ── Supabase credentials (public anon key — safe to embed) ───────────────────
_SUPABASE_URL = "https://hlzjslwrhabsxdskinwd.supabase.co"
_ANON_KEY     = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhsempzbHdyaGFic3hkc2tpbndkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE0ODYyNTEsImV4cCI6MjA3NzA2MjI1MX0"
    "._xpIADB4jDsXIGp92-sFTQW8KAbli-Lr99ggJ8DRyX8"
)
_API_HEADERS = {
    "apikey":        _ANON_KEY,
    "Authorization": f"Bearer {_ANON_KEY}",
    "Content-Type":  "application/json",
}

# Cache novel slug → id to avoid redundant API calls within a job
_novel_id_cache: dict[str, str] = {}


def _parse_url(url: str) -> tuple[str, int] | None:
    """
    Parse /novel/{slug}/chapter/{number} → (slug, chapter_number).
    Returns None if the URL doesn't match.
    """
    m = re.search(r"/novel/([^/]+)/chapter/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2))
    return None


def _get_novel_id(slug: str, session, log_fn=None) -> str | None:
    """Resolve a novel slug to its Supabase UUID, with in-process caching."""
    if slug in _novel_id_cache:
        return _novel_id_cache[slug]

    api_url = (
        f"{_SUPABASE_URL}/rest/v1/novels"
        f"?slug=eq.{slug}"
        f"&select=id,title,slug"
        f"&limit=1"
    )
    try:
        r = session.get(api_url, headers=_API_HEADERS, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if rows:
            novel_id = rows[0]["id"]
            _novel_id_cache[slug] = novel_id
            if log_fn:
                log_fn(f"[Sakuraze] Resolved slug '{slug}' → {novel_id}", "dim")
            return novel_id
        if log_fn:
            log_fn(f"[Sakuraze] Novel not found for slug '{slug}'", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"[Sakuraze] Novel lookup failed: {e}", "err")
    return None


def _fetch_chapter_row(novel_id: str, chapter_number: int, session, log_fn=None) -> dict | None:
    """Fetch a single chapter row from Supabase."""
    api_url = (
        f"{_SUPABASE_URL}/rest/v1/chapters"
        f"?novel_id=eq.{novel_id}"
        f"&chapter_number=eq.{chapter_number}"
        f"&select=id,title,content,chapter_number,is_premium,coin_cost,scheduled_free_at"
        f"&limit=1"
    )
    try:
        r = session.get(api_url, headers=_API_HEADERS, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0]
        if log_fn:
            log_fn(f"[Sakuraze] Chapter {chapter_number} not found", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"[Sakuraze] Chapter fetch failed: {e}", "err")
    return None


class SakurazeAdapter(BaseAdapter):
    name     = "sakuraze"
    priority = 100

    # ── Detection ─────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "sakuraze.vercel.app" in url:
            return 1.0

        score = 0.0

        # Supabase project fingerprint in any loaded JS
        if "hlzjslwrhabsxdskinwd.supabase.co" in html:
            score += 0.9

        # Site name in meta tags / title
        if "sakuraze" in html.lower():
            score += 0.7

        # React SPA shell with /novel/.../chapter/ URL pattern
        if re.search(r"/novel/[^/]+/chapter/\d+", url):
            score += 0.2

        return min(score, 1.0)

    def __init__(self):
        # Cache the title fetched from the API so extract_title() can
        # return it.  The server calls extract_title before extract_content,
        # but uses the same adapter instance throughout a job — so the title
        # cached during chapter N's extract_content is available when
        # extract_title is called for the same URL on the test-url endpoint.
        self._title_cache: dict[str, str] = {}

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        parsed = _parse_url(url)
        if not parsed:
            if log_fn:
                log_fn("[Sakuraze] URL doesn't match /novel/{slug}/chapter/{num}", "warn")
            return None

        slug, chapter_number = parsed

        novel_id = _get_novel_id(slug, session, log_fn)
        if not novel_id:
            return None

        row = _fetch_chapter_row(novel_id, chapter_number, session, log_fn)
        if not row:
            return None

        # Premium / locked chapter
        if row.get("is_premium") and (row.get("coin_cost") or 0) > 0:
            if log_fn:
                log_fn(
                    f"[Sakuraze] Ch.{chapter_number} is premium (cost: {row['coin_cost']} coins)",
                    "warn",
                )
            return None

        raw_content = row.get("content") or ""
        if not raw_content:
            if log_fn:
                log_fn(f"[Sakuraze] Ch.{chapter_number} has empty content", "warn")
            return None

        # Content may contain HTML tags (TipTap-rendered HTML stored in DB)
        if "<" in raw_content and ">" in raw_content:
            from bs4 import BeautifulSoup as BS
            text = clean_text(BS(raw_content, "lxml").get_text(separator="\n"))
        else:
            text = clean_text(raw_content)

        # Cache title so extract_title() can return it for this URL
        if row.get("title"):
            self._title_cache[url] = clean_text(row["title"])

        if log_fn:
            log_fn(f"[Sakuraze] Fetched Ch.{chapter_number} via Supabase API ({len(text.split())} words)", "dim")

        return strip_watermarks(text)

    # ── Navigation ─────────────────────────────────────────────────────────

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        parsed = _parse_url(url)
        if not parsed:
            return None
        slug, chapter_number = parsed
        # Infer the next chapter URL from the slug + chapter_number pattern.
        # The crawler will call extract_content on it and discover naturally
        # if chapter N+1 doesn't exist (empty content → is_junk_page → stop).
        next_url = f"https://sakuraze.vercel.app/novel/{slug}/chapter/{chapter_number + 1}"
        if log_fn:
            log_fn(f"[Sakuraze] Next chapter (inferred): {next_url}", "dim")
        return next_url

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        parsed = _parse_url(url)
        if not parsed:
            return None
        slug, chapter_number = parsed
        if chapter_number <= 1:
            return None
        return f"https://sakuraze.vercel.app/novel/{slug}/chapter/{chapter_number - 1}"

    # ── Title extraction ───────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        # The title cache is populated by extract_content().
        # On the /test-url endpoint the server calls extract_title first,
        # so the cache will be empty — we return None and let the server
        # fall back to the <title> tag (which at least has the novel name).
        # During a real scrape job, extract_content is called first on the
        # same URL, so the next chapter's title is already cached.
        # Either way, returning None here is safe — the server always has
        # a fallback path.
        if self._title_cache:
            # Return the most recently cached title (last URL processed)
            return next(reversed(self._title_cache.values()), None)
        return None

    def extract_title_for_url(self, url: str, session, log_fn=None) -> str | None:
        """
        Extended helper called by the patched job path (see note below).
        Not part of BaseAdapter — used when the server wants a title from us.
        """
        parsed = _parse_url(url)
        if not parsed:
            return None
        slug, chapter_number = parsed
        novel_id = _get_novel_id(slug, session, log_fn)
        if not novel_id:
            return None
        row = _fetch_chapter_row(novel_id, chapter_number, session, log_fn)
        if row and row.get("title"):
            return clean_text(row["title"])
        return f"Chapter {chapter_number}"
