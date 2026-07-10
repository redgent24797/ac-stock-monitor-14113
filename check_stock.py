#!/usr/bin/env python3
"""
Portable Air Conditioner stock monitor  (v2)

Checks UK retailer category pages for portable AC units coming IN STOCK
and sends a notification (via ntfy.sh, optionally to email) when a
product changes from unavailable -> available.

IMPORTANT stock rules (per user requirements):
  - "Back order", "pre-order", "delivers from <date>", "coming soon",
    "notify me" etc. are treated as NOT in stock, even if the site
    lets you add the item to the basket.
  - Only a genuine in-stock signal counts.

v2 changes:
  - Product-page probing: when a category page lists products without
    stock wording (Appliances Direct), each product page is fetched and
    classified individually (capped at PROBE_LIMIT per site).
  - Deeper/wider product-card detection for sites like Screwfix.
  - Retries with backoff and a longer timeout (helps John Lewis).
  - Detects bot-challenge pages and reports them clearly.
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
        "product_base":
