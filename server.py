#!/usr/bin/env python3
"""Tiny backend for Snapshot: serves index.html and proxies sources that
can't be called from the browser (Reddit, X, Google Trends). Stdlib only.

Credentials come from the environment, never from the page:
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   (reddit.com/prefs/apps -> "script" or "web app")
  X_BEARER_TOKEN                           (X developer portal; paid API tier)
  GTRENDS_GEO                              (optional; country code for Google Trends, default US; no key needed)
Run: python3 server.py   (PORT env var overrides 8765)
"""
import base64, json, os, re, threading, time
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = "snapshot-prototype/0.1"
CACHE_TTL = 60  # seconds; also keeps us inside upstream rate limits
_cache, _lock = {}, threading.Lock()
_reddit_token = {"value": None, "expires": 0}


class NotConfigured(Exception):
    pass


def http_json(url, headers=None, data=None):
    req = Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(req, timeout=10) as r:
        return json.load(r)


def reddit():
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        raise NotConfigured("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to enable Reddit")
    if time.time() > _reddit_token["expires"] - 30:
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        tok = http_json(
            "https://www.reddit.com/api/v1/access_token",
            headers={"Authorization": f"Basic {basic}"},
            data=urlencode({"grant_type": "client_credentials"}).encode(),
        )
        _reddit_token.update(value=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600))
    d = http_json(
        "https://oauth.reddit.com/r/all/hot?limit=30&raw_json=1",
        headers={"Authorization": f"Bearer {_reddit_token['value']}"},
    )
    return [
        {"title": p["title"], "url": "https://reddit.com" + p["permalink"], "meta": f"r/{p['subreddit']} · {p['ups']}", "value": p["ups"]}
        for p in (c["data"] for c in d["data"]["children"])
    ]


def x_trends():
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise NotConfigured("Set X_BEARER_TOKEN to enable X")
    d = http_json("https://api.x.com/2/trends/by/woeid/1", headers={"Authorization": f"Bearer {token}"})
    return [
        {
            "title": t["trend_name"],
            "url": "https://x.com/search?q=" + t["trend_name"].replace("#", "%23").replace(" ", "%20"),
            "meta": f"{t['tweet_count']} posts" if t.get("tweet_count") else "",
            "value": t.get("tweet_count") or 0,
        }
        for t in d.get("data", [])[:30]
    ]


def google_trends():
    # Google has no public Trends API; this is the "Daily Search Trends" RSS feed that powers trends.google.com/trending.
    geo = re.sub(r"[^A-Za-z-]", "", os.environ.get("GTRENDS_GEO", "US")) or "US"
    req = Request(f"https://trends.google.com/trending/rss?geo={geo}", headers={"User-Agent": "Mozilla/5.0 " + UA})
    with urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    ns = {"ht": "https://trends.google.com/trending/rss"}
    items = []
    for it in root.iterfind(".//item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        raw = (it.findtext("ht:approx_traffic", namespaces=ns) or "").strip()
        m = re.match(r"(\d+)\s*([KM]?)", raw.replace(",", ""), re.I)
        n = int(m.group(1)) * {"": 1, "K": 1000, "M": 1000000}[m.group(2).upper()] if m else 0
        items.append({
            "title": title,
            "url": "https://www.google.com/search?q=" + quote(title),
            "meta": f"{raw} searches" if raw else "",
            "value": n,
        })
    return items[:30]


ROUTES = {"/api/reddit": reddit, "/api/x": x_trends, "/api/gtrends": google_trends}


def cached(path):
    with _lock:
        hit = _cache.get(path)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    items = ROUTES[path]()
    with _lock:
        _cache[path] = (time.time(), items)
    return items


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in ROUTES:
            # static files only: index.html, nothing else (server.py would leak otherwise)
            if path not in ("/", "/index.html"):
                return self.send_error(404)
            self.path = "/index.html"
            return super().do_GET()
        try:
            self.send_json(200, cached(path))
        except NotConfigured as e:
            self.send_json(503, {"error": str(e)})
        except HTTPError as e:
            self.send_json(502, {"error": f"upstream HTTP {e.code}"})
        except (URLError, TimeoutError, KeyError, ValueError) as e:
            self.send_json(502, {"error": f"upstream failed: {type(e).__name__}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"Snapshot on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
