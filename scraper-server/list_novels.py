import requests
API_URL = "http://localhost:3000"
API_KEY = "skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2"
hdrs = {"x-scraper-key": API_KEY}
r = requests.get(API_URL + "/api/scraper/novels", headers=hdrs, timeout=10)
data = r.json()
print("Total novels:", data.get("total"))
for n in data.get("novels", []):
    print("  slug:", repr(n.get("slug")), " | chapters:", n.get("chapterCount", 0), " | title:", repr(n.get("title")))
