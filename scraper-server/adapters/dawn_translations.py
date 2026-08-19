import re
from urllib.parse import urlparse

from .base import BaseAdapter
from .utils import clean_text, strip_watermarks


class DawnTranslationsAdapter(BaseAdapter):
    name     = "dawntranslations"
    priority = 100

    def __init__(self):
        super().__init__()
        self._cached_json = None


    # ── Detection ──────────────────────────────────────────

    def can_handle(self, url: str, html: str) -> float:
        if "dawn-translations.com" not in url:
            return 0.0

        if "/novel/" in url:
            return 1.0

        return 0.6


    # ── Helpers ───────────────────────────────────────────

    def _get_data_url(self, url: str) -> str:
        if url.endswith("/"):
            return url + "__data.json"
        return url + "/__data.json"


    def _fetch_json(self, url, session, log_fn=None):
        if self._cached_json:
            return self._cached_json

        data_url = self._get_data_url(url)

        try:
            r = session.get(data_url, timeout=15)
            r.raise_for_status()
            data = r.json()
            self._cached_json = data
            return data
        except Exception as e:
            if log_fn:
                log_fn(f"[Dawn] JSON fetch failed: {e}", "warn")
            return None


    # ── FIXED: Smart chapter extraction ───────────────────

    def _extract_chapter(self, data):
        """
        Find the REAL chapter content by selecting the largest valid text block
        (avoids ads, review prompts, etc.)
        """

        best_candidate = None
        best_length = 0

        nodes = data.get("nodes", [])

        for node in nodes:
            if not isinstance(node, dict):
                continue

            arr = node.get("data")
            if not isinstance(arr, list):
                continue

            for item in arr:
                if not isinstance(item, dict):
                    continue

                content = item.get("content")
                if not content:
                    continue

                # Strip HTML quickly
                text = re.sub(r"<[^>]+>", "", content)
                word_count = len(text.split())

                # Skip junk / UI blocks
                if word_count < 100:
                    continue

                if word_count > best_length:
                    best_length = word_count
                    best_candidate = item

        return best_candidate


    # ── Content Extraction ─────────────────────────────────

    def extract_content(self, soup, html, url, session, log_fn=None):

        data = self._fetch_json(url, session, log_fn)
        chapter = self._extract_chapter(data)

        if not chapter:
            if log_fn:
                log_fn("[Dawn] Chapter not found", "warn")
            return None

        content = chapter.get("content") or ""

        if not content:
            return None

        # Convert HTML → text
        if "<" in content:
            from bs4 import BeautifulSoup
            content = BeautifulSoup(content, "lxml").get_text("\n")

        text = clean_text(content)
        text = strip_watermarks(text)

        # 🚫 Remove known junk blocks
        bad_phrases = [
            "Leave a Review",
            "You Might Also Like",
            "Exclusive Access",
            "Rate on NU",
            "Similar novels",
        ]

        for phrase in bad_phrases:
            if phrase.lower() in text.lower():
                if log_fn:
                    log_fn("[Dawn] Junk content detected — skipping", "warn")
                return None

        if len(text.split()) < 100:
            return None

        if log_fn:
            log_fn(f"[Dawn] Extracted {len(text.split())} words", "dim")

        return text


    # ── Navigation ─────────────────────────────────────────

    def find_next_url(self, soup, url, html, log_fn=None):

        data = getattr(self, "_cached_json", None)

        if data:
            for node in data.get("nodes", []):
                if not isinstance(node, dict):
                    continue

                arr = node.get("data", [])
                for item in arr:
                    if isinstance(item, dict) and "adjacentChapters" in item:
                        next_ch = item["adjacentChapters"].get("next")

                        if next_ch and next_ch.get("slug"):
                            return self._build_url(url, next_ch["slug"])

        return self._increment_url(url, +1)


    def find_prev_url(self, soup, url, html, log_fn=None):

        data = getattr(self, "_cached_json", None)

        if data:
            for node in data.get("nodes", []):
                if not isinstance(node, dict):
                    continue

                arr = node.get("data", [])
                for item in arr:
                    if isinstance(item, dict) and "adjacentChapters" in item:
                        prev_ch = item["adjacentChapters"].get("prev")

                        if prev_ch and prev_ch.get("slug"):
                            return self._build_url(url, prev_ch["slug"])

        return self._increment_url(url, -1)


    def _increment_url(self, url, step):
        m = re.search(r"/(\d+)/?$", url)
        if not m:
            return None

        num = int(m.group(1)) + step
        if num <= 0:
            return None

        return url.replace(m.group(1), str(num))


    def _build_url(self, base_url, slug):
        p = urlparse(base_url)
        parts = p.path.rstrip("/").split("/")

        if parts[-1].isdigit():
            parts[-1] = slug
        else:
            parts.append(slug)

        return f"{p.scheme}://{p.netloc}{'/'.join(parts)}"


    # ── Title ──────────────────────────────────────────────

    def extract_title(self, soup, fallback_num):

        data = getattr(self, "_cached_json", None)

        if data:
            chapter = self._extract_chapter(data)
            if chapter:
                title = (chapter.get("title") or "").strip()
                if title:
                    return clean_text(title)

        return f"Chapter {fallback_num}"


    # ── Latest Chapter Detection ───────────────────────────

    def detect_latest_chapter(self, index_url, check_selector, session, log_fn=None):

        data_url = self._get_data_url(index_url)

        try:
            r = session.get(data_url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None

        for node in data.get("nodes", []):
            if not isinstance(node, dict):
                continue

            arr = node.get("data", [])
            for item in arr:
                if isinstance(item, dict) and "chapters" in item:
                    chapters = item["chapters"]

                    if not chapters:
                        return None

                    latest = chapters[-1]

                    slug = latest.get("slug")
                    num  = latest.get("number")

                    if not slug or num is None:
                        return None

                    p = urlparse(index_url)
                    base = p.path.rstrip("/")

                    return int(num), f"{p.scheme}://{p.netloc}{base}/{slug}"

        return None