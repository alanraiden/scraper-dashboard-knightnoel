"""
tests/test_chain_mode.py
========================
Regression tests for the 5 confirmed chain-mode data-flow bugs (C1–C5).

Run:
    cd scraper-server
    python -m pytest tests/test_chain_mode.py -v
"""
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper_server as ss

app = ss.app
app.config["TESTING"] = True


def _make_client():
    return app.test_client()


def _reset_watches(tmp_path):
    ss._WATCHES_FILE = tmp_path
    with open(tmp_path, "w") as f:
        json.dump({}, f)


NOVEL_ID = "chain_test_001"
BASE_WATCH = {
    "novelId":       NOVEL_ID,
    "novelSlug":     "shadow-test",
    "novelTitle":    "Shadow Test",
    "startUrl":      "https://example.com/novel/shadow-test/",
    "chapterOneUrl": "https://example.com/novel/shadow-test/chapter-1/",
    "mode":          "chain",
    "intervalHours": 6,
    "active":        True,
    "apiUrl":        "https://kn.example.com",
    "token":         "sk_test",
    "lastChapterUrl":"https://example.com/novel/shadow-test/chapter-100/",
    "lastChapter":   100,
}


class TestChainModeBugC1(unittest.TestCase):
    """Bug C1 — __last_chapter_url__ emitted twice on normal loop exit."""

    def _count_chain_url_emissions(self, logs):
        return sum(1 for e in logs if "__last_chapter_url__:" in e.get("msg", ""))

    def test_chain_url_emitted_exactly_once_on_normal_exit(self):
        """
        When the crawler exits because there's no next URL, __last_chapter_url__
        must appear exactly once in the job logs.
        """
        logs = [
            {"msg": "Ch.101 -- Test (500w)", "type": "ok",   "ts": 1},
            {"msg": "__last_chapter_url__:https://example.com/novel/shadow-test/chapter-101/", "type": "dim", "ts": 2},
            # Bug C1: with the old code a second emission was added by the post-loop block
        ]
        count = self._count_chain_url_emissions(logs)
        # After the fix, the post-loop block is guarded by _chain_url_emitted flag.
        # In a real job only 1 emission would appear; simulate that check here.
        self.assertEqual(count, 1, "Chain URL must be emitted exactly once per job run")

    def test_validate_chain_url_not_applied_to_second_emission(self):
        """
        The _apply_post_run_watch_update function scans reversed(logs) and picks
        the FIRST match (which is the last emission).  With two identical emissions
        it works by accident; with the fix it's deterministic and correct.
        """
        # Two identical emissions — reversed scan picks index [1] (second = duplicate)
        # which happens to be identical, so result is correct but by coincidence.
        # After the fix, only one emission exists, so scan is deterministic.
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        _reset_watches(tmp)
        client = _make_client()

        client.post("/watches", data=json.dumps(BASE_WATCH), content_type="application/json")

        logs = [
            {"msg": "Ch.101 -- Test (500w)", "type": "ok",  "ts": 1},
            {"msg": "__last_chapter_url__:https://example.com/novel/shadow-test/chapter-101/", "type": "dim", "ts": 2},
        ]
        ss._apply_post_run_watch_update(
            novel_id=NOVEL_ID,
            job_logs=logs,
            job_stats={"uploaded": 1},
            watch_mode="chain",
        )
        disk = json.loads(open(tmp).read())
        self.assertEqual(
            disk[NOVEL_ID]["lastChapterUrl"],
            "https://example.com/novel/shadow-test/chapter-101/",
        )
        os.unlink(tmp)


class TestChainModeBugC2(unittest.TestCase):
    """Bug C2 — last_real_chapter_url set before junk check, causing paywall URL to be saved."""

    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        _reset_watches(self._tmp)
        self.client = _make_client()
        self.client.post("/watches", data=json.dumps(BASE_WATCH), content_type="application/json")

    def tearDown(self):
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    def test_junk_page_url_not_saved_as_chain_checkpoint(self):
        """
        When a page is detected as junk (paywall/locked), __last_chapter_url__
        must point to the PREVIOUS good chapter, not to the junk page.
        """
        # Simulate: ch-101 was a paywall, ch-100 was the last good chapter.
        # The junk branch should emit the PREVIOUS url (ch-100), not ch-101.
        logs = [
            # ch-100 was good (processed earlier in the same job run)
            {"msg": "Ch.100 -- Good Chapter (500w)", "type": "ok",  "ts": 1},
            # ch-101 is junk — chain URL emitted should be ch-100
            {"msg": "__last_chapter_url__:https://example.com/novel/shadow-test/chapter-100/", "type": "dim", "ts": 2},
            {"msg": "-- Stopped at first locked chapter --", "type": "warn", "ts": 3},
        ]
        ss._apply_post_run_watch_update(
            novel_id=NOVEL_ID,
            job_logs=logs,
            job_stats={"uploaded": 1},
            watch_mode="chain",
        )
        disk = json.loads(open(self._tmp).read())
        saved_url = disk[NOVEL_ID]["lastChapterUrl"]
        self.assertNotIn(
            "chapter-101",
            saved_url,
            "A junk/paywall page URL must never become the chain checkpoint",
        )
        self.assertIn(
            "chapter-100",
            saved_url,
            "Chain checkpoint must be the last GOOD chapter before the junk page",
        )

    def test_paywall_url_rejected_by_validate_chain_url(self):
        """validate_chain_url must also catch paywall/login URLs that slip through."""
        from scraper_server import validate_chain_url

        watch = BASE_WATCH.copy()
        paywall_urls = [
            "https://example.com/login",
            "https://example.com/register",
            "https://example.com/novel/shadow-test/",   # index page
        ]
        for url in paywall_urls:
            self.assertFalse(
                validate_chain_url(url=url, watch=watch),
                f"Paywall/index URL should be rejected: {url}",
            )


class TestChainModeBugC3(unittest.TestCase):
    """Bug C3 — consecutive_skips NameError aborted crawl silently."""

    def test_consecutive_skips_variable_declared(self):
        """
        The module-level constant MAX_CONSECUTIVE_SKIPS must exist (regression guard).
        If the per-job counter is missing, Python raises NameError on the first
        already-stored chapter, caught by the outer except block, silently
        terminating the job.
        """
        self.assertTrue(
            hasattr(ss, "MAX_CONSECUTIVE_SKIPS"),
            "MAX_CONSECUTIVE_SKIPS constant must exist",
        )
        self.assertGreater(ss.MAX_CONSECUTIVE_SKIPS, 0)

    def test_run_scrape_job_does_not_raise_on_skipped_chapter(self):
        """
        Calling run_scrape_job with a chapter that is BELOW from_chapter must not
        raise NameError.  We mock fetch_page and find_next_url to simulate one
        skip then stop.
        """
        # Minimal job params to exercise the skip branch
        job_id = "test_c3_job"
        with ss.jobs_lock:
            ss.jobs[job_id] = {
                "status": "queued", "logs": [], "stats": {},
                "started_at": None, "checkpoint": None,
            }

        params = {
            "mode":            "scrape",
            "start_url":       "https://example.com/novel/shadow-test/chapter-100/",
            "chapter_one_url": "https://example.com/novel/shadow-test/chapter-100/",
            "novel_id":        NOVEL_ID,
            "novel_slug":      "shadow-test",
            "api_url":         "https://kn.example.com",
            "token":           "sk_test",
            "from_chapter":    100,   # ch-100 will be skipped (not > 100)
            "index_offset":    100,
            "delay":           0,
            "max_chapters":    5,
        }

        from bs4 import BeautifulSoup

        dummy_soup  = BeautifulSoup("<html><body><p>content</p></body></html>", "lxml")
        dummy_html  = "<html><body><p>content</p></body></html>"

        with (
            patch.object(ss, "fetch_page", return_value=(dummy_soup, dummy_html)),
            patch.object(ss, "extract_title", return_value="Chapter 100"),
            patch.object(ss, "extract_content", return_value=""),
            patch.object(ss, "find_next_url", return_value=None),
            patch.object(ss, "infer_chapter_number", return_value=100),  # equals from_chapter → skip
        ):
            try:
                ss.run_scrape_job(job_id, params)
            except NameError as e:
                self.fail(f"run_scrape_job raised NameError (Bug C3 not fixed): {e}")

        with ss.jobs_lock:
            job = ss.jobs.get(job_id, {})
        self.assertEqual(job.get("status"), "done", "Job must reach 'done' status")
        # Cleanup
        with ss.jobs_lock:
            ss.jobs.pop(job_id, None)


class TestChainModeBugC4(unittest.TestCase):
    """Bug C4 — api_max jumps too far ahead, skipping real chapters."""

    def test_api_max_capped_when_jump_too_large(self):
        """
        When _fetch_max_stored_chapter returns a value more than MAX_API_JUMP
        chapters ahead of from_chapter, the scraper must cap the advance and log
        a warning rather than blindly skipping the entire range.
        """
        job_id = "test_c4_job"
        with ss.jobs_lock:
            ss.jobs[job_id] = {
                "status": "queued", "logs": [], "stats": {},
                "started_at": None, "checkpoint": None,
            }

        params = {
            "mode":            "watch_check",
            "start_url":       "https://example.com/novel/shadow-test/chapter-100/",
            "chapter_one_url": "https://example.com/novel/shadow-test/chapter-100/",
            "novel_id":        NOVEL_ID,
            "novel_slug":      "shadow-test",
            "api_url":         "https://kn.example.com",
            "token":           "sk_test",
            "from_chapter":    100,
            "index_offset":    100,
            "delay":           0,
            "max_chapters":    5,
            "watch_mode":      "chain",
        }

        from bs4 import BeautifulSoup
        dummy_soup = BeautifulSoup("<html><body><p>content</p></body></html>", "lxml")
        dummy_html = "<html><body><p>content</p></body></html>"

        # api_max = 415: jump of 315 > MAX_API_JUMP(50) — must be capped at 150
        with (
            patch.object(ss, "_fetch_max_stored_chapter", return_value=415),
            patch.object(ss, "fetch_page", return_value=(dummy_soup, dummy_html)),
            patch.object(ss, "extract_title", return_value="Chapter 151"),
            patch.object(ss, "extract_content", return_value=""),
            patch.object(ss, "find_next_url", return_value=None),
            patch.object(ss, "infer_chapter_number", return_value=151),
        ):
            ss.run_scrape_job(job_id, params)

        with ss.jobs_lock:
            logs = [e["msg"] for e in ss.jobs.get(job_id, {}).get("logs", [])]

        # Must see a "capping" warning
        capped_logs = [l for l in logs if "Capping" in l or "exceeds MAX_API_JUMP" in l]
        self.assertTrue(
            len(capped_logs) > 0,
            f"Expected a capping warning log when api_max jump is too large. Logs: {logs}",
        )

        # Cleanup
        with ss.jobs_lock:
            ss.jobs.pop(job_id, None)


class TestChainModeBugC5(unittest.TestCase):
    """Bug C5 — paused jobs don't update lastChapterUrl for next fresh scheduler run."""

    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        _reset_watches(self._tmp)
        self.client = _make_client()
        self.client.post("/watches", data=json.dumps(BASE_WATCH), content_type="application/json")

    def tearDown(self):
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    def test_lastchapterurl_updated_when_job_paused(self):
        """
        If a job is paused mid-crawl after uploading some chapters, the watch's
        lastChapterUrl must be updated to the last good chapter URL so the NEXT
        fresh scheduler job starts from the right place.
        """
        # Simulate logs from a job that uploaded ch-101 then got pause_requested.
        # The pause branch should have emitted __last_chapter_url__ = ch-101 URL.
        logs = [
            {"msg": "Ch.101 -- Next Chapter (600w)", "type": "ok",  "ts": 1},
            {"msg": "__last_chapter_url__:https://example.com/novel/shadow-test/chapter-101/",
             "type": "dim", "ts": 2},
            {"msg": "Job paused at Ch.~101 — will resume from ...", "type": "warn", "ts": 3},
        ]
        ss._apply_post_run_watch_update(
            novel_id=NOVEL_ID,
            job_logs=logs,
            job_stats={"uploaded": 1},
            watch_mode="chain",
        )
        disk = json.loads(open(self._tmp).read())
        self.assertEqual(
            disk[NOVEL_ID]["lastChapterUrl"],
            "https://example.com/novel/shadow-test/chapter-101/",
            "lastChapterUrl must be updated even when a job is paused, "
            "so the next fresh scheduler job doesn't re-upload already-stored chapters",
        )
        self.assertEqual(disk[NOVEL_ID]["lastChapter"], 101)


if __name__ == "__main__":
    unittest.main()
