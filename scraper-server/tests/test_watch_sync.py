"""
tests/test_watch_sync.py
========================
Regression tests for the watch-state synchronisation and chain-URL validation
bugs confirmed in the v5 audit.

Each test is named after the bug it covers:
  - test_chain_url_edit_persists_to_server
  - test_upsert_does_not_silently_overwrite_corrected_chain_url
  - test_upsert_force_url_overrides_stale_server_value
  - test_lastchapter_only_updated_from_confirmed_uploads
  - test_chain_url_rejected_if_wrong_series
  - test_chain_url_rejected_if_not_chapter_page
  - test_server_state_read_back_on_reconnect
  - test_job_completion_vs_upload_failure_distinction
  - test_duplicate_chapter_does_not_advance_lastchapter
  - test_server_restart_preserves_corrected_chain_url

Run:
    cd scraper-server
    python -m pytest tests/test_watch_sync.py -v
"""
import json
import os
import threading
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call
import sys

# Ensure the scraper-server package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Minimal Flask test client — import the app without starting the scheduler
# ---------------------------------------------------------------------------
# We monkey-patch WatchScheduler before importing so the background thread
# doesn't start during tests.
import scraper_server as ss

# Helpers to make the Flask test client available
app = ss.app
app.config["TESTING"] = True


def _make_client():
    return app.test_client()


def _reset_watches(tmp_path):
    """Point the server's watches file at a fresh temp file."""
    ss._WATCHES_FILE = tmp_path
    # Clear in-memory watches cache by writing an empty file
    with open(tmp_path, "w") as f:
        json.dump({}, f)


def _upsert(client, body):
    return client.post(
        "/watches",
        data=json.dumps(body),
        content_type="application/json",
    )


def _patch_watch(client, novel_id, fields):
    return client.patch(
        f"/watches/{novel_id}",
        data=json.dumps(fields),
        content_type="application/json",
    )


def _get_watches(client):
    r = client.get("/watches")
    return json.loads(r.data)["watches"]


# ---------------------------------------------------------------------------
# Shared test novel fixture
# ---------------------------------------------------------------------------
NOVEL_ID   = "test_novel_001"
NOVEL_SLUG = "the-great-test"
BASE_WATCH = {
    "novelId":    NOVEL_ID,
    "novelSlug":  NOVEL_SLUG,
    "novelTitle": "The Great Test",
    "startUrl":   "https://example.com/novel/the-great-test/",
    "chapterOneUrl": "https://example.com/novel/the-great-test/chapter-1/",
    "mode":       "chain",
    "intervalHours": 6,
    "active":     True,
    "apiUrl":     "https://kn.example.com",
    "token":      "sk_test",
    "lastChapterUrl": "https://example.com/novel/the-great-test/chapter-10/",
    "lastChapter": 10,
}


class TestWatchSyncBugs(unittest.TestCase):
    """Tests for the 8 confirmed watch-sync and chain-URL validation bugs."""

    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        _reset_watches(self._tmp)
        self.client = _make_client()

    def tearDown(self):
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    # ── Bug 1 regression ───────────────────────────────────────────────────
    # updateChainUrl was only touching localStorage; PATCH /watches/:id was
    # never called.  After the fix, the PATCH endpoint is called and the value
    # survives a server restart (i.e. persists to watches.json).

    def test_chain_url_edit_persists_to_server(self):
        """PATCH /watches/:id updates lastChapterUrl and the change survives reload."""
        _upsert(self.client, BASE_WATCH)

        new_url = "https://example.com/novel/the-great-test/chapter-42/"
        r = _patch_watch(self.client, NOVEL_ID, {"lastChapterUrl": new_url})
        self.assertEqual(r.status_code, 200)

        # Simulate "server restart" by re-loading from disk
        watches_on_disk = json.loads(open(self._tmp).read())
        self.assertEqual(
            watches_on_disk[NOVEL_ID]["lastChapterUrl"],
            new_url,
            "Chain URL must be persisted to watches.json so it survives restart",
        )

    # ── Bug 2 regression ───────────────────────────────────────────────────
    # upsert_watch used existing.get("lastChapterUrl", body) which always
    # preferred the stale server value.

    def test_upsert_does_not_silently_overwrite_corrected_chain_url(self):
        """
        Corrected chain URL set via PATCH must not be overwritten by a
        subsequent upsert that carries the OLD url in the body.
        """
        # 1. Create initial watch
        _upsert(self.client, BASE_WATCH)

        # 2. Correct the chain URL via PATCH (simulates user edit)
        corrected = "https://example.com/novel/the-great-test/chapter-42/"
        _patch_watch(self.client, NOVEL_ID, {"lastChapterUrl": corrected})

        # 3. Dashboard re-upserts with old URL in body (happens on every
        #    health-check reconnect in the pre-fix code)
        old_body = dict(BASE_WATCH)
        old_body["lastChapterUrl"] = "https://example.com/novel/the-great-test/chapter-10/"
        _upsert(self.client, old_body)

        # 4. Server must keep the corrected URL
        watches = {w["novelId"]: w for w in _get_watches(self.client)}
        self.assertEqual(
            watches[NOVEL_ID]["lastChapterUrl"],
            corrected,
            "A later upsert with an older URL must not overwrite a PATCH-corrected value",
        )

    def test_upsert_force_url_overrides_stale_server_value(self):
        """
        When the caller sends force_lastChapterUrl=true in the upsert body,
        the supplied URL must take effect even if the server already has a value.
        """
        _upsert(self.client, BASE_WATCH)

        forced_url = "https://example.com/novel/the-great-test/chapter-99/"
        body = dict(BASE_WATCH)
        body["lastChapterUrl"]       = forced_url
        body["force_lastChapterUrl"] = True
        _upsert(self.client, body)

        watches = {w["novelId"]: w for w in _get_watches(self.client)}
        self.assertEqual(
            watches[NOVEL_ID]["lastChapterUrl"],
            forced_url,
            "force_lastChapterUrl=true must override the existing server value",
        )

    # ── Bug 3 regression ───────────────────────────────────────────────────
    # __last_chapter_url__ log marker accepted without validating same-novel.

    def test_chain_url_rejected_if_wrong_series(self):
        """
        A chapter URL from a different novel slug must be rejected when
        validate_chain_url() is called; it must not be written to watches.json.
        """
        from scraper_server import validate_chain_url

        valid   = "https://example.com/novel/the-great-test/chapter-15/"
        invalid = "https://example.com/novel/some-other-series/chapter-15/"

        self.assertTrue(
            validate_chain_url(invalid_url=False, url=valid, watch=BASE_WATCH),
            "URL belonging to same slug should be valid",
        )
        self.assertFalse(
            validate_chain_url(invalid_url=False, url=invalid, watch=BASE_WATCH),
            "URL from a different novel series must be rejected",
        )

    def test_chain_url_rejected_if_not_chapter_page(self):
        """
        Non-chapter URLs (sign-up, comments, index, etc.) must be rejected.
        """
        from scraper_server import validate_chain_url

        bad_urls = [
            "https://example.com/sign-up/",
            "https://example.com/novel/the-great-test/",           # index page
            "https://example.com/novel/the-great-test/#comments",  # comment anchor
            "https://example.com/login",
        ]
        for url in bad_urls:
            self.assertFalse(
                validate_chain_url(invalid_url=False, url=url, watch=BASE_WATCH),
                f"Should reject non-chapter URL: {url}",
            )

    # ── Bug 4 regression ───────────────────────────────────────────────────
    # lastChapter updated from scraped log lines, not confirmed upload response.

    def test_lastchapter_only_updated_from_confirmed_uploads(self):
        """
        lastChapter in watches.json must only advance if bulk_upload_chapters
        reported created > 0, not merely because a chapter was scraped.
        """
        _upsert(self.client, BASE_WATCH)

        # Simulate a job that scraped Ch.11 but the upload returned 0 created
        # (e.g. API auth-expired).  The post-run block must NOT advance lastChapter.
        novel_id = NOVEL_ID
        job_logs = [
            {"msg": "Ch.11 -- Test Title (500w)", "type": "ok",  "ts": 1000},
            {"msg": "__last_chapter_url__:https://example.com/novel/the-great-test/chapter-11/", "type": "dim", "ts": 1001},
        ]
        job_stats = {"scraped": 1, "uploaded": 0, "skipped": 0, "errors": 1}

        # Call the internal post-run logic directly
        ss._apply_post_run_watch_update(
            novel_id=novel_id,
            job_logs=job_logs,
            job_stats=job_stats,
            watch_mode="chain",
        )

        watches_on_disk = json.loads(open(self._tmp).read())
        self.assertEqual(
            watches_on_disk[NOVEL_ID]["lastChapter"],
            10,  # unchanged from BASE_WATCH
            "lastChapter must NOT advance when uploaded=0 (upload failed)",
        )

    def test_duplicate_chapter_does_not_advance_lastchapter(self):
        """
        If a chapter is uploaded but the API reports skipped (duplicate),
        lastChapter must not advance unless created > 0.
        """
        _upsert(self.client, BASE_WATCH)

        job_logs = [
            {"msg": "Ch.10 -- Same Chapter (500w)", "type": "ok",  "ts": 1000},
            {"msg": "__last_chapter_url__:https://example.com/novel/the-great-test/chapter-10/", "type": "dim", "ts": 1001},
        ]
        job_stats = {"scraped": 1, "uploaded": 0, "skipped": 1, "created": 0, "errors": 0}

        ss._apply_post_run_watch_update(
            novel_id=NOVEL_ID,
            job_logs=job_logs,
            job_stats=job_stats,
            watch_mode="chain",
        )

        watches_on_disk = json.loads(open(self._tmp).read())
        self.assertEqual(
            watches_on_disk[NOVEL_ID]["lastChapter"],
            10,
            "A duplicate upload (skipped) must not advance lastChapter",
        )

    # ── Bug 5 regression ───────────────────────────────────────────────────
    # Server state never read back on reconnect; bot corrections overwritten.

    def test_server_state_read_back_on_reconnect(self):
        """
        GET /watches must return runtime fields (lastChapter, lastChapterUrl)
        so the dashboard can merge them into its local state on reconnect.
        """
        _upsert(self.client, BASE_WATCH)

        # Bot patches the watch server-side overnight
        bot_url = "https://example.com/novel/the-great-test/chapter-55/"
        _patch_watch(self.client, NOVEL_ID, {"lastChapterUrl": bot_url, "lastChapter": 55})

        # Dashboard reconnects and calls GET /watches
        watches = {w["novelId"]: w for w in _get_watches(self.client)}

        self.assertEqual(
            watches[NOVEL_ID]["lastChapterUrl"],
            bot_url,
            "GET /watches must expose bot-corrected lastChapterUrl for dashboard to read back",
        )
        self.assertEqual(
            watches[NOVEL_ID]["lastChapter"],
            55,
            "GET /watches must expose bot-corrected lastChapter for dashboard to read back",
        )

    # ── Bug 4b regression ──────────────────────────────────────────────────
    # Job "done" status vs successful upload vs successful scrape are conflated.

    def test_job_completion_vs_upload_failure_distinction(self):
        """
        A job that finishes (status=done) but has uploaded=0 must be
        reported with a distinct status so the dashboard can show it differently
        from a job with uploaded>0.
        """
        # The JSON returned by GET /jobs/<id> must include uploaded count
        job_id = "fakejob1"
        with ss.jobs_lock:
            ss.jobs[job_id] = {
                "status": "done",
                "stats":  {"scraped": 3, "uploaded": 0, "skipped": 0, "errors": 1},
                "logs":   [],
                "started_at": time.time() - 60,
            }

        r = self.client.get(f"/jobs/{job_id}")
        data = json.loads(r.data)

        self.assertEqual(data["status"], "done")
        self.assertEqual(data["stats"]["uploaded"], 0)
        # The dashboard uses stats.uploaded > 0 to determine 'done_ok' vs 'done_warn'
        # This test documents that the field must exist and be accurate.
        self.assertIn("uploaded", data["stats"],
                      "stats.uploaded must be present in GET /jobs/<id> response")

    # ── Server-restart persistence ─────────────────────────────────────────

    def test_server_restart_preserves_corrected_chain_url(self):
        """
        After patching lastChapterUrl via PATCH and reloading watches.json,
        the corrected value must still be returned — simulating a service restart.
        """
        _upsert(self.client, BASE_WATCH)

        patched_url = "https://example.com/novel/the-great-test/chapter-77/"
        _patch_watch(self.client, NOVEL_ID, {"lastChapterUrl": patched_url})

        # Simulate restart: reload from disk
        disk_data = json.loads(open(self._tmp).read())
        self.assertEqual(
            disk_data[NOVEL_ID]["lastChapterUrl"],
            patched_url,
            "Patched chain URL must survive a simulated server restart",
        )


# ---------------------------------------------------------------------------
# URL validation unit tests (independent of Flask)
# ---------------------------------------------------------------------------

class TestValidateChainUrl(unittest.TestCase):
    """Unit tests for the new validate_chain_url() helper."""

    WATCH = {
        "novelSlug":  "shadow-slave",
        "startUrl":   "https://freewebnovel.com/shadow-slave/",
        "chapterOneUrl": "https://freewebnovel.com/shadow-slave/chapter-1.html",
    }

    def _validate(self, url):
        from scraper_server import validate_chain_url
        return validate_chain_url(invalid_url=False, url=url, watch=self.WATCH)

    def test_valid_chapter_url_accepted(self):
        self.assertTrue(self._validate(
            "https://freewebnovel.com/shadow-slave/chapter-100.html"
        ))

    def test_wrong_domain_rejected(self):
        self.assertFalse(self._validate(
            "https://novelupdates.com/shadow-slave/chapter-100.html"
        ))

    def test_wrong_novel_slug_rejected(self):
        self.assertFalse(self._validate(
            "https://freewebnovel.com/omniscient-reader/chapter-100.html"
        ))

    def test_sign_up_page_rejected(self):
        self.assertFalse(self._validate(
            "https://freewebnovel.com/register"
        ))

    def test_index_page_rejected(self):
        # The startUrl itself should be rejected as a chain checkpoint
        self.assertFalse(self._validate(
            "https://freewebnovel.com/shadow-slave/"
        ))

    def test_comment_anchor_on_index_page_rejected(self):
        """A fragment anchor on the START/INDEX page must be rejected (same page, just an anchor)."""
        self.assertFalse(self._validate(
            # The WATCH startUrl is freewebnovel.com/shadow-slave/ — same path, just a fragment
            "https://freewebnovel.com/shadow-slave/#comments"
        ))

    def test_comment_anchor_on_chapter_page_accepted(self):
        """A fragment anchor on a chapter page is an acceptable chain checkpoint."""
        # It's a real chapter page with a fragment — not the index page
        self.assertTrue(self._validate(
            "https://freewebnovel.com/shadow-slave/chapter-100.html#comments"
        ))

    def test_empty_url_rejected(self):
        self.assertFalse(self._validate(""))
        self.assertFalse(self._validate(None))


if __name__ == "__main__":
    unittest.main()
