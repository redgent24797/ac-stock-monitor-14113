#!/usr/bin/env python3
"""
Portable Air Conditioner stock monitor  (v4)

Checks UK retailer category pages for portable AC units coming IN STOCK
and sends a notification (via ntfy.sh, optionally to email) when a
product changes from unavailable -> available.

IMPORTANT stock rules (per user requirements):
  - "Back order", "pre-order", "delivers from <date>", "delivery from
    4 weeks", "coming soon", "notify me" etc. are treated as NOT in
    stock, even if the site lets you add the item to the basket.
  - Visible negative wording on a product page overrides the site's own
    structured data, which is sometimes optimistic.
  - Only a genuine in-stock signal counts.

v4 changes:
  - Browser impersonation via curl_cffi (TLS-level Chrome fingerprint),
    which unblocks John Lewis and possibly other protected sites.

v3 changes:
  - Product-page probing now checks visible page text for negative
    wording BEFORE trusting structured data (fixes Appliances Direct
    "Delivery from 4 weeks" being counted as in stock).
  - Added "delivery in ..." style phrases to the negative list.
  - Notification titles sanitised (emoji stripped cleanly) so ntfy no
    longer rejects them.
"""

import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
FIRST_RUN_SUMMARY = os.environ.get("FIRST_RUN_SUMMARY", "1") == "1"
PROBE_LIMIT = 12          # max individual product pages fetched per site
FETCH_RETRIES = 2
FETCH_TIMEOUT = 45

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Anything matching these phrases means the product is NOT really in stock,
# even if an "add to basket" button is present. Checked case-insensitively.
# Negative keywords ALWAYS override positive ones.
NEGATIVE_KEYWORDS = [
    "out of stock", "sold out", "currently unavailable", "unavailable online",
    "back order", "back-order", "backorder", "on backorder",
    "pre-order", "pre order", "preorder",
    "delivers from", "delivery from", "delivered from",
    "dispatched from", "dispatches from", "despatched from",
    "delivery in", "delivered in", "dispatched in", "despatched in",
    "available from", "available to order",
    "coming soon", "notify me", "email me when", "email when available",
    "expected in stock", "due in stock", "awaiting stock",
]

POSITIVE_KEYWORDS = [
    "in stock", "add to basket", "add to cart", "add to trolley", "buy now",
]

# Products whose titles match these are ignored (accessories, non-portable).
EXCLUDE_TITLE_KEYWORDS = [
    "hose", "sleeve", "window kit", "window seal", "duct", "bracket", "cover",
    "wall mounted", "wall-mounted", "split", "filter", "remote control only",
    "air cooler",  # evaporative coolers are not air conditioners
]

SITES = [
    # --- Shopify sites: structured products.json feed (very reliable) ---
    {
        "name": "Air Con Centre",
        "type": "shopify",
        "url": "https://www.airconcentre.co.uk/collections/portable-air-conditioners/products.json?limit=250",
        "product_base": "https://www.airconcentre.co.uk/products/",
    },
    {
        "name": "Meaco",
        "type": "shopify",
        "url": "https://www.meaco.com/collections/air-conditioners/products.json?limit=250",
        "product_base": "https://www.meaco.com/products/",
    },

    # --- HTML sites ---
    {
        "name": "John Lewis",
        "type": "html",
        "url": "https://www.johnlewis.com/browse/electricals/heaters-fans-dehumidifiers/air-conditioners/_/N-7jqe",
        "product_pattern": r"/p\d+",
    },
    {
        "name": "Appliances Direct",
        "type": "html",
        "url": "https://www.appliancesdirect.co.uk/ct/heating-and-air-conditioning/air-conditioners/portable",
        "product_pattern": r"/p/[a-z0-9-]+/[a-z0-9-]+",
        "probe_products": True,   # category page shows no stock text
    },
    {
        "name": "Nisbets",
        "type": "html",
        "url": "https://www.nisbets.co.uk/search?text=portable%20air%20conditioner",
        "product_pattern": r"/[a-z0-9-]+/[a-z]{1,2}\d{3,}",
    },
    {
        "name": "Currys",
        "type": "html",
        "url": "https://www.currys.co.uk/appliances/fans-heating-and-air-treatment/heating-and-cooling/air-conditioners?searchTerm=portable%20air%20condition",
        "product_pattern": r"/products/",
    },
    {
        "name": "Screwfix",
        "type": "html",
        "url": "https://www.screwfix.com/c/heating-plumbing/air-conditioning-units/cat840494",
        "product_pattern": r"/p/[a-z0-9-]+/\w+",
    },
    {
        "name": "Wilko",
        "type": "html",
        "url": "https://www.wilko.com/en-uk/technology-electricals/home-appliances/cooling/air-conditioners/c/257",
        "product_pattern": r"/p/",
    },
    {
        "name": "De'Longhi",
        "type": "html",
        "url": "https://www.delonghi.com/en-gb/c/more-appliances/air-comfort/portable-air-conditioners",
        "product_pattern": r"/p/",
    },
    {
        "name": "AO.com",
        "type": "html",
        "url": "https://ao.com/l/air_conditioners/1/55-143-796-823-825/",
        "product_pattern": r"/product/",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def title_excluded(title):
    t = (title or "").lower()
    return any(k in t for k in EXCLUDE_TITLE_KEYWORDS)


def classify_text(text):
    """Return True (in stock), False (not), or None (can't tell).
    Negative keywords ALWAYS override positive ones (back-order rule)."""
    t = " ".join((text or "").lower().split())
    if any(k in t for k in NEGATIVE_KEYWORDS):
        return False
    if any(k in t for k in POSITIVE_KEYWORDS):
        return True
    return None


def looks_blocked(html):
    """Detect bot-challenge / access-denied pages served with HTTP 200."""
    sample = html[:6000].lower()
    for marker in ("access denied", "captcha", "are you a robot",
                   "unusual traffic", "request blocked", "cf-challenge",
                   "pardon our interruption", "verify you are human"):
        if marker in sample:
            return True
    return False


try:
    # curl_cffi impersonates a real Chrome browser at the TLS level, which
    # gets past bot detection on sites like John Lewis, Currys, AO etc.
    from curl_cffi import requests as cf_requests
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False


def fetch(url):
    last_err = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            if HAVE_CURL_CFFI:
                resp = cf_requests.get(url, headers=HEADERS,
                                       timeout=FETCH_TIMEOUT,
                                       impersonate="chrome")
            else:
                resp = requests.get(url, headers=HEADERS,
                                    timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES:
                time.sleep(3 * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Site checkers -> each returns list of {"title", "url", "in_stock", "price"}
# ---------------------------------------------------------------------------

def check_shopify(site):
    data = fetch(site["url"]).json()
    products = []
    for p in data.get("products", []):
        title = p.get("title", "").strip()
        if title_excluded(title):
            continue
        variants = p.get("variants", [])
        available = any(v.get("available") for v in variants)
        price = variants[0].get("price") if variants else None
        products.append({
            "title": title,
            "url": urljoin(site["product_base"], p.get("handle", "")),
            "in_stock": bool(available),
            "price": f"£{price}" if price else "",
        })
    return products


def _walk_jsonld(node, found):
    """Recursively collect schema.org Product objects from JSON-LD."""
    if isinstance(node, dict):
        types = node.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if "Product" in types:
            found.append(node)
        for v in node.values():
            _walk_jsonld(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, found)


def _availability_from_offer(offers):
    """Map schema.org availability to bool. BackOrder/PreOrder = NOT in stock."""
    if isinstance(offers, list):
        results = [_availability_from_offer(o) for o in offers]
        if any(r is True for r in results):
            return True
        if any(r is False for r in results):
            return False
        return None
    if not isinstance(offers, dict):
        return None
    avail = str(offers.get("availability", ""))
    if not avail:
        return None
    a = avail.lower()
    if "instock" in a or "instoreonly" in a or "onlineonly" in a or "limitedavailability" in a:
        return True
    return False


def _jsonld_products(soup):
    found = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_jsonld(data, found)
    return found


def _extract_product_links(soup, site):
    """Return {url: best-title} for links matching the site's product pattern."""
    pattern = re.compile(site.get("product_pattern", r"/p/"))
    links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue
        url = urljoin(site["url"], href.split("?")[0].split("#")[0])
        title = (a.get_text(" ", strip=True) or a.get("title")
                 or a.get("aria-label") or "")
        # keep the longest (most descriptive) title seen for this URL
        if len(title) > len(links.get(url, "")):
            links[url] = title.strip()
    return links


def _probe_product_page(url):
    """Fetch one product page and classify it. Returns True/False/None."""
    try:
        html = fetch(url).text
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup
    text = " ".join(body.get_text(" ", strip=True).lower().split())
    # visible negative wording overrides EVERYTHING, including the site's
    # structured data ("delivery from 4 weeks" = back order = NOT in stock)
    if any(k in text for k in NEGATIVE_KEYWORDS):
        return False
    for p in _jsonld_products(soup):
        avail = _availability_from_offer(p.get("offers"))
        if avail is not None:
            return avail
    if any(k in text for k in POSITIVE_KEYWORDS):
        return True
    return None


def check_html(site):
    resp = fetch(site["url"])
    if looks_blocked(resp.text):
        raise RuntimeError("site served a bot-challenge/blocked page")
    soup = BeautifulSoup(resp.text, "html.parser")
    products = []
    seen = set()

    # ---- Pass 1: JSON-LD structured data on the category page ----
    for p in _jsonld_products(soup):
        title = (p.get("name") or "").strip()
        url = p.get("url") or ""
        if isinstance(url, dict):
            url = url.get("@id", "")
        url = urljoin(site["url"], url) if url else ""
        if not title or title_excluded(title):
            continue
        avail = _availability_from_offer(p.get("offers"))
        if avail is None:
            continue
        key = url or title
        if key in seen:
            continue
        seen.add(key)
        offers = p.get("offers")
        o = offers[0] if isinstance(offers, list) and offers else offers
        price = f"£{o['price']}" if isinstance(o, dict) and o.get("price") else ""
        products.append({"title": title, "url": url or site["url"],
                         "in_stock": avail, "price": price})
    if products:
        return products

    links = _extract_product_links(soup, site)

    # ---- Pass 2: classify the product card around each link ----
    if not site.get("probe_products"):
        for a in soup.find_all("a", href=True):
            pattern = re.compile(site.get("product_pattern", r"/p/"))
            if not pattern.search(a["href"]):
                continue
            url = urljoin(site["url"], a["href"].split("?")[0].split("#")[0])
            if url in seen:
                continue
            title = links.get(url, "")
            if len(title) < 8:
                continue
            if title_excluded(title):
                seen.add(url)
                continue
            # climb ancestors until the surrounding text yields a verdict
            card, status = a, None
            for _ in range(10):
                if card.parent is None:
                    break
                card = card.parent
                text = card.get_text(" ", strip=True)
                status = classify_text(text)
                if status is not None or len(text) > 600:
                    break
            if status is None:
                continue
            seen.add(url)
            products.append({"title": title[:120], "url": url,
                             "in_stock": status, "price": ""})
        if products:
            return products

    # ---- Pass 3: probe individual product pages ----
    # Used when the category page lists products but shows no stock wording
    # (e.g. Appliances Direct), or when pass 2 found nothing classifiable.
    candidates = [(u, t) for u, t in links.items()
                  if len(t) >= 8 and not title_excluded(t)]
    for url, title in candidates[:PROBE_LIMIT]:
        status = _probe_product_page(url)
        time.sleep(1.5)
        if status is None:
            continue
        products.append({"title": title[:120], "url": url,
                         "in_stock": status, "price": ""})
    return products


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title, message, url=None, priority="high"):
    if not NTFY_TOPIC:
        log(f"[notify skipped - no NTFY_TOPIC] {title}: {message}")
        return
    headers = {
        "Title": title.encode("ascii", "ignore").decode().strip(),
        "Priority": priority,
        "Tags": "snowflake,shopping_cart",
    }
    if url:
        headers["Click"] = url
    if NOTIFY_EMAIL:
        headers["Email"] = NOTIFY_EMAIL
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                      data=message.encode("utf-8"), headers=headers, timeout=20)
        log(f"[notified] {title}")
    except requests.RequestException as e:
        log(f"[notify FAILED] {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = not state

    new_state = dict(state)
    alerts = []
    site_report = []

    for site in SITES:
        name = site["name"]
        try:
            items = check_shopify(site) if site["type"] == "shopify" else check_html(site)
        except Exception as e:
            log(f"[{name}] ERROR: {e}")
            site_report.append(f"⚠️ {name}: could not read ({type(e).__name__})")
            continue

        in_stock_count = sum(1 for i in items if i["in_stock"])
        log(f"[{name}] {len(items)} products parsed, {in_stock_count} in stock")
        site_report.append(f"✓ {name}: {len(items)} products, {in_stock_count} in stock")

        for item in items:
            key = f"{name}|{item['url']}"
            was = state.get(key, {}).get("in_stock", False)
            now = item["in_stock"]
            new_state[key] = {"title": item["title"], "in_stock": now,
                              "last_seen": int(time.time())}
            if now and not was and not first_run:
                alerts.append((name, item))

        time.sleep(2)  # be polite between sites

    # Send alerts (cap to avoid a flood if a whole site restocks at once)
    if len(alerts) > 6:
        by_site = {}
        for name, item in alerts:
            by_site.setdefault(name, []).append(item)
        lines = []
        for name, items in by_site.items():
            lines.append(f"{name}: {len(items)} unit(s) now in stock, e.g. "
                         f"{items[0]['title']} {items[0]['price']}")
        notify("Multiple portable AC units in stock!",
               "\n".join(lines), url=alerts[0][1]["url"])
    else:
        for name, item in alerts:
            notify(f"In stock at {name}",
                   f"{item['title']} {item['price']}".strip(),
                   url=item["url"])

    if first_run and FIRST_RUN_SUMMARY:
        notify("Stock monitor is running",
               "First scan complete. Site status:\n" + "\n".join(site_report),
               priority="default")

    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=1, sort_keys=True)

    log(f"Done. {len(alerts)} in-stock change(s) detected.")


if __name__ == "__main__":
    sys.exit(main())
