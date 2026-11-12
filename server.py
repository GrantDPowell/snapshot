#!/usr/bin/env python3
"""Tiny backend for Snapshot: serves index.html and proxies sources that
can't be called from the browser (Reddit, X, Google Trends). Stdlib only.

Credentials come from the environment, never from the page:
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   (reddit.com/prefs/apps -> "script" or "web app")
  X_BEARER_TOKEN                           (X developer portal; paid API tier)
  GTRENDS_GEO                              (optional; country code for Google Trends, default US; no key needed)
Run: python3 server.py   (PORT env var overrides 8765)
"""
import base64, html, json, os, re, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
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
        "https://oauth.reddit.com/r/all/hot?limit=100&raw_json=1",
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


_EN = re.compile(r"\b(the|and|of|to|in|is|for|on|with|as|at|by|says|after|over|from|about|are|was|has|how|why|new|its|his|her)\b", re.I)


def google_trends():
    # Google has no public Trends API; this is the "Daily Search Trends" RSS feed that powers trends.google.com/trending.
    # A trend is a bare search phrase ("microsoft ceo"), so each one also brings its top headlines: they carry the
    # names and context (who, what happened) that tie it to stories in the other feeds.
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
        heads = 0
        for ni in it.findall("ht:news_item", ns):
            head = (ni.findtext("ht:news_item_title", namespaces=ns) or "").strip()
            link = (ni.findtext("ht:news_item_url", namespaces=ns) or "").strip()
            if not head or not link.startswith("http") or not _EN.search(head):
                continue   # skip headlines in other languages: their words would just be noise
            src = (ni.findtext("ht:news_item_source", namespaces=ns) or "").strip()
            items.append({"title": head, "url": link, "meta": f"{src} · trending: {title}" if src else f"trending: {title}", "value": n * 0.6})
            heads += 1
            if heads == 2:
                break
    return items[:60]


# ---- US newsrooms, as RSS/Atom. Each group is one source in the cloud, so a story the news desks, the sports desks
# and the entertainment desks are all covering gets the spread bonus. No keys; feeds that block bots were left out.
FEED_GROUPS = {
    "/api/news": [
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
        ("Washington Post", "https://feeds.washingtonpost.com/rss/national"),
        ("NBC News", "https://feeds.nbcnews.com/nbcnews/public/news"),
        ("CBS News", "https://www.cbsnews.com/latest/rss/main"),
        ("ABC News", "https://abcnews.go.com/abcnews/topstories"),
        ("Fox News", "https://moxie.foxnews.com/google-publisher/latest.xml"),
        ("Guardian US", "https://www.theguardian.com/us-news/rss"),
        ("BBC US", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("NY Post", "https://nypost.com/feed/"),
        ("Newsweek", "https://www.newsweek.com/rss"),
        ("The Atlantic", "https://www.theatlantic.com/feed/all/"),
        ("Vox", "https://www.vox.com/rss/index.xml"),
        ("LA Times", "https://www.latimes.com/local/rss2.0.xml"),
        ("Time", "https://time.com/feed/"),
    ],
    "/api/politics": [
        ("Politico", "https://rss.politico.com/politics-news.xml"),
        ("The Hill", "https://thehill.com/feed/"),
        ("Axios", "https://api.axios.com/feed/"),
        ("NBC Politics", "https://feeds.nbcnews.com/nbcnews/public/politics"),
        ("NPR Politics", "https://feeds.npr.org/1014/rss.xml"),
        ("Fox Politics", "https://moxie.foxnews.com/google-publisher/politics.xml"),
    ],
    "/api/sports": [
        ("CBS Sports", "https://www.cbssports.com/rss/headlines/"),
        ("Yahoo Sports", "https://sports.yahoo.com/rss/"),
        ("SB Nation", "https://www.sbnation.com/rss/index.xml"),
        ("Fox Sports", "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30"),
    ],
    "/api/entertainment": [
        ("Variety", "https://variety.com/feed/"),
        ("Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
        ("Billboard", "https://www.billboard.com/feed/"),
        ("TMZ", "https://www.tmz.com/rss.xml"),
        ("E! News", "https://www.eonline.com/syndication/feeds/rssfeeds/topstories.xml"),
        ("Deadline", "https://deadline.com/feed/"),
        ("IGN", "https://feeds.feedburner.com/ign/all"),
        ("Polygon", "https://www.polygon.com/rss/index.xml"),
    ],
    "/api/business": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Business Insider", "https://feeds.businessinsider.com/custom/all"),
        ("Wired", "https://www.wired.com/feed/rss"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Engadget", "https://www.engadget.com/rss.xml"),
    ],
    "/api/science": [
        ("NYT Science", "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml"),
        ("NYT Health", "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml"),
        ("NPR Health", "https://feeds.npr.org/1128/rss.xml"),
        ("ScienceAlert", "https://www.sciencealert.com/feed"),
    ],
}
PER_FEED = 50
ATOM = "{http://www.w3.org/2005/Atom}"


def read_feed(url, limit=PER_FEED):
    """[(title, link)] from an RSS 2.0 or Atom feed, newest first as published."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 " + UA})
    with urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title, link = html.unescape((it.findtext("title") or "").strip()), (it.findtext("link") or "").strip()
        if title and link.startswith("http"):
            out.append((title, link))
    for e in root.iter(ATOM + "entry"):
        title = html.unescape((e.findtext(ATOM + "title") or "").strip())
        el = e.find(ATOM + "link")
        link = el.get("href", "") if el is not None else ""
        if title and link.startswith("http"):
            out.append((title, link))
    return out[:limit]


def feed_group(feeds):
    """Fetch a group's feeds in parallel and interleave them, so every outlet's top stories rank near the top
    (items carry no counts, so their weight in the cloud comes from rank). Outlets that fail are skipped."""
    with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        results = list(pool.map(lambda f: _try(read_feed, f[1]), feeds))
    per = [[{"title": t, "url": u, "meta": name} for t, u in items] for (name, _), items in zip(feeds, results) if items]
    if not per:
        raise URLError("no feed in this group answered")
    return [x for row in zip_longest(*per) for x in row if x]


def _try(fn, *args):
    try:
        return fn(*args)
    except (HTTPError, URLError, TimeoutError, ET.ParseError, OSError, ValueError):
        return []


GNEWS = "https://news.google.com/rss{}?hl=en-US&gl=US&ceid=US:en"
GNEWS_SECTIONS = ["", *(f"/headlines/section/topic/{t}" for t in ("NATION", "WORLD", "BUSINESS", "TECHNOLOGY", "ENTERTAINMENT", "SPORTS", "SCIENCE", "HEALTH"))]


def google_news():
    # Google News' US top stories plus every section: one headline per story cluster, written "Headline - Outlet"
    with ThreadPoolExecutor(max_workers=len(GNEWS_SECTIONS)) as pool:
        sections = list(pool.map(lambda sec: _try(read_feed, GNEWS.format(sec), 80), GNEWS_SECTIONS))
    if not any(sections):
        raise URLError("Google News didn't answer")
    items, seen = [], set()
    for title, link in (x for row in zip_longest(*sections) for x in row if x):
        if title in seen:   # a story can lead more than one section
            continue
        seen.add(title)
        head, _, outlet = title.rpartition(" - ")
        items.append({"title": head or title, "url": link, "meta": outlet if head else "Google News"})
    return items


def polymarket():
    # the markets people are putting the most money on in the last day: a read on what America is betting about
    d = http_json("https://gamma-api.polymarket.com/events?active=true&closed=false&order=volume24hr&ascending=false&limit=50")   # 50 is the most it serves
    return [
        {"title": e["title"], "url": "https://polymarket.com/event/" + e["slug"], "meta": f"${e.get('volume24hr') or 0:,.0f} traded today", "value": e.get("volume24hr") or 0}
        for e in d if e.get("title") and e.get("slug")
    ]


def bluesky():
    # the public trends API answers from here even when the browser's direct call doesn't
    d = http_json("https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends?limit=25")   # 25 is the most it serves
    return [
        {"title": t["displayName"], "url": t["link"] if t["link"].startswith("http") else "https://bsky.app" + t["link"],
         "meta": f"{t['postCount']:,} posts" if t.get("postCount") else "", "value": t.get("postCount") or 0}
        for t in d.get("trends", []) if t.get("displayName") and t.get("link")
    ]


ROUTES = {"/api/reddit": reddit, "/api/x": x_trends, "/api/gtrends": google_trends, "/api/gnews": google_news,
          "/api/polymarket": polymarket, "/api/bsky": bluesky,
          **{path: (lambda feeds=feeds: feed_group(feeds)) for path, feeds in FEED_GROUPS.items()}}
TTL = {**{path: 300 for path in FEED_GROUPS}, "/api/gnews": 300}   # newsrooms update slowly; be polite

# ---- first-seen times for cloud words, shared by every visitor (so "new" means new to the server, not to one page load)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
SEEN_FORGET = 7 * 86400   # drop a word after a week of not being sent, so it counts as new if it ever returns
_seen, _seen_lock = None, threading.Lock()   # key -> [first_seen_ms, last_seen_ms]


def mark_seen(keys):
    """Record these words and return {key: ms since first seen}. The very first batch on an empty store is the baseline:
    those words count as first seen at time 0 (already old), otherwise a fresh install would paint the whole cloud as new."""
    global _seen
    now = int(time.time() * 1000)
    with _seen_lock:
        if _seen is None:
            try:
                with open(SEEN_FILE) as f:
                    _seen = json.load(f)
            except (OSError, ValueError):
                _seen = {}
        baseline = not _seen
        for k in keys:
            if k in _seen:
                _seen[k][1] = now
            else:
                _seen[k] = [0 if baseline else now, now]
        for k in [k for k, v in _seen.items() if now - v[1] > SEEN_FORGET * 1000]:
            del _seen[k]
        try:
            with open(SEEN_FILE + ".tmp", "w") as f:
                json.dump(_seen, f)
            os.replace(SEEN_FILE + ".tmp", SEEN_FILE)
        except OSError:
            pass   # in-memory tracking still works if the disk doesn't
        return {k: now - _seen[k][0] for k in keys}   # ages, so browser clock skew can't matter


def cached(path):
    with _lock:
        hit = _cache.get(path)
        if hit and time.time() - hit[0] < TTL.get(path, CACHE_TTL):
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

    def do_POST(self):
        if self.path.split("?")[0] != "/api/seen":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            keys = json.loads(self.rfile.read(min(n, 800_000)))["keys"]
            keys = list({k for k in keys if isinstance(k, str) and 0 < len(k) <= 60})[:8000]
        except (ValueError, KeyError, TypeError):
            return self.send_json(400, {"error": "expected {\"keys\": [strings]}"})
        self.send_json(200, mark_seen(keys))

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
