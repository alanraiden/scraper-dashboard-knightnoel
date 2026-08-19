# Changes — Knight Novel scraper target

## Why your "Failed to fetch" happened

The screen you hit ("Sign in with the Google account you use on your
website" / "Backend URL") is `ConnectPanel.jsx` — the **whole-app login
gate**. It's built entirely around your old bespoke backend: it POSTs to
`{backendUrl}/api/auth/login` (or `/api/auth/google`), expects back
`{token, user}`, and then uses that bearer token for every other call in
the app (novel list, etc.).

Knight Novel doesn't have those routes — it uses NextAuth, whose routes
are `/api/auth/csrf` and `/api/auth/callback/credentials`, not
`/api/auth/login`. So pointing this screen at `http://localhost:3000/api`
was never going to work, regardless of the exact URL — it's not a typo or
config problem, it's two different auth systems. (You'd likely have hit a
CORS error too, even if the route existed: this dashboard runs on its own
port, and Next.js API routes don't send CORS headers for cross-origin
browser requests by default.)

The Knight Novel login I built last round happens somewhere else
entirely: **inside the scrape job itself**, run by the local Python
server. That's server-to-server, so it isn't subject to browser CORS at
all, and it speaks NextAuth's real login flow (csrf token → credentials
callback), not the old `/api/auth/login`.

## What changed to fix this

- **`ConnectPanel.jsx`**: renamed "NovaSphere Scraper" → "Knight Novel
  Scraper", and added a **"Skip — scrape into Knight Novel only"** button
  under the login form. Click that instead of trying to sign in — it
  bypasses this legacy gate entirely (no backend URL needed here) and
  drops you straight into the dashboard.
- **`App.jsx`**: renamed the "NovaSphere" logo text to "Knight Novel".
  Skips the legacy `fetchNovels()` call when you used the skip button
  (nothing to fetch — there's no novel list on this backend for Knight
  Novel). Added a **"New Knight Novel Job"** button next to Refresh that
  opens the scrape modal directly, without needing an existing novel
  card from the legacy backend.
- **`ScrapeModal.jsx`**: when opened without a legacy novel (via the new
  button above), it now defaults "Target site" to Knight Novel
  automatically, and the "Legacy backend" option is disabled/greyed out
  in that case since there's no `novel._id` to scrape into.

## How to actually run it locally now

1. `npm run dev` in Knight Novel (should be on `http://localhost:3000`).
2. Make sure the account you'll use has `role: "admin"` — run
   `npm run make-admin -- you@example.com` there if you haven't.
3. In the scraper dashboard: on the login screen, click **"Skip —
   scrape into Knight Novel only"**.
4. Click **"New Knight Novel Job"**.
5. In the modal: Target site is already "Knight Novel" — fill in
   - Knight Novel site URL: `http://localhost:3000`
   - Novel slug: an existing novel's slug on Knight Novel
   - Admin email / password: the admin account from step 2
   - Starting chapter URL, as usual
6. Start the local Python server first (`python
   scraper-server/scraper_server.py`) — Knight Novel mode requires it,
   since the session-cookie login can't happen from the browser.

Everything from the previous round (auth_mode branching, NextAuth login,
`/api/admin/novels/{slug}/chapters` payload shape) is unchanged — this
round just fixes the front door so you can actually reach it.

## Still true from before

- Auto-detecting the last scraped chapter only works in legacy mode —
  in Knight Novel mode, set "Skip chapters up to" manually.
- Test with `max_chapters: 1` against a throwaway/real novel first before
  running a full crawl.
- The 24/7-on-Ubuntu piece (gunicorn/waitress, systemd service) is still
  open for later.
