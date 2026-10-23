#!/usr/bin/env python3
"""Scrape the BNA "Vind een architect" directory (bna.nl/vind-een-bna-architect).

How the site works (checked October 2026):
  - bna.nl/architecten redirects to /vind-een-bna-architect/. That page is an
    empty shell; the results are loaded by mijnbna-architecten-filter.js from a
    WordPress AJAX endpoint:
        POST https://bna.nl/wp-admin/admin-ajax.php
             action=mijnbna_filter_architecten, nonce=<from page>, page=N
    It returns JSON ({accounts: [...], pagination: {total, per_page=16,
    last_page}}) taken from BNA's Zoho CRM: id, name, city, disciplines,
    lat/long. The page size is fixed at 16 on the server.
  - The API's pagination is broken: walking all pages of a query returns
    only about two thirds of pagination.total (the last pages are empty, on
    the website too), and which firms go missing differs per query. The
    script therefore unions the unfiltered listing with one pass per filter
    value (discipline, project type, service, expertise, certification)
    and distance-sorted sweeps from several cities, until the number of
    unique firms reaches the reported total. (The skipped records are
    positions 17-24 of every 24: page n starts at offset (n-1)*24 but holds
    16 records.) The summary shows found versus reported firms.
  - Address, website, phone, email and the project types, services and
    expertises are only on the profile page https://bna.nl/architecten/<id>/
    (server-rendered HTML, no JavaScript needed).
  So the script calls the API for the firm list, then fetches each profile.

Politeness:
  - respects robots.txt (urllib.robotparser)
  - 1-2 s random delay between API requests; ~10 s between profile pages,
    because bna.nl allows only ~50-60 profile views per 10 minutes (it then
    shows "Te veel verzoeken"; the script waits the requested 10 minutes)
  - identifiable User-Agent
  - every API page and profile is cached in --cache-dir; a rerun reads the
    cache and only hits the site for what is missing (--refresh refetches)

Outputs (in --out-dir, default: current directory):
  bna_architects_all.csv  - every firm, all fields
  bna_by_city.csv         - city, number of firms (most to fewest)
  bna_amsterdam.csv       - Amsterdam firms only

Usage:
  python scrape_bna.py                 # full run (~2.5 h first time, seconds from cache)
  python scrape_bna.py --limit 20      # quick test on 20 profiles
  python scrape_bna.py --city Amsterdam  # only Amsterdam -> bna_amsterdam.csv
  python scrape_bna.py --refresh       # ignore the cache and refetch everything
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://bna.nl"
LISTING_URL = f"{BASE}/vind-een-bna-architect/"
AJAX_URL = f"{BASE}/wp-admin/admin-ajax.php"
AJAX_ACTION = "mijnbna_filter_architecten"
PROFILE_URL = BASE + "/architecten/{id}/"
USER_AGENT = (
    "BNA-directory-research/1.0 (+mailto:thomas@front-materials.com; "
    "polite scraper, 1-2s delay, cached)"
)
# bna.nl allows roughly 50-60 profile views per 10 minutes, then shows a
# "Te veel verzoeken" page and asks you to come back in 10 minutes.
RATE_LIMIT_MARKER = "Te veel verzoeken"
RATE_LIMIT_WAIT = 10 * 60
PROFILE_DELAY = 10.0
POSTCODE_RE = re.compile(r"\b([1-9][0-9]{3})\s?([A-Za-z]{2})\b")

FIELDS = [
    "name", "profile_url", "bna_id", "street", "postcode", "city", "city_raw",
    "website", "domain", "email", "phone", "disciplines", "project_types",
    "services", "expertises", "employees", "latitude", "longitude",
]

# Profile section headings -> output column.
SECTION_FIELDS = {
    "vakgebied(en)": "disciplines",
    "vakgebieden": "disciplines",
    "projecten": "project_types",
    "diensten": "services",
    "expertises": "expertises",
}
# (key in filter_options on the listing page, filter name in the API request)
FILTER_FACETS = [("disciplines", "subject"), ("projects", "project"), ("services", "service"),
                 ("expertises", "expertise"), ("certifications", "certification")]
SWEEP_ORIGINS = ["Amsterdam", "Maastricht", "Groningen", "Vlissingen", "Enschede", "Den Helder"]
EMPLOYEE_LABELS = ("aantal medewerkers", "medewerkers", "bureaugrootte",
                   "omvang", "fte", "aantal werknemers", "aantal fte")


# --------------------------------------------------------------------------
# HTTP with cache, robots.txt and rate limiting
# --------------------------------------------------------------------------

class PoliteFetcher:
    def __init__(self, cache_dir, min_delay=1.0, max_delay=2.0, refresh=False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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
            txt = self._request("GET", f"{BASE}/robots.txt") or ""
            self._robots.parse(txt.splitlines())
        return self._robots.can_fetch(USER_AGENT, url)

    def fetch(self, cache_name, url, method="GET", data=None, delay=None):
        """Return the response text, from cache when present; None on error.

        delay overrides the minimum pause before this request (seconds)."""
        path = self.cache_dir / cache_name
        if path.exists() and not self.refresh:
            self.cache_hits += 1
            return path.read_text(encoding="utf-8")
        if not self.allowed(url):
            print(f"  robots.txt disallows {url}, skipping", file=sys.stderr)
            return None
        text = self._request(method, url, data() if callable(data) else data, delay)
        while text is not None and RATE_LIMIT_MARKER in text:
            # BNA serves "Te veel verzoeken ... Probeer het over 10 minuten
            # opnieuw" (with HTTP 200) after too many profile views. Never cache it.
            print(f"  rate limited by bna.nl; waiting {RATE_LIMIT_WAIT // 60} minutes as the site asks...",
                  file=sys.stderr)
            time.sleep(RATE_LIMIT_WAIT)
            text = self._request(method, url, data() if callable(data) else data, delay)
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return text

    def _request(self, method, url, data=None, delay=None):
        for attempt in range(3):
            pause = random.uniform(self.min_delay, self.max_delay)
            if delay is not None:
                pause = max(pause, random.uniform(delay, delay * 1.1))
            wait = pause - (time.time() - self._last_request)
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
# Firm list via the AJAX API
# --------------------------------------------------------------------------

class FirmListAPI:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self._nonce = None

    def nonce(self):
        # The nonce rotates, so it is always read from a fresh listing page.
        if self._nonce is None:
            html = self.fetcher._request("GET", LISTING_URL) or ""
            m = re.search(r'mijnbna_architecten_filter_params\s*=\s*\{[^;]*?"nonce"\s*:\s*"(\w+)"', html)
            if not m:
                sys.exit("Could not find the API nonce on the listing page - has the site changed?")
            self._nonce = m.group(1)
        return self._nonce

    def page(self, n, facet=None, value=None):
        slug = f"{facet}-" + re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") if facet else "all"
        cache = f"api/{slug}/page_{n:03d}.json"
        # The server finds nothing for values containing "&" ("Stedenbouw & planning"),
        # but matches on the part before it.
        if facet == "location":  # every firm, sorted by distance from `value`
            params = [("filters[location]", value), ("filters[range]", 5000)]
        elif facet:
            params = [(f"filters[{facet}][]", value.split("&")[0].strip())]
        else:
            params = []
        # The body is built lazily so cached reruns never load the listing page.
        text = self.fetcher.fetch(cache, AJAX_URL, method="POST", data=lambda: [
            ("action", AJAX_ACTION), ("nonce", self.nonce()), ("page", n)] + params)
        try:
            payload = json.loads(text or "")
        except json.JSONDecodeError:
            payload = {}
        if not payload.get("success"):
            (self.fetcher.cache_dir / cache).unlink(missing_ok=True)
            sys.exit(f"API page {n} failed: {str(text)[:200]}")
        accounts = payload["data"].get("accounts") or []
        if isinstance(accounts, dict):  # Zoho sometimes returns {"0": {...}}
            accounts = list(accounts.values())
        return accounts, payload["data"]["pagination"]

    def filter_values(self):
        """[(facet, value)] for every filter option on the listing page."""
        html = self.fetcher.fetch("listing.html", LISTING_URL) or ""
        m = re.search(r"mijnbna_architecten_filter_params\s*=\s*(\{.*?\});", html)
        try:
            opts = json.loads(m.group(1))["filter_options"]
        except (AttributeError, ValueError, KeyError, TypeError):
            print("  warning: could not read the filter options", file=sys.stderr)
            return []
        return [(facet, o["actual_value"]) for option_key, facet in FILTER_FACETS
                for o in opts.get(option_key, [])]

    def listing(self, firms, facet=None, value=None):
        """Walk all pages of one query, adding firms; stop at the first empty page."""
        n, last, returned, total, before = 1, 1, 0, 0, len(firms)
        while n <= last:
            accounts, pag = self.page(n, facet, value)
            last = int(pag.get("last_page") or 1)
            total = int(pag.get("total") or 0)
            if not accounts:
                break
            returned += len(accounts)
            for a in accounts:
                firms.setdefault(str(a["id"]), a)
            n += 1
        print(f"  {value or 'all firms':<30} reported {total:>4}, returned {returned:>4}, "
              f"new {len(firms) - before:>4} -> {len(firms)} unique")
        return total

    def all_firms(self):
        firms = {}
        self.reported_total = self.listing(firms)
        for facet, value in self.filter_values():
            self.listing(firms, facet, value)
        # Distance-sorted sweeps order the firms differently, so each one
        # returns most of the firms that the gaps in earlier queries hid.
        for origin in SWEEP_ORIGINS:
            if len(firms) >= self.reported_total:
                break
            self.listing(firms, "location", origin)
        return firms


# --------------------------------------------------------------------------
# Profile parsing
# --------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip(" ,;|")


def split_list(text):
    return "; ".join(p for p in (clean(x) for x in clean(text).split(",")) if p)


def parse_profile(html):
    soup = BeautifulSoup(html, "lxml")
    page = soup.select_one(".architect-detail-page")
    if page is None:
        return None
    rec = {"extra_labels": []}
    h1 = page.select_one("h1.bureau-title") or page.find("h1")
    rec["name"] = clean(h1.get_text(" ")) if h1 else ""

    for sec in page.select(".content-main .section"):
        h = sec.find(["h5", "h4", "h3"])
        if not h:
            continue
        label = clean(h.get_text(" ")).lower()
        h.extract()
        value = clean(sec.get_text(" "))
        if label in SECTION_FIELDS:
            rec[SECTION_FIELDS[label]] = split_list(value)
        elif any(l in label for l in EMPLOYEE_LABELS):
            rec["employees"] = value
        else:
            rec["extra_labels"].append(label)

    for item in page.select(".contact-item"):
        h = item.find(["h3", "h4", "h5"])
        label = clean(h.get_text(" ")).lower() if h else ""
        if h:
            h.extract()
        if label == "website":
            a = item.find("a", href=True)
            rec["website"] = normalise_url(a["href"] if a else item.get_text(" "))
        elif label.startswith("adres"):
            lines = [clean(p.get_text(" ")) for p in item.find_all("p")] or \
                    [clean(x) for x in item.get_text("\n").split("\n")]
            rec.update(parse_address([l for l in lines if l]))
        elif label.startswith("telefoon") or label.startswith("mobiel"):
            a = item.find("a", href=re.compile(r"^tel:", re.I))
            key = "phone" if label.startswith("telefoon") else "mobile"
            rec[key] = clean(a["href"][4:] if a else item.get_text(" "))
        elif label.startswith("e-mail") or label == "email":
            a = item.find("a", href=re.compile(r"^mailto:", re.I))
            rec["email"] = mailto_address(a["href"]) if a else clean(item.get_text(" "))
        elif any(l in label for l in EMPLOYEE_LABELS):
            rec["employees"] = clean(item.get_text(" "))
        elif label:
            rec["extra_labels"].append(label)

    if not rec.get("phone") and rec.get("mobile"):
        rec["phone"] = rec["mobile"]
    if not rec.get("email"):
        a = page.find("a", href=re.compile(r"^mailto:", re.I))
        if a:
            rec["email"] = mailto_address(a["href"])
    return rec


def mailto_address(href):
    return clean(href[7:].split("?")[0]).lower()


def parse_address(lines):
    """['Kaaisjouwerskade 28', '6541EV, NIJMEGEN'] -> street/postcode/city."""
    out = {"street": "", "postcode": "", "city_raw": ""}
    rest = []
    for line in lines:
        m = POSTCODE_RE.search(line)
        if m and not out["postcode"]:
            out["postcode"] = f"{m.group(1)} {m.group(2).upper()}"
            out["city_raw"] = clean(line[m.end():]).strip(", ")
            before = clean(line[:m.start()])
            if before:
                rest.append(before)
        else:
            rest.append(line)
    if not out["postcode"] and len(rest) >= 2:
        # Foreign or postcode-less address: last line is the city.
        out["city_raw"] = rest.pop()
    out["street"] = ", ".join(rest)
    return out


def normalise_url(url):
    url = clean(url)
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    return url


def domain_of(url):
    if not url:
        return ""
    host = urlparse(normalise_url(url)).netloc.lower().split(":")[0].split("@")[-1]
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

CITY_ALIASES = {
    "a'dam": "Amsterdam", "adam": "Amsterdam", "amsterdam zo": "Amsterdam",
    "amsterdam-zuidoost": "Amsterdam", "amsterdam zuidoost": "Amsterdam",
    "r'dam": "Rotterdam", "rdam": "Rotterdam",
    "'s-gravenhage": "Den Haag", "s-gravenhage": "Den Haag", "'s gravenhage": "Den Haag",
    "the hague": "Den Haag", "den haag": "Den Haag", "s gravenhage": "Den Haag",
    "'s-hertogenbosch": "'s-Hertogenbosch", "s-hertogenbosch": "'s-Hertogenbosch",
    "den bosch": "'s-Hertogenbosch", "'s hertogenbosch": "'s-Hertogenbosch",
    "s hertogenbosch": "'s-Hertogenbosch",
}
# Cities whose districts are often written as "<City>-<District>" / "<City> <District>".
DISTRICT_CITIES = ("Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven",
                   "Groningen", "Haarlem", "Almere", "Tilburg", "Breda")
DISTRICT_WORDS = r"(?:noord|zuid|oost|west|centrum|zuidoost|nieuw-west|nieuw west|noordwest|" \
                 r"zuidwest|noordoost|stad|haven|buiten|poort|zo|z|n|o|w)"
LOWER_WORDS = {"aan", "den", "de", "op", "in", "bij", "der", "en", "van", "het", "a", "d"}


def normalise_city(raw, postcode=""):
    city = clean(raw).strip(".")
    if not city:
        # Amsterdam postcodes are 1000-1109.
        if postcode[:4].isdigit() and 1000 <= int(postcode[:4]) <= 1109:
            return "Amsterdam"
        return ""
    low = city.lower().replace("’", "'")
    low = re.sub(r"\s*\((?:nl|nederland|the netherlands)\)$", "", low)
    low = re.sub(r",?\s*(?:nederland|the netherlands|netherlands|nl)$", "", low).strip()
    if low in CITY_ALIASES:
        return CITY_ALIASES[low]
    for base in DISTRICT_CITIES:
        b = base.lower()
        if low == b or re.fullmatch(rf"{re.escape(b)}[\s\-]+{DISTRICT_WORDS}(?:[\s\-]+\w+)?", low):
            return base
    # Title-case each word but keep Dutch particles lowercase ("Alphen aan den Rijn").
    parts = re.split(r"(\s+|-)", low)
    out = []
    for i, w in enumerate(parts):
        if i > 0 and w in LOWER_WORDS:
            out.append(w)
        elif w.startswith("'s") or w.startswith("'t"):
            out.append(w)
        elif w.startswith("ij"):
            out.append("IJ" + w[2:])
        else:
            out.append(w[:1].upper() + w[1:])
    return "".join(out)


def completeness(rec):
    return sum(bool(rec.get(f)) for f in FIELDS)


def dedupe(records):
    """One row per BNA id, then one per firm name + postcode (same office listed twice)."""
    by_firm = {}
    for r in records:
        name_key = re.sub(r"[^a-z0-9]", "", re.sub(
            r"\b(bna|b\.?v\.?|architecten|architects?|architectuur)\b", "", r["name"].lower()))
        k = (name_key, r["postcode"].replace(" ", "")) if name_key else r["bna_id"]
        if k not in by_firm or completeness(r) > completeness(by_firm[k]):
            by_firm[k] = r
    return sorted(by_firm.values(), key=lambda r: (r["city"], r["name"].lower()))


def write_csv(path, rows, fields):
    # utf-8-sig so Excel opens accents (Coördinatie) correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_record(firm_id, api, prof):
    prof = prof or {}
    rec = {f: "" for f in FIELDS}
    rec.update({k: v for k, v in prof.items() if k in FIELDS and v})
    rec["bna_id"] = firm_id
    rec["profile_url"] = PROFILE_URL.format(id=firm_id)
    rec["name"] = rec["name"] or clean(api.get("Account_Name"))
    rec["city_raw"] = rec["city_raw"] or clean(api.get("Postadres_plaats") or api.get("Shipping_City"))
    if not rec["disciplines"] and api.get("Vakgebied_en"):
        rec["disciplines"] = "; ".join(api["Vakgebied_en"])
    rec["latitude"] = api.get("Breedtegraad") or ""
    rec["longitude"] = api.get("Lengtegraad") or ""
    rec["city"] = normalise_city(rec["city_raw"], rec["postcode"])
    rec["domain"] = domain_of(rec["website"])
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--limit", type=int, default=0, help="only scrape the first N profiles")
    ap.add_argument("--city", help="only fetch firms in this city and write bna_<city>.csv, e.g. --city Amsterdam")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    ap.add_argument("--min-delay", type=float, default=1.0)
    ap.add_argument("--max-delay", type=float, default=2.0)
    ap.add_argument("--profile-delay", type=float, default=PROFILE_DELAY,
                    help="seconds between profile pages; bna.nl rate-limits faster visits (default %(default)s)")
    args = ap.parse_args()

    fetcher = PoliteFetcher(args.cache_dir, args.min_delay, args.max_delay, args.refresh)

    print("Fetching the firm list from the BNA API...")
    api = FirmListAPI(fetcher)
    firms = api.all_firms()
    ids = sorted(firms)
    if args.city:
        # Pre-select on the city the API lists, so only those profiles are fetched.
        ids = [i for i in ids if normalise_city(clean(
            firms[i].get("Postadres_plaats") or firms[i].get("Shipping_City"))) == args.city]
    if args.limit:
        ids = ids[:args.limit]

    print(f"Fetching {len(ids)} profile pages...")
    records, extra_labels, failed = [], Counter(), []
    for i, firm_id in enumerate(ids, 1):
        html = fetcher.fetch(f"profiles/{firm_id}.html", PROFILE_URL.format(id=firm_id),
                             delay=args.profile_delay)
        prof = parse_profile(html) if html else None
        if prof is None:
            failed.append(firm_id)
        else:
            extra_labels.update(prof.pop("extra_labels"))
        records.append(build_record(firm_id, firms[firm_id], prof))
        if i % 50 == 0 or i == len(ids):
            print(f"  {i}/{len(ids)} (live requests: {fetcher.live_requests}, cache hits: {fetcher.cache_hits})")

    rows = dedupe(records)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.city:
        rows = [r for r in rows if r["city"] == args.city]
        path = out / f"bna_{re.sub(r'[^a-z0-9]+', '_', args.city.lower()).strip('_')}.csv"
        write_csv(path, rows, FIELDS)
        print(f"\n{len(rows)} firms in {args.city} -> {path}")
        return
    write_csv(out / "bna_architects_all.csv", rows, FIELDS)

    counts = Counter(r["city"] or "(unknown)" for r in rows)
    by_city = [{"city": c, "firms": n} for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    write_csv(out / "bna_by_city.csv", by_city, ["city", "firms"])

    amsterdam = [r for r in rows if r["city"] == "Amsterdam"]
    write_csv(out / "bna_amsterdam.csv", amsterdam, FIELDS)

    print("\n=== Summary ===")
    print(f"Total firms: {len(rows)}  (duplicates removed: {len(records) - len(rows)}; "
          f"the site reports {api.reported_total} firms, {len(firms)} unique ids were found)")
    print(f"Cities: {len(by_city)}")
    print("Top 15 cities:")
    for row in by_city[:15]:
        print(f"  {row['city']:<25} {row['firms']:>5}")
    print(f"Amsterdam: {len(amsterdam)} firms")
    missing = {f: sum(not r[f] for r in rows) for f in
               ("street", "postcode", "city", "website", "email", "phone",
                "disciplines", "project_types", "employees")}
    print("Missing values per field: " + ", ".join(f"{k}={v}" for k, v in missing.items()))
    if failed:
        print(f"Profiles that could not be fetched/parsed ({len(failed)}): {', '.join(failed[:20])}")
    if extra_labels:
        print("Unmapped profile labels (consider adding): " + ", ".join(f"{k} ({v})" for k, v in extra_labels.most_common()))


if __name__ == "__main__":
    main()
