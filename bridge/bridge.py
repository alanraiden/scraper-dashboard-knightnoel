"""
bridge.py — Local Python Bridge Server
=======================================
Runs on http://localhost:5174
The React dashboard sends scrape/watch jobs here.
This server runs novel_scraper.py logic directly (no CORS issues).

Start it with:
    python bridge.py

Requirements (same as novel_scraper.py):
    pip install flask flask-cors requests beautifulsoup4 lxml
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import threading, queue, json, time, re, os, sys

# ── Make sure novel_scraper.py is importable from same folder ─────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import novel_scraper as scraper
except ImportError:
    print("ERROR: novel_scraper.py not found next to bridge.py")
    print("Make sure both files are in the same folder.")
    sys.exit(1)

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173", "http://127.0.0.1:5173"])

# ── Active jobs: { job_id: { queue, thread, status } } ───────────────────────
jobs = {}

def make_job_id():
    import uuid
    return str(uuid.uuid4())[:8]


# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({ "status": "ok", "version": "1.0" })


# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPE JOB  —  POST /scrape
#  Body: { startUrl, fromChapter, maxChapters, delay, novelId, apiUrl, token }
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scrape", methods=["POST"])
def start_scrape():
    data = request.json or {}
    start_url    = data.get("startUrl", "").strip()
    from_chapter = int(data.get("fromChapter", 0))
    max_chapters = int(data.get("maxChapters", 500))
    delay        = float(data.get("delay", 1.2))
    novel_id     = data.get("novelId", "").strip()
    api_url      = data.get("apiUrl", "").strip()
    token        = data.get("token", "").strip()

    if not start_url:
        return jsonify({ "error": "startUrl is required" }), 400
    if not novel_id or not api_url or not token:
        return jsonify({ "error": "novelId, apiUrl, and token are required" }), 400

    job_id = make_job_id()
    q      = queue.Queue()
    jobs[job_id] = { "queue": q, "status": "running", "stopped": False }

    def run():
        cfg = scraper.DEFAULT_CONFIG.copy()
        cfg["delay"]        = delay
        cfg["max_chapters"] = max_chapters

        def log(msg, level="info"):
            q.put({ "type": "log", "level": level, "msg": msg })

        log(f"Starting scrape from: {start_url}")
        log(f"Skipping chapters up to: Ch.{from_chapter}")

        chapters = []
        url      = start_url
        index    = 1
        import requests as req_lib
        session  = req_lib.Session()
        visited  = set()

        while url:
            if jobs[job_id]["stopped"]:
                log("Stopped by user.", "warn")
                break
            if url in visited:
                break
            if len(chapters) >= max_chapters:
                log(f"Max chapters ({max_chapters}) reached.", "warn")
                break
            visited.add(url)

            log(f"[{index}] {url}")
            soup = scraper.fetch_page(url, session)
            if soup is None:
                log(f"Failed to fetch {url}", "error")
                break

            title   = scraper.extract_title(soup, index)
            content = scraper.extract_content(soup, cfg)
            ch_num  = scraper.infer_chapter_number(title, index)

            if ch_num <= from_chapter:
                log(f"~ Ch.{ch_num} already stored, skipping", "dim")
            else:
                word_count = len(content.split()) if content else 0
                if not content or word_count < 10:
                    log(f"⚠ Ch.{ch_num} — {title} (no content, skipping)", "warn")
                else:
                    log(f"✓ Ch.{ch_num} — {title} ({word_count}w)", "ok")
                    chapters.append({
                        "Chapter Number": ch_num,
                        "Title":          title,
                        "Content":        content,
                    })
                    # Push immediately
                    pushed, _ = scraper.push_chapters_to_api(
                        [{ "Chapter Number": ch_num, "Title": title, "Content": content }],
                        novel_id, api_url, token
                    )
                    if pushed:
                        q.put({ "type": "uploaded", "number": ch_num, "title": title })
                    else:
                        log(f"✗ Upload failed for Ch.{ch_num}", "error")

            next_url = scraper.find_next_url(soup, url, cfg)
            if not next_url or next_url == url:
                log("No more chapters found.", "info")
                break
            url    = next_url
            index += 1
            time.sleep(delay)

        q.put({ "type": "done", "total": len(chapters) })
        jobs[job_id]["status"] = "done"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    jobs[job_id]["thread"] = t

    return jsonify({ "jobId": job_id })


# ══════════════════════════════════════════════════════════════════════════════
#  STREAM JOB LOGS  —  GET /scrape/<job_id>/stream  (Server-Sent Events)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scrape/<job_id>/stream")
def stream_job(job_id):
    if job_id not in jobs:
        return jsonify({ "error": "Job not found" }), 404

    def generate():
        q = jobs[job_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "done":
                    break
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ══════════════════════════════════════════════════════════════════════════════
#  STOP JOB  —  POST /scrape/<job_id>/stop
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/scrape/<job_id>/stop", methods=["POST"])
def stop_job(job_id):
    if job_id not in jobs:
        return jsonify({ "error": "Job not found" }), 404
    jobs[job_id]["stopped"] = True
    return jsonify({ "ok": True })


# ══════════════════════════════════════════════════════════════════════════════
#  WATCH CHECK  —  POST /watch
#  Body: { startUrl, mode, checkSelector, fromChapter, novelId, apiUrl, token }
#  Runs one check cycle synchronously, streams logs via SSE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/watch", methods=["POST"])
def watch_check():
    data          = request.json or {}
    start_url     = data.get("startUrl", "").strip()
    mode          = data.get("mode", "index")
    check_sel     = data.get("checkSelector", "").strip()
    from_chapter  = int(data.get("fromChapter", 0))
    novel_id      = data.get("novelId", "").strip()
    api_url       = data.get("apiUrl", "").strip()
    token         = data.get("token", "").strip()

    if not start_url or not novel_id or not api_url or not token:
        return jsonify({ "error": "startUrl, novelId, apiUrl, token required" }), 400

    job_id = make_job_id()
    q      = queue.Queue()
    jobs[job_id] = { "queue": q, "status": "running", "stopped": False }

    def run():
        import requests as req_lib
        session = req_lib.Session()
        cfg     = scraper.DEFAULT_CONFIG.copy()

        def log(msg, level="info"):
            q.put({ "type": "log", "level": level, "msg": msg })

        log(f"Watch check: {start_url} (mode={mode})")

        # ── Detect latest chapter on source site ──────────────────────────
        site_latest = 0
        chapter_start_url = start_url

        try:
            if mode == "index":
                soup = scraper.fetch_page(start_url, session)
                if soup is None:
                    log("Could not fetch index page", "error")
                    q.put({ "type": "done", "total": 0 })
                    return

                # Use check_sel if provided, else auto-detect
                site_latest, chapter_start_url = _detect_latest_from_index(
                    soup, start_url, check_sel, log
                )
            else:
                # mode == "latest" — fetch the latest chapter URL directly
                soup = scraper.fetch_page(start_url, session)
                if soup is None:
                    log("Could not fetch latest chapter page", "error")
                    q.put({ "type": "done", "total": 0 })
                    return
                title       = scraper.extract_title(soup, 0)
                site_latest = scraper.infer_chapter_number(title, 0)
                chapter_start_url = start_url
        except Exception as e:
            log(f"Detection error: {e}", "error")
            q.put({ "type": "done", "total": 0 })
            return

        log(f"Site latest: Ch.{site_latest} | Stored: Ch.{from_chapter}", "info")

        if site_latest <= from_chapter:
            log("No new chapters.", "dim")
            q.put({ "type": "done", "total": 0, "newLast": from_chapter })
            return

        count = site_latest - from_chapter
        log(f"{count} new chapter(s) detected! Fetching…", "ok")

        # ── Scrape new chapters ────────────────────────────────────────────
        if mode == "latest":
            new_chapters = _scrape_backward(
                chapter_start_url, from_chapter, cfg, session, jobs[job_id], log
            )
        else:
            new_chapters = _scrape_forward_from(
                chapter_start_url, from_chapter, cfg, session, jobs[job_id], log
            )

        # ── Push to API ────────────────────────────────────────────────────
        uploaded = 0
        new_last = from_chapter
        for ch in new_chapters:
            if jobs[job_id]["stopped"]:
                break
            ok, _ = scraper.push_chapters_to_api([ch], novel_id, api_url, token)
            if ok:
                uploaded += 1
                new_last  = max(new_last, ch["Chapter Number"])
                q.put({ "type": "uploaded", "number": ch["Chapter Number"], "title": ch["Title"] })
                log(f"✓ Uploaded Ch.{ch['Chapter Number']} — {ch['Title']}", "ok")
            else:
                log(f"✗ Upload failed Ch.{ch['Chapter Number']}", "error")
            time.sleep(0.15)

        q.put({ "type": "done", "total": uploaded, "newLast": new_last })
        jobs[job_id]["status"] = "done"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    jobs[job_id]["thread"] = t

    return jsonify({ "jobId": job_id })


# ── Internal helpers ──────────────────────────────────────────────────────────

def _detect_latest_from_index(soup, base_url, check_sel, log):
    """Return (latest_chapter_num, chapter_url) from a novel index page."""
    import re as re_mod
    from urllib.parse import urljoin

    if check_sel:
        try:
            el = soup.select_one(check_sel)
            if el:
                m = re_mod.search(r"(\d+)", el.get_text())
                if m:
                    return int(m.group(1)), urljoin(base_url, el.get("href", ""))
        except Exception:
            pass

    # Auto-detect: find all chapter links
    list_selectors = [
        ".wp-manga-chapter a", ".chapter-list li a", ".chapters li a",
        "ul.chapter-list a", "ul.row-content-chapter li a",
        ".listing-chapters_wrap li a", ".eph-num a",
        "li.chapter a", "li[class*='chapter'] a",
    ]
    candidates = []
    for sel in list_selectors:
        try:
            for a in soup.select(sel):
                txt  = a.get_text(strip=True)
                href = a.get("href", "")
                m    = re_mod.search(r"chapter[\s\-_#]?(\d+)", txt, re_mod.I) \
                    or re_mod.search(r"ch[\s\-_.]?(\d+)", txt, re_mod.I) \
                    or re_mod.search(r"chapter[\-_]?(\d+)", href, re_mod.I)
                if m:
                    candidates.append((int(m.group(1)), urljoin(base_url, href)))
        except Exception:
            pass

    if not candidates:
        for a in soup.find_all("a", href=True):
            txt  = a.get_text(strip=True)
            href = a["href"]
            m    = re_mod.search(r"chapter[\s\-_#]?(\d+)", txt, re_mod.I) \
                or re_mod.search(r"chapter[\-_\/]?(\d+)", href, re_mod.I)
            if m:
                candidates.append((int(m.group(1)), urljoin(base_url, href)))

    if candidates:
        best = max(candidates, key=lambda x: x[0])
        log(f"Detected latest: Ch.{best[0]}", "dim")
        return best
    log("Could not detect latest chapter from index page", "warn")
    return 0, base_url


def _scrape_forward_from(start_url, from_chapter, cfg, session, job, log):
    """Crawl forward, return list of chapter dicts."""
    from urllib.parse import urljoin
    results, url, index = [], start_url, 1
    visited = set()
    while url:
        if job["stopped"] or url in visited:
            break
        visited.add(url)
        soup = scraper.fetch_page(url, session)
        if not soup:
            log(f"Fetch failed: {url}", "error")
            break
        title   = scraper.extract_title(soup, index)
        content = scraper.extract_content(soup, cfg)
        ch_num  = scraper.infer_chapter_number(title, index)
        if ch_num > from_chapter:
            if content and len(content.split()) >= 10:
                results.append({ "Chapter Number": ch_num, "Title": title, "Content": content })
                log(f"✓ Ch.{ch_num} — {title} ({len(content.split())}w)", "ok")
            else:
                log(f"⚠ Ch.{ch_num} skipped (no text content)", "warn")
        next_url = scraper.find_next_url(soup, url, cfg)
        if not next_url or next_url == url:
            break
        url = next_url; index += 1
        import time as t; t.sleep(cfg["delay"])
    return results


def _scrape_backward(latest_url, from_chapter, cfg, session, job, log):
    """Walk backwards via prev links, return in forward order."""
    results, url = [], latest_url
    visited = set()
    while url:
        if job["stopped"] or url in visited:
            break
        visited.add(url)
        soup = scraper.fetch_page(url, session)
        if not soup:
            break
        title   = scraper.extract_title(soup, 0)
        content = scraper.extract_content(soup, cfg)
        ch_num  = scraper.infer_chapter_number(title, 0)
        if ch_num <= from_chapter:
            log(f"Reached Ch.{ch_num} (already stored). Done.", "info")
            break
        if content and len(content.split()) >= 10:
            results.append({ "Chapter Number": ch_num, "Title": title, "Content": content })
            log(f"✓ Ch.{ch_num} — {title}", "ok")
        prev_links = cfg.get("prev_selectors", [
            "a.prev_page","a[rel='prev']","a.prev-chap","a#prev_chap",
            "a.btn-prev","a[title*='Prev']","a[title*='Previous']",
        ])
        prev_url = scraper.find_next_url(soup, url, {"next_selectors": prev_links})
        if not prev_url or prev_url == url:
            break
        url = prev_url
        import time as t; t.sleep(cfg["delay"])
    results.reverse()
    return results


if __name__ == "__main__":
    print("=" * 50)
    print("  NovaSphere Python Bridge")
    print("  http://localhost:5174")
    print("=" * 50)
    print("Keep this running while using the dashboard.")
    print("Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=5174, debug=False, threaded=True)
