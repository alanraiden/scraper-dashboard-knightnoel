# Knight Novel Scraper Dashboard

A local scraper control panel for Knight Novel (`http://localhost:3000`).
Runs at **http://localhost:5174**.

---

## Quick Start

### 1. Set up Knight Novel

Make sure Knight Novel is running at `http://localhost:3000` and your `.env.local` has:

```env
SCRAPER_API_KEY=skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2
```

That key is already set. Knight Novel has these scraper bridge routes built-in:

| Route | Method | What it does |
|---|---|---|
| `/api/scraper/auth` | POST | Verifies the API key |
| `/api/scraper/novels` | GET | Lists all novels |
| `/api/scraper/novels/:slug/chapters` | GET | Chapter list for dedup |
| `/api/scraper/novels/:slug/chapters/bulk` | POST | Bulk chapter upload |

All routes check the `x-scraper-key` header against `SCRAPER_API_KEY`.

### 2. Start the scraper dashboard

```bash
cd scraper-dashboard-knightnovel
npm install
npm run dev
```

Open **http://localhost:5174**.

### 3. Connect

On the Connect screen:
- **Backend URL**: `http://localhost:3000`
- **Scraper API Key**: `skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2`

Click **Connect**. The dashboard calls `/api/scraper/auth` through the Vite proxy (no CORS issues).

### 4. (Optional) Start the Python scraper server

For server-side scraping with full JS support:

```bash
cd scraper-dashboard-knightnovel/scraper-server
pip install flask requests beautifulsoup4 lxml flask-cors
python scraper_server.py
```

Runs on **http://localhost:7832**. The dashboard detects it automatically and uses it for scrape jobs. The Python server uploads chapters directly to Knight Novel using the same `x-scraper-key` header.

---

## Architecture

```
Browser (port 5174)
    │
    ├── /api-local/*  ──► Python scraper (port 7832)
    │                         └── POSTs chapters to KN using x-scraper-key
    │
    └── /api-kn/*     ──► Knight Novel (port 3000)
                              └── /api/scraper/* routes
```

The Vite dev server proxies both paths, so there are **zero CORS issues** from the browser.

---

## Credentials

| Key | Value |
|---|---|
| `SCRAPER_API_KEY` | `skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2` |

Set in Knight Novel's `.env.local`. Entered once in the Connect screen and stored in `localStorage`.
