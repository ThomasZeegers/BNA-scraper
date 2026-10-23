#!/usr/bin/env python3
"""Scrape the BNA "Vind een architect" directory (bna.nl/vind-een-bna-architect/).

How the site works (checked October 2026):
  - /architecten/ redirects to /vind-een-bna-architect/. The firm list is not in
    the HTML; mijnbna-architecten-filter.js loads it from a JSON endpoint
    (see QUERIES below for why several queries are merged):
      POST https://bna.nl/wp-admin/admin-ajax.php
        action=mijnbna_filter_architecten, nonce=<from the page>,
        filters[...]=..., page=N
    -> {"success": true, "data": {"accounts": [...], "pagination": {...}}}
  - Profile pages are rate limited (~125, then a "Te veel verzoeken" page that
    says 10 minutes but lasted longer); the script detects that page, does not
    cache it and pauses with growing waits before retrying. A full Amsterdam
    run therefore takes about an hour the first time; reruns are instant.
    16 firms per page (fixed by the server). Each account holds id, name, city,
    disciplines and coordinates, but no address, website, phone or email.
  - Every firm has a server-rendered profile at /architecten/<id>/ with website,
    address, phone, a mailto: link and the disciplines, projects, services and
    expertises. The site does not publish the number of employees.

So the script uses the API to list the firms in the chosen area (default:
25 km around Amsterdam; --all-nl for the whole directory), then fetches each
firm's profile once.

Politeness:
  - checks robots.txt (it currently allows everything)
  - 1-2 s random delay between live requests, identifiable User-Agent
  - every raw response is cached under --cache-dir (api/<query>/page_NNN.json,
    profiles/<id>.html); a rerun reads the cache and makes no requests.
    Use --refresh-list to fetch the firm list again (picks up new members;
    profiles already cached are reused) or --refresh to refetch everything.

Outputs (in --out-dir, default: current directory):
  bna_architects_all.csv  - every firm in the area, all fields
  bna_by_city.csv         - city, number of firms (most to fewest)
  bna_amsterdam.csv       - Amsterdam firms only

Usage:
  python scrape_bna.py                      # Amsterdam + 25 km (~1 h uncached)
  python scrape_bna.py --radius 10          # Amsterdam + 10 km
  python scrape_bna.py --location Haarlem   # another centre
  python scrape_bna.py --all-nl             # whole directory (several hours uncached)
  python scrape_bna.py --limit 20           # quick test on 20 profiles
"""

import argparse
import csv
import json
import math
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import robotparser
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://bna.nl"
LISTING_URL = f"{BASE}/vind-een-bna-architect/"
AJAX_URL = f"{BASE}/wp-admin/admin-ajax.php"
PROFILE_URL = BASE + "/architecten/{id}/"
USER_AGENT = (
    "BNA-directory-research/1.0 (+mailto:thomas@front-materials.com; "
    "polite scraper, 1-2s delay, cached)"
)
# After ~125 profile pages the site answers HTTP 200 with this text instead of
# the profile and asks to come back in 10 minutes; in practice the block lasted
# longer (it looks like an hourly quota), so the pauses grow up to 30 minutes.
RATE_LIMIT_MARKER = "Te veel verzoeken"
RATE_LIMIT_WAITS = (11, 15, 20, 30, 30, 30, 30, 30)  # minutes
NL_POSTCODE_RE = re.compile(r"\b([1-9][0-9]{3})\s?([A-Za-z]{2})\b")

FIELDS = [
    "name", "profile_url", "street", "postcode", "city", "city_raw",
    "website", "domain", "email", "phone",  # domain: website host, else email domain
    "disciplines", "project_types", "services", "expertises",
    "employees", "distance_km", "latitude", "longitude", "bna_id",
]


# --------------------------------------------------------------------------
# HTTP with cache, robots.txt and rate limiting
# --------------------------------------------------------------------------

class PoliteFetcher:
    def __init__(self, cache_dir, min_delay=1.0, max_delay=2.0, refresh=False):
        self.cache_dir = Path(cache_dir)
        self.min_delay, self.max_delay = min_delay, max_delay
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        })
        self._last_request = 0.0
        self.live_requests = 0
        self.cache_hits = 0
        self._robots = None

    def allowed(self, url):
        if self._robots is None:
            self._robots = robotparser.RobotFileParser()
            text = self._request("GET", f"{BASE}/robots.txt") or ""
            self._robots.parse(text.splitlines())
        return self._robots.can_fetch(USER_AGENT, url)

    def fetch(self, cache_name, url, data=None, refresh=None):
        """GET (or POST when data is given) url, cached under cache_name.

        Returns the response text, or None on error / disallowed by robots.txt.
        """
        path = self.cache_dir / cache_name
        refresh = self.refresh if refresh is None else refresh
        if path.exists() and not refresh:
            self.cache_hits += 1
            return path.read_text(encoding="utf-8")
        if not self.allowed(url):
            print(f"  robots.txt disallows {url}, skipping", file=sys.stderr)
            return None
        text = self._request("POST" if data else "GET", url, data)
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return text

    def _request(self, method, url, data=None):
        for wait_min in RATE_LIMIT_WAITS + (None,):
            text = self._request_once(method, url, data)
            if text is None or RATE_LIMIT_MARKER not in text:
                return text
            if wait_min is None:
                sys.exit("Still rate limited by bna.nl after several hours; rerun later "
                         "(everything fetched so far is cached).")
            print(f"  rate limited by bna.nl at {time.strftime('%H:%M')}, pausing {wait_min} minutes",
                  file=sys.stderr, flush=True)
            time.sleep(wait_min * 60)

    def _request_once(self, method, url, data=None):
        for attempt in range(4):
            wait = random.uniform(self.min_delay, self.max_delay) - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.request(method, url, data=data, timeout=30)
            except requests.RequestException as exc:
                print(f"  error fetching {url}: {exc}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                continue
            finally:
                self._last_request = time.time()
                self.live_requests += 1
            if resp.status_code == 429 or resp.status_code >= 500:
                retry = resp.headers.get("Retry-After", "")
                retry = int(retry) if retry.isdigit() else 10 * (attempt + 1)
                print(f"  HTTP {resp.status_code} on {url}, waiting {retry}s", file=sys.stderr)
                time.sleep(retry)
                continue
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code} on {url}", file=sys.stderr)
                return None
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        return None


# --------------------------------------------------------------------------
# Firm list via the JSON API
# --------------------------------------------------------------------------

def get_nonce(fetcher):
    html = fetcher.fetch("listing.html", LISTING_URL, refresh=True) or ""
    m = re.search(r'mijnbna_architecten_filter_params\s*=\s*\{.*?"nonce"\s*:\s*"([0-9a-f]+)"', html)
    if not m:
        sys.exit("Could not find the filter nonce on the listing page; the site may have changed.")
    return m.group(1)


# The server's pagination is lossy: the unfiltered list and facet filters stop
# returning rows after roughly two thirds of the reported total and repeat rows
# on the first pages. A location + radius query is filtered on the server by
# distance and comes back complete (checked: Amsterdam 10 km returned 142 of
# 142, Amersfoort 200 km 835 of 835), so that is the main source. It misses
# firms without coordinates, so the unfiltered list is merged in as a fallback;
# from it only firms without coordinates are kept, and in region mode only
# when their city is one the radius query also found.
NATIONAL_QUERIES = {
    "discipline_architectuur": {"filters[subject][]": "Architectuur"},
    "discipline_interieur": {"filters[subject][]": "Interieurarchitectuur"},
    "discipline_landschap": {"filters[subject][]": "Landschapsarchitectuur"},
    "discipline_stedenbouw": {"filters[subject][]": "Stedenbouw & planning"},
}


def has_coords(acc):
    try:
        return -90 <= float(acc.get("Breedtegraad")) <= 90 and -180 <= float(acc.get("Lengtegraad")) <= 180
    except (TypeError, ValueError):
        return False


def build_queries(location, radius, national):
    if national:
        return {"nl_200km": {"filters[location]": "Amersfoort", "filters[range]": "200"},
                "all": {}, **NATIONAL_QUERIES}
    slug = re.sub(r"[^a-z0-9]+", "_", location.lower()).strip("_")
    return {f"{slug}_{radius}km": {"filters[location]": location, "filters[range]": str(radius)},
            "all": {}}


def api_page(fetcher, query, filters, page, nonce_holder, refresh):
    """Return the parsed JSON for one API page (cached as api/<query>/page_NNN.json)."""
    cache = f"api/{query}/page_{page:03d}.json"
    cached = fetcher.cache_dir / cache
    if not cached.exists() or refresh:
        if nonce_holder[0] is None:
            nonce_holder[0] = get_nonce(fetcher)
        data = {
            "action": "mijnbna_filter_architecten",
            "nonce": nonce_holder[0],
            "filters[range]": "5000",  # "Alle afstanden"
            **filters,
            "page": str(page),
        }
        text = fetcher.fetch(cache, AJAX_URL, data=data, refresh=True)
    else:
        text = fetcher.fetch(cache, AJAX_URL)
    try:
        resp = json.loads(text or "")
    except json.JSONDecodeError:
        resp = {}
    if not resp.get("success"):
        cached.unlink(missing_ok=True)
        sys.exit(f"API query {query} page {page} failed: {str(text)[:200]}")
    data = resp["data"]
    acc = data.get("accounts") or []
    data["accounts"] = list(acc.values()) if isinstance(acc, dict) else acc
    return data


def list_firms(fetcher, queries, national, refresh=False):
    """Return {id: account} merged over queries, plus the API's own total.

    The first query is the radius query; rows from the other queries are only
    added for firms without coordinates (and, in region mode, in a city the
    radius query already covers).
    """
    nonce = [None]
    firms, api_total, region_cities = {}, 0, set()
    for n, (query, filters) in enumerate(queries.items()):
        page, last_page, rows = 1, 1, 0
        while page <= last_page:
            data = api_page(fetcher, query, filters, page, nonce, refresh)
            last_page = int(data["pagination"]["last_page"])
            if not data["accounts"]:
                break
            for acc in data["accounts"]:
                city = normalise_city(acc.get("Shipping_City") or acc.get("Postadres_plaats"))
                if n == 0:
                    region_cities.add(city)
                elif has_coords(acc) or not (national or city in region_cities):
                    continue
                firms.setdefault(str(acc["id"]), acc)
            rows += len(data["accounts"])
            page += 1
        if query == "all":
            api_total = int(data["pagination"]["total"])
        print(f"  {query}: {rows} rows (reported {data['pagination']['total']}), {len(firms)} unique firms so far")
    return firms, api_total


# --------------------------------------------------------------------------
# Profile parsing
# --------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip(" ,;|")


def split_list(text):
    return "; ".join(p for p in (clean(x) for x in (text or "").split(",")) if p)


def section(page, css_class):
    node = page.select_one(f".section-{css_class} div")
    return split_list(node.get_text(" ")) if node else ""


def parse_address(lines):
    """Split the address <p> lines into street, postcode and city."""
    street, postcode, city = [], "", ""
    for line in lines:
        m = NL_POSTCODE_RE.search(line)
        foreign = re.match(r"^\s*([A-Z]{0,2}-?\d{4,5})\s*,?\s+(.+)$", line)
        if m and not postcode:
            postcode = f"{m.group(1)} {m.group(2).upper()}"
            city = clean(line[m.end():])
        elif foreign and not postcode:
            postcode, city = foreign.group(1), clean(foreign.group(2))
        elif not postcode:
            street.append(line)
        elif not city:
            city = line
    return clean(", ".join(street)), postcode, city


def parse_profile(html):
    soup = BeautifulSoup(html, "lxml")
    page = soup.select_one(".architect-detail-page")
    if page is None:
        return None
    rec = {}
    h1 = page.select_one(".bureau-title")
    rec["name"] = clean(h1.get_text(" ")) if h1 else ""
    rec["disciplines"] = section(page, "disciplines")
    rec["project_types"] = section(page, "projects")
    rec["services"] = section(page, "services")
    rec["expertises"] = section(page, "expertises")

    site = page.select_one(".contact-item.website a[href]")
    rec["website"] = normalise_url(site["href"]) if site else ""
    addr_lines = [clean(p.get_text(" ")) for p in page.select(".contact-item.address p")]
    rec["street"], rec["postcode"], rec["city_raw"] = parse_address([l for l in addr_lines if l])
    tel = page.select_one(".contact-item.phone a[href^='tel:']")
    rec["phone"] = clean(unquote(tel["href"][4:])) if tel else ""
    mail = page.select_one("a[href^='mailto:']")
    rec["email"] = unquote(mail["href"][7:].split("?")[0]).strip().lower() if mail else ""
    if rec["email"].endswith("@bna.nl"):
        rec["email"] = ""
    rec["employees"] = ""  # not published on bna.nl; column kept for CRM import
    return rec


def normalise_url(url):
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "http://" + url.lstrip("/")
    return url


# Mail domains that say nothing about the firm, so they are never used as domain.
FREEMAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.nl", "outlook.com", "live.nl",
    "live.com", "icloud.com", "me.com", "yahoo.com", "xs4all.nl", "kpnmail.nl", "kpnplanet.nl",
    "planet.nl", "ziggo.nl", "home.nl", "casema.nl", "chello.nl", "hetnet.nl", "zeelandnet.nl",
    "tele2.nl", "upcmail.nl", "online.nl", "quicknet.nl", "solcon.nl",
}


def domain_of(url):
    if not url:
        return ""
    host = urlparse(normalise_url(url)).netloc.lower().split(":")[0].split("@")[-1]
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

CITY_ALIASES = {
    "a'dam": "Amsterdam", "adam": "Amsterdam",
    "r'dam": "Rotterdam", "rdam": "Rotterdam",
    "'s-gravenhage": "Den Haag", "s-gravenhage": "Den Haag", "'s gravenhage": "Den Haag",
    "the hague": "Den Haag", "den haag": "Den Haag",
    "'s-hertogenbosch": "'s-Hertogenbosch", "s-hertogenbosch": "'s-Hertogenbosch",
    "den bosch": "'s-Hertogenbosch", "'s hertogenbosch": "'s-Hertogenbosch",
}
# Cities whose districts are often written as "<City>-<District>" / "<City> <District>".
DISTRICT_CITIES = ("Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven",
                   "Groningen", "Haarlem", "Almere", "Tilburg", "Breda", "Nijmegen")
# "Amsterdam-Duivendrecht" is the postal name of Duivendrecht (municipality
# Ouder-Amstel), but it is an Amsterdam address for all practical purposes.
DISTRICT_WORDS = (r"(?:duivendrecht|noord|zuid|oost|west|centrum|zuidoost|zuid-oost|nieuw-west|nieuw west|"
                  r"noordwest|zuidwest|noordoost|stad|haven|buiten|poort|zo|z|n|o|w|nw|zw|no)")
# Province codes appended to the postal city ("NIJKERK GLD"); "(NH)" style is kept
# because it tells apart towns such as Bergen (NH) and Bergen (L).
PROVINCE_SUFFIX = r"\s+(?:gld|gl|gn|gr|zh|nh|nb|ut|utr|ov|fr|frl|dr|fl|fld|lb|li|lim|zl|ze|zld)$"
LOWERCASE_WORDS = ("aan", "den", "de", "op", "in", "bij", "der", "en", "van", "het", "ter", "ten", "te")


def normalise_city(raw):
    low = clean(raw).strip(".").lower()
    low = re.sub(PROVINCE_SUFFIX, "", low)
    low = re.sub(r"\s*\((?:nl|nederland|the netherlands)\)$", "", low)
    low = re.sub(r",?\s*(?:nederland|the netherlands|netherlands|nl)$", "", low).strip()
    if not low:
        return ""
    if low in CITY_ALIASES:
        return CITY_ALIASES[low]
    for base in DISTRICT_CITIES:
        b = base.lower()
        if low == b or re.fullmatch(rf"{re.escape(b)}[\s\-]+{DISTRICT_WORDS}", low):
            return base
    # Title-case each word but keep Dutch particles lowercase ("Alphen aan den Rijn").
    out = []
    low = re.sub(r"\ba/d\b", "aan den", low)
    for i, w in enumerate(re.split(r"(\s+|-)", low)):
        if (w in LOWERCASE_WORDS and i > 0) or w.startswith("'s") or w.startswith("'t"):
            out.append(w)
        elif re.fullmatch(r"\([a-z]{1,3}\)", w):
            out.append(w.upper())  # "Bergen (NH)"
        elif w.startswith("ij"):
            out.append("IJ" + w[2:])
        else:
            out.append(w[:1].upper() + w[1:])
    return "".join(out)


def completeness(rec):
    return sum(bool(rec.get(f)) for f in FIELDS)


def dedupe(records):
    by_id = {}
    for r in records:
        k = r["bna_id"]
        if k not in by_id or completeness(r) > completeness(by_id[k]):
            by_id[k] = r
    by_firm = {}
    for r in by_id.values():
        name_key = re.sub(r"[^a-z0-9]", "", re.sub(r"\b(bna|b\.?v\.?|architecten|architects?)\b", "", r["name"].lower()))
        k = (name_key, r["postcode"].replace(" ", "")) if name_key else r["bna_id"]
        if k not in by_firm or completeness(r) > completeness(by_firm[k]):
            by_firm[k] = r
    return sorted(by_firm.values(), key=lambda r: (r["city"].lower(), r["name"].lower()))


def add_distances(firms, centre_city):
    """distance_km from the centre city (median position of its firms)."""
    pts = [(float(r["latitude"]), float(r["longitude"])) for r in firms
           if r["city"] == centre_city and has_coords({"Breedtegraad": r["latitude"], "Lengtegraad": r["longitude"]})]
    if not pts:
        return
    lat0 = sorted(p[0] for p in pts)[len(pts) // 2]
    lon0 = sorted(p[1] for p in pts)[len(pts) // 2]
    for r in firms:
        if has_coords({"Breedtegraad": r["latitude"], "Lengtegraad": r["longitude"]}):
            lat, lon = float(r["latitude"]), float(r["longitude"])
            dlat, dlon = math.radians(lat - lat0), math.radians(lon - lon0)
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat0)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2
            r["distance_km"] = round(6371 * 2 * math.asin(math.sqrt(a)), 1)


def write_csv(path, rows, fields):
    # utf-8-sig so Excel shows accented characters correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--location", default="Amsterdam", help="centre of the search area (default: Amsterdam)")
    ap.add_argument("--radius", type=int, default=25, choices=(5, 10, 25, 50, 100, 200),
                    help="radius in km around --location (the values the site offers; default: 25)")
    ap.add_argument("--all-nl", action="store_true", help="scrape the whole directory instead of one region")
    ap.add_argument("--limit", type=int, default=0, help="only scrape the first N profiles")
    ap.add_argument("--refresh-list", action="store_true", help="refetch the firm list from the API")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and refetch everything")
    ap.add_argument("--min-delay", type=float, default=1.0)
    ap.add_argument("--max-delay", type=float, default=2.0)
    args = ap.parse_args()

    fetcher = PoliteFetcher(args.cache_dir, args.min_delay, args.max_delay, args.refresh)

    area = "all of the Netherlands" if args.all_nl else f"{args.radius} km around {args.location}"
    print(f"Fetching the firm list from the BNA API ({area})...")
    queries = build_queries(args.location, args.radius, args.all_nl)
    firms_by_id, total = list_firms(fetcher, queries, args.all_nl, refresh=args.refresh or args.refresh_list)
    accounts = sorted(firms_by_id.values(), key=lambda a: str(a["id"]))
    print(f"  {len(accounts)} unique firms in {area} (whole directory per the API: {total})")
    if args.limit:
        accounts = accounts[:args.limit]

    print("Fetching firm profiles...")
    records = []
    for i, acc in enumerate(accounts, 1):
        firm_id = str(acc["id"])
        url = PROFILE_URL.format(id=firm_id)
        html = fetcher.fetch(f"profiles/{firm_id}.html", url)
        rec = parse_profile(html) if html else None
        if rec is None:
            print(f"  no profile for {acc.get('Account_Name')} ({url}); using API data only", file=sys.stderr)
            rec = {f: "" for f in FIELDS}
        rec["bna_id"] = firm_id
        rec["profile_url"] = url
        rec["name"] = rec["name"] or clean(acc.get("Account_Name"))
        rec["city_raw"] = rec["city_raw"] or clean(acc.get("Shipping_City") or acc.get("Postadres_plaats"))
        if not rec["disciplines"] and acc.get("Vakgebied_en"):
            rec["disciplines"] = "; ".join(acc["Vakgebied_en"])
        rec["latitude"] = acc.get("Breedtegraad") or ""
        rec["longitude"] = acc.get("Lengtegraad") or ""
        rec["city"] = normalise_city(rec["city_raw"])
        rec["domain"] = domain_of(rec["website"])
        mail_domain = rec["email"].rpartition("@")[2]
        if not rec["domain"] and mail_domain and mail_domain not in FREEMAIL:
            rec["domain"] = mail_domain
        records.append(rec)
        if i % 50 == 0 or i == len(accounts):
            print(f"  {i}/{len(accounts)} (live requests: {fetcher.live_requests}, cache hits: {fetcher.cache_hits})")

    firms = dedupe(records)
    add_distances(firms, normalise_city(args.location))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "bna_architects_all.csv", firms, FIELDS)

    counts = Counter(r["city"] or "(unknown)" for r in firms)
    by_city = [{"city": c, "firms": n} for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    write_csv(out / "bna_by_city.csv", by_city, ["city", "firms"])

    amsterdam = [r for r in firms if r["city"] == "Amsterdam"]
    write_csv(out / "bna_amsterdam.csv", amsterdam, FIELDS)

    print("\n=== Summary ===")
    print(f"Total firms: {len(firms)}  (API reports {total}, duplicates removed: {len(records) - len(firms)})")
    print("Top 15 cities:")
    for row in by_city[:15]:
        print(f"  {row['city']:<25} {row['firms']:>5}")
    print(f"Amsterdam: {len(amsterdam)} firms")
    check = ("street", "postcode", "city", "website", "email", "phone", "disciplines", "project_types")
    print("Missing values per field: " + ", ".join(f"{f}={sum(not r[f] for r in firms)}" for f in check))
    print(f"Requests this run: {fetcher.live_requests} live, {fetcher.cache_hits} from cache")


if __name__ == "__main__":
    main()
