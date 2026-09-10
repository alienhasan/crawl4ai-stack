#!/usr/bin/env python3
"""
Google Maps business listing scraper for the Crawl4AI lead-gen project.

Uses the Crawl4AI AsyncWebCrawler browser stack (Playwright under the hood) to
search Google Maps for a business type + location and return a clean list of
businesses with their full listing details.

- No LLM, no AI, no summarization. Only deterministic extraction.
- Returns each result as {"name", "rating", "review_count", "address",
  "phone", "website"}.
- Uses a dedicated BrowserConfig so the existing /clean endpoint config in
  crawl_api.py is left completely unchanged.

How it works (two phases):
  Phase A - feed collection: scroll the Google Maps results feed
    (div[role="feed"]) and collect the place URL + name of every business
    (deduped by name) until max_results are gathered or the feed stops
    loading. The feed rows themselves only expose name + star rating; they do
    NOT contain phone, full address, review count or a website link in the
    current Google Maps UI.
  Phase B - detail extraction: navigate to each business's own place URL and
    read the place detail panel (h1.DUwDvf etc.). That panel carries the
    full address, phone number, rating + review count, and the canonical
    website row (a[data-item-id="authority"]). Costs roughly 5-8 s per place.

Pitfalls handled:
  - Headless Chromium flags (no-sandbox, dev-shm) to avoid crashes on WSL.
  - Google Maps consent banner auto-dismissed.
  - Results feed lazy-loads on scroll -> we scroll to load more, dedupe by name.
  - Google masks phone digits in HTML attributes (href="tel:+4420****5272",
    data-item-id="phone:tel:+4420****5272") when it suspects a bot, but the
    rendered text and aria-label carry the full number -> phone is always read
    from visible text / aria-label, never from attributes.
  - Sponsored/ad rows are skipped in Phase A.
  - Website tracking params (utm_*, fbclid, gclid, ...) are stripped.
"""

import logging
import os
from typing import Dict, List, Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

logger = logging.getLogger(__name__)

# WSLg provides an X server at :0 even though DISPLAY may not be exported.
# Harmless in headless mode, needed if a caller opts into a headed browser.
if os.environ.get("DISPLAY") is None and os.path.exists("/tmp/.X11-unix/X0"):
    os.environ["DISPLAY"] = ":0"

# --- Browser config dedicated to Google Maps scraping ----------------------
# Kept separate from crawl_api.py's browser_config so existing behaviour is
# completely unchanged. Stealth + a realistic Linux Chrome UA + generous
# viewport reduce bot flags and let results cards render fully.
def _maps_browser_config() -> BrowserConfig:
    return BrowserConfig(
        browser_type="chromium",
        headless=True,
        viewport_width=1400,
        viewport_height=900,
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        extra_args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
        # Crawl4AI's built-in stealth (playwright-stealth) to avoid bot flags.
        enable_stealth=True,
        verbose=False,
    )


# --- Phase A: feed collection (runs in-page) -------------------------------
# Each business card is a div[role="article"] inside the results feed. The card
# link (a.hfpxzc, href contains /maps/place/) carries the business name in its
# aria-label and points at the business's own Google Maps place URL. Feed rows
# only show name + star rating -> collect {name, href, rating} and leave the
# richer fields (phone, full address, review count, website) to Phase B.
FEED_JS = r"""
() => {
  const feed = document.querySelector('div[role="feed"]');
  if(!feed) return [];
  const rows = Array.from(feed.querySelectorAll('div[role="article"]'))
                    .filter(r => r.querySelector('a[href*="/maps/place/"]'));
  const out = [];
  for(const r of rows){
    const text = r.innerText || '';
    if(/Sponsored/.test(text)) continue;           // skip ad rows entirely
    const pa = r.querySelector('a[href*="/maps/place/"]');
    const name = (pa && pa.getAttribute('aria-label')) || '';
    const href = (pa && pa.getAttribute('href')) || '';
    if(!name || !href) continue;
    // Feed rows show the star rating as a bare number line (e.g. "4.8").
    let rating = '';
    for(const ln of text.split('\n')){
      const s = ln.trim();
      if(/^\d(\.\d)?$/.test(s)){ rating = s; break; }
    }
    out.push({name: name, href: href, rating: rating});
  }
  return out;
}
"""

# Scroll the results feed to load more. The feed element (div[role="feed"]) is
# itself the scrollable results panel, so check it first, then its ancestors.
# Returns True when the scroll position advanced (there was somewhere to scroll).
SCROLL_JS = r"""
() => {
  const feed = document.querySelector('div[role="feed"]');
  if(!feed) return false;
  const candidates = [];
  let el = feed;
  candidates.push(feed);
  for(let i=0;i<10;i++){
    el = el && el.parentElement;
    if(!el) break;
    candidates.push(el);
  }
  let target = null;
  for(const c of candidates){
    if(c && c.scrollHeight > c.clientHeight + 40){ target = c; break; }
  }
  if(!target) return false;
  const before = target.scrollTop;
  target.scrollTop = target.scrollHeight;
  return target.scrollTop > before;
}
"""

# Dismiss the Google consent/cookie banner if present (both EU and newer US flows).
CONSENT_SELECTORS = [
    "button[aria-label*='Accept all']",
    "button[aria-label*='Accept']",
    "#L2AGLb",
    "form[action*='consent'] button",
]


# --- Phase B: place detail extraction (runs in-page) -----------------------
# Runs on an individual place page (https://www.google.com/maps/place/...).
# DOM map (verified live, hl=en):
#   name        -> h1.DUwDvf (h2.DUwDvf on overlay views)
#   rating      -> div.F7nice, text like "4.8 (895)" (stars span aria-hidden)
#   review count-> span[role="img"][aria-label$="reviews"] e.g. "1,423 reviews"
#   address     -> button[data-item-id="address"] (aria-label "Address: ...")
#   phone       -> [data-item-id^="phone:"] visible text " +44 20 3757 5272" —
#                  read from text/aria ONLY: the attribute is digit-masked
#                  (tel:+4420****5272) while rendered text is complete.
#   website     -> a[data-item-id="authority"] (the "Website" row Google stores)
#                  fallback a[aria-label^="Website:"], then a[data-item-id^="action:"]
PLACE_JS = r"""
() => {
  const q = (s) => document.querySelector(s);
  const clean = (s) => (s || '').replace(/[\uE000-\uF8FF\u2190-\u21FF\uFE0F\uFEFF\u200D\u200B\u00A0]/g, '')
                                .replace(/\s+/g, ' ').trim();
  const out = {name: '', rating: '', review_count: '', address: '', phone: '', website: ''};

  const nameEl = q('h1.DUwDvf') || q('h2.DUwDvf');
  if(nameEl) out.name = clean(nameEl.innerText);

  const f7 = q('div.F7nice');
  if(f7){
    const txt = clean(f7.innerText);            // e.g. "4.8 (895)" or "4.8 (1,423)"
    const rm = txt.match(/(\d+(?:\.\d+)?)/);
    if(rm) out.rating = rm[1];
    const cntEl = f7.querySelector('[role="img"][aria-label*="review" i]')
               || f7.querySelector('[aria-label*="review" i]');
    if(cntEl){
      const cm = (cntEl.getAttribute('aria-label') || '').match(/([\d,]+)/);
      if(cm) out.review_count = cm[1].replace(/,/g, '');
    }
    if(!out.review_count){
      const pm = txt.match(/\(([\d,]+)\)/);
      if(pm) out.review_count = pm[1].replace(/,/g, '');
    }
  }

  const ab = q('button[data-item-id="address"]');
  if(ab){
    let a = ab.getAttribute('aria-label') || '';
    a = a.replace(/^Address:\s*/i, '');
    out.address = clean(a) || clean(ab.innerText);
  }

  const pb = q('[data-item-id^="phone:"]');
  if(pb){
    let p = pb.getAttribute('aria-label') || '';
    p = p.replace(/^Phone:\s*/i, '');
    out.phone = clean(p) || clean(pb.innerText);
  }

  const wa = q('a[data-item-id="authority"]');
  if(wa && /^https?:/i.test(wa.href || '')) out.website = wa.href;
  if(!out.website){
    const wl = Array.from(document.querySelectorAll('a[aria-label^="Website:"]'))
                    .find(a => /^https?:/i.test(a.href || ''));
    if(wl) out.website = wl.href;
  }
  if(!out.website){
    const ac = q('a[data-item-id^="action:"]');
    if(ac && /^https?:/i.test(ac.href || '')) out.website = ac.href;
  }

  return out;
}
"""


def build_maps_url(query: str, location: Optional[str] = None) -> str:
    """Build a Google Maps search URL for a business type (+ optional location)."""
    parts = []
    if query:
        parts.append(query.strip())
    if location:
        parts.append(location.strip())
    q = "+".join(part.replace(" ", "+") for part in parts if part)
    # hl=en forces English UI regardless of IP/geo (otherwise Bengali/localised).
    return f"https://www.google.com/maps/search/{q}?hl=en"


# Tracking parameters that add noise to a business website URL.
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid",
                    "yclid", "twclid", "srsltid", "ref", "ref_src"}


def clean_website_url(url: str) -> str:
    """Strip common tracking parameters from a business website URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        query_dict = parse_qs(parsed.query, keep_blank_values=True)
        clean_dict = {k: v for k, v in query_dict.items() if k.lower() not in _TRACKING_PARAMS}
        return urlunparse(parsed._replace(query=urlencode(clean_dict, doseq=True)))
    except Exception:
        return url


async def _dismiss_consent(page) -> bool:
    """Dismiss the Google consent/cookie banner if present. Returns True if any was closed."""
    for sel in CONSENT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def _collect_place_targets(page, max_results: Optional[int]) -> List[Dict[str, str]]:
    """Phase A: scroll the feed and collect {name, href, rating} place targets."""
    seen: Dict[str, Dict[str, str]] = {}
    prev_count = -1
    no_change = 0

    for _ in range(60):  # hard cap on scrolls to avoid infinite loops
        rows = await page.evaluate(FEED_JS)
        if isinstance(rows, dict):  # defensive: older versions returned {results: []}
            rows = rows.get("results", []) or []
        for row in rows:
            name = (row.get("name") or "").strip()
            href = (row.get("href") or "").strip()
            if name and href and name not in seen:
                seen[name] = {
                    "name": name,
                    "href": href,
                    "rating": (row.get("rating") or "").strip(),
                }

        cur = len(seen)
        # Stop early if we already have enough results.
        if max_results and cur >= max_results:
            break

        advanced = await page.evaluate(SCROLL_JS)
        await page.wait_for_timeout(2200)

        if not advanced or cur == prev_count:
            no_change += 1
            if no_change >= 3:
                break
        else:
            no_change = 0
        prev_count = cur

    targets = list(seen.values())
    if max_results:
        targets = targets[:max_results]
    return targets


async def _fetch_place_details(page, targets: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Phase B: visit each place URL and pull full listing details from its panel."""
    results = []
    for i, target in enumerate(targets):
        try:
            await page.goto(target["href"], wait_until="commit", timeout=60000)
        except Exception as exc:  # pragma: no cover - network flakiness
            logger.warning(f"[{i+1}/{len(targets)}] goto failed for {target['name']}: {exc}")
            continue

        # Wait for the detail panel to render. Non-fatal: we still attempt the
        # extraction after a short settle so a slow panel doesn't kill the row.
        try:
            await page.wait_for_selector(
                "h1.DUwDvf, h2.DUwDvf, [data-item-id='address']", timeout=45000
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[{i+1}/{len(targets)}] panel not detected for {target['name']}: {exc}")

        # Let lazy fields (phone, website) settle before reading the panel.
        await page.wait_for_timeout(3000)

        try:
            d = await page.evaluate(PLACE_JS)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[{i+1}/{len(targets)}] extract failed for {target['name']}: {exc}")
            continue

        if not isinstance(d, dict):
            continue

        name = (d.get("name") or "").strip() or target["name"]
        website = (d.get("website") or "").strip()
        # Guard against a Google ad click-tracker ever leaking through.
        if "/aclk?" in website:
            website = ""
        results.append({
            "name": name,
            "rating": (d.get("rating") or "").strip() or target.get("rating", ""),
            "review_count": (d.get("review_count") or "").strip(),
            "address": (d.get("address") or "").strip(),
            "phone": (d.get("phone") or "").strip(),
            "website": clean_website_url(website),
        })
        logger.info(f"[{i+1}/{len(targets)}] {name} | rating={results[-1]['rating']} "
                    f"reviews={results[-1]['review_count']} phone={results[-1]['phone']} "
                    f"website={results[-1]['website']}")
    return results


async def scrape_google_maps(
    query: str,
    location: Optional[str] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    Search Google Maps for `query` (optionally restricted to `location`) and
    return [{name, rating, review_count, address, phone, website}, ...].
    Raises on unrecoverable errors.
    """
    url = build_maps_url(query, location)
    logger.info(f"Scraping Google Maps: {url}")

    browser_config = _maps_browser_config()
    run_config = CrawlerRunConfig(
        session_id="google-maps-scrape",
        cache_mode="bypass",
        page_timeout=60000,
        wait_until="domcontentloaded",
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        page, context = await crawler.crawler_strategy.browser_manager.get_page(run_config)

        try:
            # Navigate with a fast "commit" (returns the moment the response
            # commits), then wait for the results feed to appear. This is much
            # more robust than waiting for full domcontentloaded, which can
            # time out when Google Maps is slow under bot-detection.
            last_err = None
            for attempt in range(3):
                try:
                    await page.goto(url, wait_until="commit", timeout=60000)
                    last_err = None
                    break
                except Exception as exc:  # pragma: no cover - network flakiness
                    last_err = exc
                    logger.warning(f"goto attempt {attempt+1} failed: {exc}")
                    await page.wait_for_timeout(4000)
            if last_err is not None:
                raise RuntimeError(f"Could not load Google Maps page: {last_err}")

            # Wait for the results feed to render (handles slow loads without
            # failing the goto). Raises a clear error if Google never renders.
            try:
                await page.wait_for_selector("div[role='feed']", timeout=45000)
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"Google Maps results feed did not load: {exc}")

            # Let the result cards render, then dismiss any consent banner.
            await page.wait_for_timeout(6000)
            try:
                await _dismiss_consent(page)
            except Exception:  # pragma: no cover
                pass

            # Phase A: collect place targets from the scrollable feed.
            targets = await _collect_place_targets(
                page, max_results=max_results or 999
            )
            logger.info(f"Collected {len(targets)} place target(s) from the feed")

            if not targets:
                return []

            # Phase B: visit each place and extract full listing details.
            return await _fetch_place_details(page, targets)

        finally:
            try:
                await context.close()
            except Exception:  # pragma: no cover
                pass
