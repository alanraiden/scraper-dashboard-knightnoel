"""
mistminthaven.py — Adapter for mistminthaven.com
=================================================
Mistmint Haven is a Next.js App Router site that uses React Server Components
(RSC) streaming.  There is no __NEXT_DATA__ block.  Instead, the page embeds
a sequence of RSC payload chunks via:

    self.__next_f.push([1, "...escaped string..."])

When all chunks are decoded and concatenated they form a React Flight stream
whose lines look like:

    KEY:TYPE_OR_JSON

The relevant line types for us are:

  T-blocks   (large text payloads)
    20:T883b,<h2>...full HTML chapter content...</h2>...
    Format:  {hex_id}:T{hex_byte_length},{raw_content}
    The content is raw HTML of exactly hex_byte_length bytes.

  J-lines    (small JSON objects)
    f:{"chapterTitle":"Eagle Rock","displayChapterNumber":"Chapter 1"}
    f:{"novelSlug":"...", "prevChapterSlug":"$undefined", "nextChapterSlug":"chapter-2"}
    f:{"type":"chapter","chapterId":"44dbb7d4-...","ownerUserId":"..."}

URL pattern:  https://www.mistminthaven.com/novels/{novel-slug}/{chapter-slug}
Example:      https://www.mistminthaven.com/novels/success-story-of-a-legendary-small-company/chapter-1

Navigation uses slug strings (e.g. "chapter-2"), not integer indices.
"$undefined" is Next.js RSC's serialisation of JS undefined — means no prev/next.

Premium / locked chapters have an empty or missing T-block.
"""

import re
import json
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks

MAX_EMPTY_RUN = 5


class MistmintHavenAdapter(BaseAdapter):
    name     = "mistminthaven"
    priority = 100

    def __init__(self):
        super().__init__()
        self._consecutive_empty = 0

    # ── Detection ──────────────────────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "mistminthaven.com" not in url:
            return 0.0
        # Must be a chapter URL: /novels/{novel-slug}/{chapter-slug}
        if not re.search(r"/novels/[^/]+/[^/]+", url):
            return 0.0
        # Next.js App Router RSC fingerprint
        if "__next_f" in html and "self.__next_f.push" in html:
            return 1.0
        return 0.8

    # ── RSC stream extraction ──────────────────────────────────────────────

    def _decode_rsc(self, html: str) -> str:
        """
        Collect all self.__next_f.push([1, "..."]) payloads, JSON-decode the
        inner string (which uses standard JSON string escaping), and concatenate
        them into a single RSC flight stream.
        """
        # The regex is non-greedy and stops at the closing ]) to avoid
        # spanning across multiple push() calls.
        chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
        parts = []
        for chunk in chunks:
            try:
                parts.append(json.loads(f'"{chunk}"'))
            except Exception:
                parts.append(chunk)
        return "".join(parts)

    def _extract_rsc_data(self, html: str) -> dict:
        """
        Parse the RSC stream and return a dict with keys:
          content_html   str | None   — raw HTML of the chapter body
          chapter_title  str | None   — e.g. "Eagle Rock"
          chapter_number str | None   — e.g. "Chapter 1"
          novel_slug     str | None
          chapter_slug   str | None
          next_slug      str | None   — next chapter slug, or None
          prev_slug      str | None   — previous chapter slug, or None
        """
        rsc = self._decode_rsc(html)

        result = {
            "content_html":   None,
            "chapter_title":  None,
            "chapter_number": None,
            "novel_slug":     None,
            "chapter_slug":   None,
            "next_slug":      None,
            "prev_slug":      None,
        }

        # ── T-block: large text payload ────────────────────────────────────
        # Format: {hex_id}:T{hex_length},{content of exactly hex_length bytes}
        # We scan for the first T-block whose content looks like HTML prose.
        # T-block byte lengths are declared in hex and refer to UTF-8 byte counts,
        # not character counts.  Encode to bytes first for correct slicing.
        rsc_bytes = rsc.encode("utf-8")
        for m in re.finditer(r'[0-9a-f]+:T([0-9a-f]+),', rsc):
            length     = int(m.group(1), 16)
            # Convert character offset to byte offset
            start_char = m.end()
            start_byte = len(rsc[:start_char].encode("utf-8"))
            content    = rsc_bytes[start_byte:start_byte + length].decode("utf-8", errors="replace")
            # Must look like HTML and be long enough to be chapter content
            if "<p>" in content and len(content) > 500:
                result["content_html"] = content
                break

        # ── J-lines: small JSON objects ────────────────────────────────────
        # Each non-T line in the RSC stream that is valid JSON carries metadata.
        # We only care about a handful of keys so we scan all JSON objects.
        json_objects = re.findall(r'\{[^{}]{10,500}\}', rsc)
        for raw in json_objects:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue

            # Chapter title / display number
            if "chapterTitle" in obj:
                result["chapter_title"]  = obj.get("chapterTitle")
                result["chapter_number"] = obj.get("displayChapterNumber")

            # Novel + chapter slugs (route params)
            if "novelSlug" in obj and "chapterSlug" in obj:
                result["novel_slug"]   = obj["novelSlug"]
                result["chapter_slug"] = obj["chapterSlug"]

            # Navigation slugs
            if "nextChapterSlug" in obj or "prevChapterSlug" in obj:
                next_s = obj.get("nextChapterSlug")
                prev_s = obj.get("prevChapterSlug")
                # "$undefined" is Next.js RSC serialisation of JS undefined
                result["next_slug"] = None if (not next_s or next_s == "$undefined") else next_s
                result["prev_slug"] = None if (not prev_s or prev_s == "$undefined") else prev_s

        return result

    # ── Content extraction ─────────────────────────────────────────────────

    def extract_content(self, soup, html: str, url: str, session, log_fn=None) -> str | None:
        data = self._extract_rsc_data(html)

        # Cache on soup for navigation helpers
        soup._mmh_data = data

        ch_num = data.get("chapter_number") or data.get("chapter_slug") or "?"

        if not data["content_html"]:
            self._consecutive_empty += 1
            if log_fn:
                log_fn(
                    f"[MistmintHaven] Ch.{ch_num} — no content T-block found "
                    f"({self._consecutive_empty}/{MAX_EMPTY_RUN})", "warn"
                )
            return None

        # Strip HTML tags
        from bs4 import BeautifulSoup as _BS
        text = _BS(data["content_html"], "lxml").get_text(separator="\n")
        text = clean_text(text)
        text = strip_watermarks(text)

        if len(text.split()) >= 50:
            if log_fn:
                log_fn(
                    f"[MistmintHaven] Extracted {len(text.split())} words "
                    f"({ch_num})", "dim"
                )
            self._consecutive_empty = 0
            return text

        self._consecutive_empty += 1
        return None

    # ── Navigation ─────────────────────────────────────────────────────────

    def _build_url(self, base_url: str, novel_slug: str, chapter_slug: str) -> str:
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}/novels/{novel_slug}/{chapter_slug}"

    def _novel_slug_from_url(self, url: str) -> str:
        m = re.search(r"/novels/([^/]+)/", url)
        return m.group(1) if m else ""

    def find_next_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        if self._consecutive_empty >= MAX_EMPTY_RUN:
            if log_fn:
                log_fn(
                    f"[MistmintHaven] {self._consecutive_empty} consecutive empty "
                    "chapters — stopping crawl", "warn"
                )
            return None

        data        = getattr(soup, "_mmh_data", None) or self._extract_rsc_data(html)
        next_slug   = data.get("next_slug")
        novel_slug  = data.get("novel_slug") or self._novel_slug_from_url(url)

        if next_slug and novel_slug:
            return self._build_url(url, novel_slug, next_slug)

        # Fallback: increment trailing integer in chapter slug
        # e.g. chapter-1 → chapter-2
        return self._increment_chapter_slug(url)

    def find_prev_url(self, soup, url: str, html: str, log_fn=None) -> str | None:
        data       = getattr(soup, "_mmh_data", None) or self._extract_rsc_data(html)
        prev_slug  = data.get("prev_slug")
        novel_slug = data.get("novel_slug") or self._novel_slug_from_url(url)

        if prev_slug and novel_slug:
            return self._build_url(url, novel_slug, prev_slug)
        return None

    @staticmethod
    def _increment_chapter_slug(url: str) -> str | None:
        """chapter-3 → chapter-4, or increment trailing integer."""
        m = re.search(r"(.*/)([^/]*?)(\d+)(/?)$", url)
        if m:
            return f"{m.group(1)}{m.group(2)}{int(m.group(3)) + 1}{m.group(4)}"
        return None

    # ── Title ──────────────────────────────────────────────────────────────

    def extract_title(self, soup, fallback_num: int) -> str | None:
        data   = getattr(soup, "_mmh_data", None)
        if not data:
            return None

        ch_num   = data.get("chapter_number", f"Chapter {fallback_num}")
        ch_title = data.get("chapter_title", "")

        if ch_title and ch_title.lower() not in ("null", ""):
            return f"{ch_num}: {clean_text(ch_title)}"
        return ch_num or f"Chapter {fallback_num}"

    # ── Latest chapter detection (watcher) ────────────────────────────────

    def detect_latest_chapter(self, index_url: str, check_selector: str, session, log_fn=None):
        """
        Mistmint Haven's API endpoint returns the full chapter list for a novel.
        The novel slug is extracted from the index_url, then we hit:
            https://api.mistminthaven.com/api/novel/slug/{novel-slug}
        which returns JSON with a volumes[].chapters[] list ordered by chapterIndex.

        Returns (chapter_number, chapter_url) or None.
        """
        # Extract novel slug from the URL
        # index_url may be a chapter URL or the novel index URL
        m = re.search(r"/novels/([^/]+)", index_url)
        if not m:
            if log_fn:
                log_fn("[MistmintHaven] detect: could not extract novel slug from URL", "warn")
            return None

        novel_slug = m.group(1)
        api_url    = f"https://api.mistminthaven.com/api/novel/slug/{novel_slug}"

        try:
            r = session.get(api_url, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if log_fn:
                log_fn(f"[MistmintHaven] detect API error: {e}", "warn")
            return None

        # Response: { data: { volumes: [ { chapters: [ {chapterIndex, slug, isFree, ...} ] } ] } }
        try:
            volumes = data["data"]["volumes"]
            all_chapters = []
            for vol in volumes:
                all_chapters.extend(vol.get("chapters", []))

            if not all_chapters:
                return None

            # Sort by chapterIndex ascending, pick the last free one
            all_chapters.sort(key=lambda c: c.get("chapterIndex", 0))
            free_chapters = [c for c in all_chapters if c.get("isFree", False)]
            latest = free_chapters[-1] if free_chapters else all_chapters[-1]

            ch_index = latest.get("chapterIndex")
            ch_slug  = latest.get("slug", "")

            if ch_index is None or not ch_slug:
                return None

            p           = urlparse(index_url)
            chapter_url = f"{p.scheme}://{p.netloc}/novels/{novel_slug}/{ch_slug}"

            if log_fn:
                log_fn(f"[MistmintHaven] latest chapter: {ch_index} → {chapter_url}", "dim")

            return int(ch_index), chapter_url

        except (KeyError, TypeError, IndexError) as e:
            if log_fn:
                log_fn(f"[MistmintHaven] detect: failed to parse API response: {e}", "warn")
            return None
