"""
Quick diagnostic: test the KN scraper API directly from Python.
Run: python test_kn_api.py
"""
import requests, json

API_URL  = "http://localhost:3000"
API_KEY  = "skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2"
SLUG     = "shadow-slave"   # change to any novel slug that exists in your DB

hdrs = {"Content-Type": "application/json", "x-scraper-key": API_KEY}

print("=" * 60)
print("1) GET /api/scraper/auth  (key check)")
r = requests.post(f"{API_URL}/api/scraper/auth",
                  json={"apiKey": API_KEY}, timeout=10)
print(f"   Status : {r.status_code}")
print(f"   Body   : {r.text[:300]!r}")

print()
print("=" * 60)
print(f"2) GET /api/scraper/novels/{SLUG}/chapters")
r = requests.get(f"{API_URL}/api/scraper/novels/{SLUG}/chapters",
                 headers=hdrs, timeout=10)
print(f"   Status : {r.status_code}")
print(f"   Body   : {r.text[:300]!r}")

print()
print("=" * 60)
print(f"3) POST /api/scraper/novels/{SLUG}/chapters  (1 test chapter)")
test_chapter = {
    "chapters": [{"number": 9999, "title": "TEST CHAPTER DELETE ME", "content": "Test content " * 20}],
    "skipDuplicates": False
}
r = requests.post(f"{API_URL}/api/scraper/novels/{SLUG}/chapters",
                  headers=hdrs, json=test_chapter, timeout=30)
print(f"   Status : {r.status_code}")
print(f"   Body   : {r.text[:500]!r}")

print()
print("Done.")
