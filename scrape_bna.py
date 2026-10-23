#!/usr/bin/env python3
"""Scrape the BNA "Vind een architect" directory (bna.nl/architecten).

Outputs (in --out-dir, default: current directory):
  bna_architects_all.csv  - every firm, all fields
  bna_by_city.csv         - city, number of firms (most to fewest)
  bna_amsterdam.csv       - Amsterdam firms only

Politeness:
  - respects robots.txt (urllib.robotparser)
  - 1-2 s random delay between live requests
  - identifiable User-Agent
  - every response is cached on disk (--cache-dir); reruns read from the cache
    and only hit the site for pages not yet cached (use --refresh to refetch)

Discovery order for firm profile URLs (profiles look like
https://bna.nl/architecten/<numeric id>/):
  1. XML sitemaps (from robots.txt, /sitemap_index.xml, /wp-sitemap.xml)
  2. the paginated listing at /architecten/ (follows rel=next / page links)
Run with --discover to print API/JSON endpoints found in the listing page's
HTML and scripts, so the scraper can be switched to an API if one exists.

Usage:
  python scrape_bna.py                 # full run
  python scrape_bna.py --limit 20      # quick test on 20 profiles
  python scrape_bna.py --discover      # inspect listing page for endpoints
"""

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://bna.nl"
LISTING_URL = f"{BASE}/architecten/"
USER_AGENT = (
    "BNA-directory-research/1.0 (+mailto:thomas@front-materials.com; "
    "polite scraper, 1-2s delay, cached)"
)
PROFILE_RE = re.compile(r"^https?://(?:www\.)?bna\.nl/architecten/([0-9]{6,})/?$")
POSTCODE_RE = re.compile(r"\b([1-9][0-9]{3})\s?([A-Z]{2})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+31|0031|0)[\s\-()]*[1-9](?:[\s\-()]*\d){8}")

FIELDS = [
    "name", "profile_url", "street", "postcode", "city", "city_raw",
    "website", "domain", "email", "phone", "specialisations", "employees",
]

# Hosts that are never a firm's own website.
NON_FIRM_HOSTS = (
    "bna.nl", "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "vimeo.com", "pinterest.com", "google.com",
    "goo.gl", "maps.app.goo.gl", "apple.com", "wa.me", "whatsapp.com",
    "tiktok.com", "issuu.com", "architectenweb.nl", "archined.nl",
    "kvk.nl", "cookiebot.com", "wordpress.org", "w.org", "gravatar.com",
)

# Labels (Dutch/English) used on profile pages.
SPEC_LABELS = ("specialisatie", "specialisaties", "vakgebied", "vakgebieden",
               "expertise", "projecttype", "projecttypes", "typologie",
               "typologieën", "werkveld", "werkvelden", "disciplines",
               "specialisations", "specializations", "project types")
EMPLOYEE_LABELS = ("aantal medewerkers", "medewerkers", "bureaugrootte",
                   "omvang", "fte", "employees", "number of employees",
                   "team size", "aantal werknemers")
WEBSITE_LABELS = ("website", "web", "internet", "url")


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
        self.robots = robotparser.RobotFileParser()
        robots_txt = self.get(f"{BASE}/robots.txt", check_robots=False) or ""
        self.robots.parse(robots_txt.splitlines())
        self.robots_txt = robots_txt

    def _cache_path(self, url):
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def allowed(self, url):
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url, check_robots=True):
        """Return response text (cached), or None on error / disallowed."""
        path = self._cache_path(url)
        if path.exists() and not self.refresh:
            self.cache_hits += 1
            return path.read_text(encoding="utf-8")
        if check_robots and not self.allowed(url):
            print(f"  robots.txt disallows {url}, skipping", file=sys.stderr)
            return None
        wait = random.uniform(self.min_delay, self.max_delay) - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30)
                self._last_request = time.time()
                self.live_requests += 1
            except requests.RequestException as exc:
                print(f"  error fetching {url}: {exc}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry = int(resp.headers.get("Retry-After", 10 * (attempt + 1)))
                print(f"  HTTP {resp.status_code} on {url}, waiting {retry}s", file=sys.stderr)
                time.sleep(retry)
                continue
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code} on {url}", file=sys.stderr)
                return None
            resp.encoding = resp.encoding or "utf-8"
            path.write_text(resp.text, encoding="utf-8")
            return resp.text
        return None


# --------------------------------------------------------------------------
# Discovery of profile URLs
# --------------------------------------------------------------------------

def canonical_profile(url):
    url = url.split("#")[0].split("?")[0]
    m = PROFILE_RE.match(url)
    return f"{BASE}/architecten/{m.group(1)}/" if m else None


def urls_from_sitemaps(fetcher):
    candidates = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", fetcher.robots_txt)
    candidates += [f"{BASE}/sitemap_index.xml", f"{BASE}/sitemap.xml", f"{BASE}/wp-sitemap.xml"]
    seen, profiles = set(), set()
    queue = list(dict.fromkeys(candidates))
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        xml = fetcher.get(sm)
        if not xml or "<loc>" not in xml:
            continue
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml):
            loc = loc.replace("&amp;", "&")
            prof = canonical_profile(loc)
            if prof:
                profiles.add(prof)
            elif loc.endswith(".xml") or "sitemap" in loc:
                queue.append(loc)
    return profiles


def urls_from_listing(fetcher, max_pages=500):
    profiles, seen_pages = set(), set()
    page_url = LISTING_URL
    page_no = 1
    while page_url and page_url not in seen_pages and page_no <= max_pages:
        seen_pages.add(page_url)
        html = fetcher.get(page_url)
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        found = {canonical_profile(urljoin(page_url, a["href"])) for a in soup.find_all("a", href=True)}
        found.discard(None)
        new = found - profiles
        profiles |= found
        print(f"  listing page {page_no}: {len(found)} profiles ({len(new)} new)")
        if not new:
            break
        page_url = next_page_url(soup, page_url, page_no)
        page_no += 1
    return profiles


def next_page_url(soup, current, page_no):
    link = soup.find("link", rel="next") or soup.find("a", rel="next")
    if link and link.get("href"):
        return urljoin(current, link["href"])
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        cls = " ".join(a.get("class", [])).lower()
        if text in ("volgende", "next", "›", "»", ">") or "next" in cls:
            return urljoin(current, a["href"])
    # Fall back to the WordPress convention /page/N/ if such links exist.
    target = f"/page/{page_no + 1}/"
    for a in soup.find_all("a", href=True):
        if target in a["href"] or f"page={page_no + 1}" in a["href"]:
            return urljoin(current, a["href"])
    return None


def discover(fetcher):
    """Print hints about how the listing is built (API endpoints, JS apps)."""
    html = fetcher.get(LISTING_URL) or ""
    print(f"listing page: {len(html)} bytes")
    soup = BeautifulSoup(html, "lxml")
    profiles = {canonical_profile(urljoin(LISTING_URL, a["href"])) for a in soup.find_all("a", href=True)}
    profiles.discard(None)
    print(f"profile links in raw HTML: {len(profiles)}")
    if not profiles:
        print("  -> none in the raw HTML: the listing is probably rendered by JavaScript")
    hints = set(re.findall(r"""["'](\S*?(?:wp-json|admin-ajax|/api/|\.json|graphql|algolia|elastic)\S*?)["']""", html))
    for f in soup.find_all(["form"]):
        hints.add(f"form action={f.get('action')} method={f.get('method')} "
                  f"inputs={[i.get('name') for i in f.find_all(['input', 'select'])]}")
    for s in soup.find_all("script", src=True):
        hints.add(f"script {s['src']}")
    for h in sorted(hints):
        print("  ", h)
    print("\nrobots.txt:\n" + fetcher.robots_txt)


# --------------------------------------------------------------------------
# Profile parsing
# --------------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip(" ,;|- ")


def labelled_values(soup):
    """Collect label -> value pairs from dl/table/'Label: value' structures."""
    pairs = []
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append((clean(dt.get_text(" ")), dd))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            pairs.append((clean(cells[0].get_text(" ")), cells[1]))
    for el in soup.find_all(["h2", "h3", "h4", "h5", "strong", "b", "span", "label", "p", "div"]):
        if el.find(["div", "p", "ul"]):
            continue
        label = clean(el.get_text(" ")).rstrip(":").strip()
        if not label or len(label) > 40:
            continue
        sib = el.find_next_sibling()
        if sib is not None:
            pairs.append((label, sib))
    return [(l.lower().rstrip(":"), v) for l, v in pairs]


def value_for(pairs, labels, joiner=", "):
    for label, node in pairs:
        if label in labels:
            items = [clean(li.get_text(" ")) for li in node.find_all("li")]
            items = [i for i in items if i] or [clean(node.get_text(" "))]
            val = joiner.join(dict.fromkeys(items))
            if val:
                return val
    return ""


def jsonld_org(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item:
                    stack.extend(item["@graph"])
                types = item.get("@type", "")
                types = types if isinstance(types, list) else [types]
                if any(t in ("Organization", "LocalBusiness", "ProfessionalService", "Architect") for t in types):
                    return item
    return {}


def parse_address(text):
    """Find '<street> <nr>, 1234 AB City' style addresses in free text."""
    m = POSTCODE_RE.search(text)
    if not m:
        return "", "", ""
    postcode = f"{m.group(1)} {m.group(2)}"
    after = text[m.end():]
    city = re.match(r"\s*,?\s*([A-Z'][\w'\-. ]*?[a-z])(?:\s{2,}|\s*[,\n|]|\s+(?:Nederland|NL|T\b|Tel|E\b|Telefoon|Website|www\.)|$)", after)
    before = text[:m.start()].rstrip(" ,\n")
    street = re.search(r"([A-Z'][^\n,|:]*?\s\d+[\w\- ]{0,6})\s*$", before)
    return (clean(street.group(1)) if street else "", postcode, clean(city.group(1)) if city else "")


def firm_website(soup, pairs):
    for label, node in pairs:
        if label in WEBSITE_LABELS:
            a = node.find("a", href=True) if hasattr(node, "find") else None
            href = a["href"] if a else clean(node.get_text(" "))
            if href and not is_non_firm(href):
                return normalise_url(href)
    main = soup.find("main") or soup
    for a in main.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and not is_non_firm(href):
            return normalise_url(href)
    return ""


def is_non_firm(url):
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    host = host.split(":")[0]
    return not host or any(host == h or host.endswith("." + h) for h in NON_FIRM_HOSTS)


def normalise_url(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url.lstrip("/")
    return url


def domain_of(url):
    if not url:
        return ""
    host = urlparse(normalise_url(url)).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse_profile(html, url):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        if tag.get("type") != "application/ld+json":
            tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()  # site chrome contains BNA's own address/phone/email
    org = jsonld_org(soup)
    pairs = labelled_values(soup)
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n")

    name = clean(org.get("name", ""))
    if not name:
        h1 = main.find("h1") or soup.find("h1")
        name = clean(h1.get_text(" ")) if h1 else ""
    if not name and soup.title:
        name = clean(soup.title.get_text().split("|")[0].split(" - ")[0])

    street = postcode = city = ""
    addr = org.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if isinstance(addr, dict):
        street = clean(addr.get("streetAddress", ""))
        postcode = clean(addr.get("postalCode", ""))
        city = clean(addr.get("addressLocality", ""))
    if not postcode:
        street, postcode, city = parse_address(text)

    email = ""
    mailto = main.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        email = mailto["href"][7:].split("?")[0].strip()
    elif org.get("email"):
        email = org["email"].replace("mailto:", "")
    else:
        m = EMAIL_RE.search(text)
        email = m.group(0) if m else ""
    if email.lower().endswith("@bna.nl"):
        email = ""

    phone = ""
    tel = main.find("a", href=re.compile(r"^tel:", re.I))
    if tel:
        phone = clean(tel["href"][4:])
    elif org.get("telephone"):
        phone = clean(org["telephone"])
    else:
        m = PHONE_RE.search(text)
        phone = clean(m.group(0)) if m else ""

    website = normalise_url(org["url"]) if org.get("url") and not is_non_firm(org["url"]) else firm_website(main, pairs)
    employees = value_for(pairs, EMPLOYEE_LABELS)
    if org.get("numberOfEmployees") and not employees:
        n = org["numberOfEmployees"]
        employees = str(n.get("value", "")) if isinstance(n, dict) else str(n)

    return {
        "name": name,
        "profile_url": url,
        "street": street,
        "postcode": postcode.upper(),
        "city_raw": city,
        "website": website,
        "email": email.lower(),
        "phone": phone,
        "specialisations": value_for(pairs, SPEC_LABELS, joiner="; "),
        "employees": employees,
    }


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

CITY_ALIASES = {
    "a'dam": "Amsterdam", "adam": "Amsterdam", "amsterdam zo": "Amsterdam",
    "r'dam": "Rotterdam", "rdam": "Rotterdam",
    "'s-gravenhage": "Den Haag", "s-gravenhage": "Den Haag", "'s gravenhage": "Den Haag",
    "the hague": "Den Haag", "den haag": "Den Haag",
    "'s-hertogenbosch": "'s-Hertogenbosch", "s-hertogenbosch": "'s-Hertogenbosch",
    "den bosch": "'s-Hertogenbosch", "'s hertogenbosch": "'s-Hertogenbosch",
}
# Cities whose districts are often written as "<City>-<District>" / "<City> <District>".
DISTRICT_CITIES = ("Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven",
                   "Groningen", "Haarlem", "Almere", "Tilburg", "Breda")
DISTRICT_WORDS = r"(?:noord|zuid|oost|west|centrum|zuidoost|nieuw-west|nieuw west|noordwest|" \
                 r"zuidwest|noordoost|zuidwest|stad|haven|buiten|poort|zo|z|n|o|w)"


def normalise_city(raw, postcode=""):
    city = clean(raw).strip(".")
    if not city:
        # Amsterdam postcodes are 1000-1109.
        if postcode[:4].isdigit() and 1000 <= int(postcode[:4]) <= 1109:
            return "Amsterdam"
        return ""
    low = city.lower()
    low = re.sub(r"\s*\((?:nl|nederland|the netherlands)\)$", "", low)
    low = re.sub(r",?\s*(?:nederland|the netherlands|netherlands|nl)$", "", low).strip()
    if low in CITY_ALIASES:
        return CITY_ALIASES[low]
    for base in DISTRICT_CITIES:
        b = base.lower()
        if low == b or re.fullmatch(rf"{re.escape(b)}[\s\-]+{DISTRICT_WORDS}(?:[\s\-]+\w+)?", low):
            return base
    # Title-case each word but keep Dutch particles/prefixes lowercase.
    words = re.split(r"(\s+|-)", low)
    out = []
    for i, w in enumerate(words):
        if w in ("aan", "den", "de", "op", "in", "bij", "der", "en", "van", "het") and i > 0:
            out.append(w)
        elif w.startswith("'s"):
            out.append(w)
        elif w.startswith("ij"):
            out.append("IJ" + w[2:])
        else:
            out.append(w[:1].upper() + w[1:])
    return "".join(out)


def completeness(rec):
    return sum(bool(rec.get(f)) for f in FIELDS)


def dedupe(records):
    by_url = {}
    for r in records:
        k = r["profile_url"]
        if k not in by_url or completeness(r) > completeness(by_url[k]):
            by_url[k] = r
    by_firm = {}
    for r in by_url.values():
        name_key = re.sub(r"[^a-z0-9]", "", re.sub(r"\b(bna|b\.?v\.?|architecten|architects?)\b", "", r["name"].lower()))
        k = (name_key, r["postcode"].replace(" ", "")) if name_key else r["profile_url"]
        if k not in by_firm or completeness(r) > completeness(by_firm[k]):
            by_firm[k] = r
    return sorted(by_firm.values(), key=lambda r: (r["city"], r["name"].lower()))


def write_csv(path, rows, fields):
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
    ap.add_argument("--limit", type=int, default=0, help="only scrape the first N profiles")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    ap.add_argument("--discover", action="store_true", help="inspect the listing page and exit")
    ap.add_argument("--min-delay", type=float, default=1.0)
    ap.add_argument("--max-delay", type=float, default=2.0)
    args = ap.parse_args()

    fetcher = PoliteFetcher(args.cache_dir, args.min_delay, args.max_delay, args.refresh)
    if not fetcher.robots_txt:
        print("Could not fetch robots.txt - is bna.nl reachable?", file=sys.stderr)
    if args.discover:
        discover(fetcher)
        return

    print("Discovering firm profiles via sitemaps...")
    profiles = urls_from_sitemaps(fetcher)
    print(f"  {len(profiles)} profile URLs in sitemaps")
    print("Discovering firm profiles via the listing pages...")
    listing = urls_from_listing(fetcher)
    profiles |= listing
    print(f"  {len(profiles)} unique profile URLs in total")
    if not profiles:
        print("No profiles found. Run with --discover to see how the listing is built.", file=sys.stderr)
        sys.exit(1)

    urls = sorted(profiles)
    if args.limit:
        urls = urls[:args.limit]
    records = []
    for i, url in enumerate(urls, 1):
        html = fetcher.get(url)
        if not html:
            continue
        rec = parse_profile(html, url)
        rec["city"] = normalise_city(rec["city_raw"], rec["postcode"])
        rec["domain"] = domain_of(rec["website"])
        records.append(rec)
        if i % 50 == 0 or i == len(urls):
            print(f"  parsed {i}/{len(urls)} (live requests: {fetcher.live_requests}, cache hits: {fetcher.cache_hits})")

    firms = dedupe(records)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "bna_architects_all.csv", firms, FIELDS)

    counts = Counter(r["city"] or "(unknown)" for r in firms)
    by_city = [{"city": c, "firms": n} for c, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    write_csv(out / "bna_by_city.csv", by_city, ["city", "firms"])

    amsterdam = [r for r in firms if r["city"] == "Amsterdam"]
    write_csv(out / "bna_amsterdam.csv", amsterdam, FIELDS)

    print("\n=== Summary ===")
    print(f"Total firms: {len(firms)}  (duplicates removed: {len(records) - len(firms)})")
    print("Top 15 cities:")
    for row in by_city[:15]:
        print(f"  {row['city']:<25} {row['firms']:>5}")
    print(f"Amsterdam: {len(amsterdam)} firms")
    missing = {f: sum(not r[f] for r in firms) for f in ("street", "postcode", "city", "website", "email", "phone", "specialisations", "employees")}
    print("Missing values per field: " + ", ".join(f"{k}={v}" for k, v in missing.items()))


if __name__ == "__main__":
    main()
