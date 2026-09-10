# Google Maps Business Scraper — Complete Documentation

This document describes the Google Maps scraper in the Crawl4AI lead-gen project:
its files, functions, DOM selectors, data flow, output handling, troubleshooting,
and which parts are safe to modify.

**Scope:** This scraper ONLY powers the `POST /maps` endpoint. The DuckDuckGo
search pipeline and the HTML-cleaning pipeline (`POST /clean`) share the same
server file but are independent and are NOT covered here.

---

## 1. File Locations & Responsibilities

| File | Role |
|---|---|
| `/home/openclaw/hermesworkspace/crawl4ai-project/maps_scraper.py` | **The scraper.** All Google Maps browser automation, DOM extraction, scrolling, and per-business detail fetching live here. Imported by the API server. |
| `/home/openclaw/hermesworkspace/crawl4ai-project/crawl_api.py` | **The API server (FastAPI).** Defines the `/maps` request/response models and the `maps_endpoint()` handler that calls `scrape_google_maps()`. Also hosts the unrelated `/clean` (HTML cleaning) pipeline. |
| `/home/openclaw/hermesworkspace/crawl4ai-project/logs/crawl_api.log` | **Runtime log.** Every business scraped is logged here with name / rating / reviews / phone / website. |
| `/home/openclaw/.hermes/skills/crawl4ai/SKILL.md` | Agent skill documentation for the crawler (incl. the `/maps` endpoint usage). |
| `/home/openclaw/.hermes/skills/crawl4ai/...` | Only the `crawl4ai` skill covers this scraper. The `SocialMediacrawler` skill is a different (Facebook/Twitter) project. |
| `/home/openclaw/hermesworkspace/crawl4ai-project/.crawl4ai-venv/` | Python virtualenv containing `crawl4ai`, Playwright, FastAPI, uvicorn, etc. |

---

## 2. Architecture Overview

The scraper is a **two-phase** design. This exists because the Google Maps UI
(2025-2026 layout) does **not** show full contact data in the search-results list:

- The **results feed** (the left-hand list of business cards) shows only:
  business name, star rating (as a bare number), category, short address line,
  and open/closed status. No phone, no review count, no website link, no full
  address.
- The **place detail page** (each business's own `google.com/maps/place/...`
  URL) shows everything: full address, phone, rating + review count, website.

Therefore:

1. **Phase A — feed collection:** scroll the feed, harvest each business's
   *place URL* (plus name and feed-level rating), dedupe by name, stop when
   `max_results` are collected or the feed stops loading.
2. **Phase B — detail extraction:** navigate to each collected place URL one at
   a time and read its detail panel. This is where phone, address, review
   count, and website are pulled from.

Phase B costs ~5-8 seconds per business, so a 25-business request takes roughly
2-4 minutes total.

```
curl POST /maps
      │
      ▼
maps_endpoint()            (crawl_api.py)
      │  validates MapsRequest, calls:
      ▼
scrape_google_maps(query, location, max_results)     (maps_scraper.py)
      │  builds URL, opens headless Chromium (Crawl4AI/Playwright)
      ├──► PHASE A:  _collect_place_targets(page, max_results)
      │       loop: FEED_JS (read cards) → SCROLL_JS (load more) → dedupe
      │       until max_results reached / 3 empty scrolls / 60 scrolls
      ├──► PHASE B:  _fetch_place_details(page, targets)
      │       for each place URL:
      │         goto(place URL) → wait for panel → PLACE_JS (extract 6 fields)
      │         → clean website URL → log INFO line → collect
      ▼
list of {name, rating, review_count, address, phone, website}
      │
      ▼
maps_endpoint() maps rows onto BusinessResult → FastAPI returns JSON array
      ▼
curl receives the JSON (one long line, no trailing newline)
```

---

## 3. `maps_scraper.py` — Functions & Constants

### 3.1 Module-level JS snippets (constants run inside the browser page)

| Constant | Line | Purpose |
|---|---|---|
| `FEED_JS` | 85 | In-page JS. Reads the results feed `div[role="feed"]`, iterates business cards `div[role="article"]`, skips rows whose text contains `Sponsored`, and returns an array of `{name, href, rating}`. `name` comes from the place link's `aria-label`, `href` is the business's own `/maps/place/...` URL, `rating` is the first bare-number line (e.g. `4.8`) in the card text. |
| `SCROLL_JS` | 114 | In-page JS. Scrolls the feed element (checking the feed and then its ancestors for the real scrollable container). Returns `true` if the scroll position actually advanced. Used to trigger lazy loading of more results. |
| `CONSENT_SELECTORS` | 138 | CSS selector list for dismissing Google's consent/cookie banner (`Accept all` buttons, `#L2AGLb`, etc.). |
| `PLACE_JS` | 158 | In-page JS. Runs on an individual **place detail page** and returns `{name, rating, review_count, address, phone, website}`. Details in §6. |

### 3.2 Python functions

| Function | Signature | What it does |
|---|---|---|
| `_maps_browser_config()` | `() -> BrowserConfig` | Builds the **dedicated** Crawl4AI `BrowserConfig` for Maps scraping: headless Chromium, 1400×900 viewport, Linux Chrome/151 user agent, `--no-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--disable-blink-features=AutomationControlled`, `enable_stealth=True`. Kept separate from `crawl_api.py`'s shared `browser_config` so the `/clean` pipeline is untouched. |
| `build_maps_url()` | `(query, location=None) -> str` | Builds `https://www.google.com/maps/search/<query>+<location>?hl=en`. The `hl=en` forces English UI regardless of the server's IP geolocation (otherwise results render in the local language, e.g. Bengali). |
| `clean_website_url()` | `(url) -> str` | Strips tracking parameters (`utm_*`, `fbclid`, `gclid`, `gclsrc`, `mc_*`, `yclid`, `twclid`, `srsltid`, `ref`, ...) from a website URL. The list lives in the module constant `_TRACKING_PARAMS`. |
| `_dismiss_consent()` | `(page) -> bool` | Clicks the first visible consent-banner selector from `CONSENT_SELECTORS`. Returns `True` if something was dismissed. Failures are swallowed (banner may not exist). |
| `_collect_place_targets()` | `(page, max_results) -> List[Dict]` | **Phase A.** Loops up to 60 times: evaluates `FEED_JS`, adds unseen `{name, href, rating}` rows to a `seen` dict (deduped **by name**), then evaluates `SCROLL_JS` and waits 2.2 s for lazy loading. Breaks when `len(seen) >= max_results`, or after 3 consecutive scrolls that produced no new rows / no scroll movement. Truncates the final list to `max_results`. |
| `_fetch_place_details()` | `(page, targets) -> List[Dict]` | **Phase B.** For each target (in feed order = Google ranking order): `page.goto(target["href"], wait_until="commit")`, waits up to 45 s for the detail panel (`h1.DUwDvf`, `h2.DUwDvf`, or `[data-item-id="address"]`), waits 3 s for lazy fields to settle, then evaluates `PLACE_JS`. If the panel reports an empty name it falls back to the feed name; if the panel reports no rating it falls back to the feed rating. Website is passed through `clean_website_url()`. Logs one INFO line per business. Rows whose navigation or extraction fails are skipped with a warning (the rest still return). |
| `scrape_google_maps()` | `(query, location=None, max_results=None) -> List[Dict]` | **Public entry point.** Opens the headless browser via Crawl4AI's `browser_manager.get_page()` (a long-lived page — see §8 for why), navigates to the search URL (3 goto attempts, `wait_until="commit"`), waits up to 45 s for `div[role="feed"]`, waits 6 s for cards to render, dismisses consent, then runs Phase A and Phase B in the **same browser context**. Closes the context in a `finally` block. Raises `RuntimeError` if the search page never loads. |

---

## 4. `crawl_api.py` — API layer

### 4.1 `/maps` request model (`MapsRequest`)
```python
class MapsRequest(BaseModel):
    query: str                # required — business type, e.g. "digital marketing agencies"
    location: Optional[str]   # optional — city/area, e.g. "New York"
    max_results: Optional[int] = 25   # optional cap, must be >= 1
```

### 4.2 `/maps` response model (`BusinessResult`)
```python
class BusinessResult(BaseModel):
    name: str          # business name
    website: str = ""  # the Website row from the Google listing (tracking params stripped)
    phone: str = ""    # full phone as rendered, e.g. "+1 212 555 0134"
    address: str = ""  # full address, e.g. "123 Madison Ave, New York, NY 10016, United States"
    rating: str = ""   # star rating as string, e.g. "4.8"
    review_count: str = ""  # integer count as string, NO commas, e.g. "1423"
```
All fields except `name` default to `""` — a business that genuinely has no
phone or website returns an empty string, and the row is **kept** (it still has
a name/address/rating).

### 4.3 `maps_endpoint(req)` handler
- Validates `query` non-empty and `max_results >= 1` (else HTTP 400).
- Calls `scrape_google_maps(req.query, location=req.location, max_results=req.max_results)`.
- Maps each returned dict onto `BusinessResult` and returns the list. FastAPI
  serializes it to JSON automatically.

### 4.4 Server startup
```python
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
```
- The server listens on **port 8000** on all interfaces.
- `crawl_api.py` imports `scrape_google_maps` from `maps_scraper.py` at module
  top — **restart the server after editing either file** (Python caches imports).

---

## 5. How to run it

### 5.1 Start the server
```bash
cd /home/openclaw/hermesworkspace/crawl4ai-project
source .crawl4ai-venv/bin/activate
python crawl_api.py
```
Health check: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs`
should return `200`.

### 5.2 Fire a scrape
```bash
curl -s -X POST "http://localhost:8000/maps" \
  -H "Content-Type: application/json" \
  -d '{"query": "digital marketing agencies", "location": "New York", "max_results": 25}'
```

---

## 6. Google Maps DOM Structure & Selectors

> ⚠️ Google Maps is a heavy JS app and its DOM changes periodically. The class
> names below (`Nv2PK`, `DUwDvf`, `F7nice`, `hfpxzc`) are **obfuscated names**
> that Google rotates over time. They were verified live in September 2026.
> If the scraper silently returns empty fields, re-probe the DOM (§10.6).

### 6.1 Search results page (Phase A)

```
div[role="feed"]                          ← the results list; ITSELF the scroll container
└── div[role="article"]  (class Nv2PK)    ← one business card
    └── a.hfpxzc[href*="/maps/place/"]    ← place link; aria-label = business NAME;
    │                                        href = that business's own place URL
    └── card text lines:
          <Business Name>                 (duplicated for a11y)
          4.8                             ← star rating as bare number line
          Dentist · 30-32 Chalton St      ← category · short address
          Closed · Opens 9 AM Mon         ← open-hours line
```

| Data | Selector / method |
|---|---|
| Business name | `a[href*="/maps/place/"]` → `aria-label` |
| Place URL (Phase B target) | same anchor → `href` |
| Rating (fallback only) | first line in card text matching `^\d(\.\d)?$` |
| Sponsored rows (excluded) | card text matching `/Sponsored/` |

### 6.2 Place detail page (Phase B)

Navigating to the place URL renders the business's detail panel:

| Data | Selector | Notes |
|---|---|---|
| Name | `h1.DUwDvf` (overlay views: `h2.DUwDvf`) | Hidden a11y `h1` may read "Results" — ignore it. |
| Rating | `div.F7nice` | Text like `4.8 (895)`; rating is the first float. |
| Review count | inside `div.F7nice`: `[role="img"][aria-label*="review" i]` | aria-label like `"1,423 reviews"`; commas stripped. |
| Address | `button[data-item-id="address"]` | Full address in its `aria-label` (after `Address: `) or visible text. |
| Phone | `[data-item-id^="phone:"]` | **Read visible text or `aria-label` only — never the attribute** (see §6.3). |
| Website | `a[data-item-id="authority"]`, fallback `a[aria-label^="Website:"]`, fallback `a[data-item-id^="action:"]` | The `authority` link is Google's stored "Website" row. |

### 6.3 Critical quirk: phone masking
When Google suspects automation, it **masks the middle digits of phone numbers
in HTML attributes** while the rendered text stays complete:

```html
<!-- attribute is masked: -->
<button data-item-id="phone:tel:+4420****5272">…</button>
<a href="tel:+4420****5272">…</a>
<!-- but visible text / aria-label are complete: -->
+44 20 3757 5272
aria-label="Phone: +44 20 3757 5272"
```
This is why `PLACE_JS` always reads the phone from `innerText`/`aria-label` and
**never** from `data-item-id` or `href`. (Masked attributes are also a signal
that Google is serving you bot-detection DOM — see §10.5.)

---

## 7. Where the results go

1. **HTTP response body** — the JSON array is the only formal output channel.
2. **Server log** — every business is logged at INFO in
   `crawl4ai-project/logs/crawl_api.log`:
   ```
   [12/25] Main Street ROI | rating=4.9 reviews=241 phone=+1 646-470-3753 website=https://www.mainstreetroi.com/
   ```
   The log is a handy fallback when terminal output is lost (§8) and a good way
   to confirm a run without re-scraping.
3. **Nothing is stored on disk.** The scraper does not write CSV/JSON files; the
   caller is responsible for saving the response (e.g. `curl -o file.json`).

---

## 8. Why output sometimes "disappears" in the terminal

This is the #1 confusion with this scraper. The scrape works; the terminal
display lies. Causes:

1. **curl's progress meter clobbers the output.** curl draws its progress meter
   on stderr with carriage returns (`\r`) on the same line. The JSON body goes
   to stdout. When both go to the same terminal, the final meter redraw can
   overwrite the start of the JSON line.
2. **The JSON is one single line with no trailing newline.** FastAPI serializes
   lists compactly, so the response is one very long line that does not end with
   `\n`. Without a trailing newline the shell prompt can appear glued to or over
   the data, and scrolling back doesn't help because the meter overwrote it.
3. Long scrape times (minutes) make the meter fill the screen, so the tail of
   the terminal looks like "nothing happened" even though bytes arrived
   (look for `100 5347 100 5272` style lines — the body WAS received).

### 8.1 Correct ways to display / retrieve results

```bash
# A) Pretty-print: -s silences the meter, json.tool reformats (recommended)
curl -s -X POST "http://localhost:8000/maps" \
  -H "Content-Type: application/json" \
  -d '{"query": "digital marketing agencies", "location": "New York", "max_results": 25}' \
  | python3 -m json.tool

# B) Save to a file, then inspect
curl -s -X POST "http://localhost:8000/maps" \
  -H "Content-Type: application/json" \
  -d '{"query": "digital marketing agencies", "location": "New York", "max_results": 25}' \
  -o maps_ny.json
python3 -m json.tool maps_ny.json

# C) jq, if installed
curl -s … | jq .

# D) Already ran it and lost the output? Check the server log:
tail -30 /home/openclaw/hermesworkspace/crawl4ai-project/logs/crawl_api.log
```

---

## 9. What each data field depends on

| Field | Produced by | Source in DOM |
|---|---|---|
| `name` | `PLACE_JS` (fallback `FEED_JS`) | `h1.DUwDvf` / `h2.DUwDvf` (fallback: feed card `aria-label`) |
| `rating` | `PLACE_JS` (fallback feed rating) | `div.F7nice` first float |
| `review_count` | `PLACE_JS` | `div.F7nice` → `[aria-label*="review" i]` |
| `address` | `PLACE_JS` | `button[data-item-id="address"]` aria/text |
| `phone` | `PLACE_JS` | `[data-item-id^="phone:"]` visible text / aria (NOT attribute) |
| `website` | `PLACE_JS` + `clean_website_url()` | `a[data-item-id="authority"]` (fallbacks: `a[aria-label^="Website:"]`, `a[data-item-id^="action:"]`) |
| Order / dedupe | `_collect_place_targets()` | Feed scroll order; deduped by name |
| Sponsored removal | `FEED_JS` | `/Sponsored/` text match |

---

## 10. Known issues, limitations & troubleshooting

### 10.1 Terminal shows no output (scrape actually worked)
See §8. Use `curl -s … | python3 -m json.tool` or `-o file.json`, or read the log.

### 10.2 Results come back incomplete (`""` phone/website)
- Some businesses genuinely have no phone or website on Google — that row is
  correct as-is.
- If MANY rows are empty, Google changed its DOM or is serving bot-detection
  DOM. Re-probe (§10.6) and update selectors in `PLACE_JS`/`FEED_JS`.

### 10.3 Request is slow
Phase B visits every business individually (~5-8 s each). 25 businesses ≈ 2-4
minutes. Lower `max_results` for faster runs. This is inherent to getting phone
numbers — the feed alone cannot provide them.

### 10.4 Some businesses are skipped
`_fetch_place_details()` logs a warning and continues when a place navigation
or extraction fails (network flake, Google redirect, panel timeout). Re-run to
fill gaps; results are not cached, so every request is a fresh scrape
(`cache_mode="bypass"`).

### 10.5 Bot detection
Symptoms: phone digits masked in attributes (normal — we read text anyway),
feed never appears (45 s timeout), "unusual traffic" page, consent loop, very
slow loads.
Mitigations already in place: stealth mode, realistic Linux Chrome UA,
generous viewport, `--disable-blink-features=AutomationControlled`, 3 goto
retries, `wait_until="commit"` + targeted `wait_for_selector` (never
`domcontentloaded`, which can hang for 60+ s on Maps).
If it starts failing: reduce request frequency, lower `max_results`, wait a
few minutes, and check `logs/crawl_api.log` for the exact error.

### 10.6 Google Maps DOM changed (selectors stale)
Class names like `DUwDvf`/`F7nice`/`Nv2PK` are rotated by Google. To re-derive
selectors, open a headed probe of the search page + one place page and dump the
`[data-item-id]` attributes, `aria-label`s and `h1/h2` classes (the same
technique used to build this scraper). Update `FEED_JS`/`PLACE_JS` accordingly
and restart the server.

### 10.7 Server won't start / port busy
- `Address already in use` → another instance is running (or a stale one from a
  previous session). Kill it: `pkill -f crawl_api.py`, then start again.
- ImportError at startup → you edited `crawl_api.py`/`maps_scraper.py` and broke
  the import; check the traceback. The two files are coupled via
  `from maps_scraper import scrape_google_maps`.

### 10.8 WSL / headless browser quirks
- Headed Chromium dies on WSL without `DISPLAY`; this scraper is `headless=True`
  so it doesn't need one, but `maps_scraper.py` still auto-sets
  `DISPLAY=:0` (WSLg) when present for safety.
- If Chromium crashes with sandbox errors, the `--no-sandbox` /
  `--disable-dev-shm-usage` args in `_maps_browser_config()` are the fix.
- In agent/Hermes shells, running the venv's python directly can be blocked;
  wrapping the start command in a `/tmp/*.sh` script is the known workaround.

### 10.9 Dependencies
- Python 3.11/3.12 venv: `/home/openclaw/hermesworkspace/crawl4ai-project/.crawl4ai-venv`
- `crawl4ai` (0.9.2 era) + Playwright Chromium browser binaries
- `fastapi`, `uvicorn`, `pydantic` v2, `beautifulsoup4` (server side)
- No external API keys — scraping is done through the browser.

---

## 11. What is safe to modify (and what each change affects)

### Safe — scraping behaviour (restart server after edits)

| Edit | Where | Effect |
|---|---|---|
| Default result cap | `MapsRequest.max_results = 25` in `crawl_api.py` | Changes the default when the caller omits `max_results`. |
| Request-timeout / render waits | `page.wait_for_timeout(...)`, `wait_for_selector(timeout=...)` in `maps_scraper.py` | Trades speed vs. reliability. Increase if slow networks/bot detection miss rows; decrease for faster runs. |
| Feed scroll depth / patience | `_collect_place_targets()` loop bounds (60 scrolls, `no_change >= 3`) | How many results can be gathered before giving up. |
| New output field | add extraction to `PLACE_JS` + a field to `BusinessResult` + map it in `maps_endpoint()` | e.g. categories, opening hours, services, plus-code, "Open now" status. Hours/attributes live in `button[aria-label*="Copy open hours"]`-style rows / `[data-item-id="oloc"]` — re-probe first. |
| Website tracking-param list | `_TRACKING_PARAMS` in `maps_scraper.py` | Which URL query params get stripped from `website`. |
| Browser fingerprint | `_maps_browser_config()` UA / viewport / args | Bot-detection profile; only affects `/maps`. |
| Consent selectors | `CONSENT_SELECTORS` | Which banners get auto-dismissed. |
| Response field order / names | `BusinessResult` | Breaking change for API consumers. |

### Do NOT touch (breaks the rest of the project)
- `/clean` endpoint logic, `clean_html()`, DuckDuckGo extraction in `crawl_api.py`
  — independent pipelines that share the file.
- `crawl_api.py`'s shared `browser_config`/`run_config` (used by `/clean`).
- The Crawl4AI library itself (`maps_scraper.py` deliberately uses its own
  dedicated `BrowserConfig` so `/clean` stays unchanged).

### Safety rule from the project
Only the Google Maps capability (`maps_scraper.py`, the `/maps` endpoint and
its models, and the `crawl4ai` skill doc) may be changed without explicit
authorization — that was the original scope of this feature.

---

## 12. Key technical facts recap

- `session_id="google-maps-scrape"` — Phase A and B share one browser page/context
  (Crawl4AI `arun()` tears pages down after one crawl; the scraper uses
  `browser_manager.get_page()` + manual `goto()` instead, so it can navigate to
  25 place URLs in one session).
- Every place navigation uses `wait_until="commit"` — returns the instant the
  response commits, far more robust on Maps than `domcontentloaded`.
- `cache_mode="bypass"` — every request is a fresh scrape.
- Order of the returned list = Google feed ranking order (Phase A collection
  order, which Phase B preserves).
- Rating/review-count edge cases: no reviews → `rating`/`review_count` fall back
  to `""`; rating can be `"5.0"`; review counts arrive comma-formatted
  (`1,423`) and are normalized to `1423`.
- Sample response:
  ```json
  [
    {
      "name": "Main Street ROI",
      "website": "https://www.mainstreetroi.com/",
      "phone": "+1 646-470-3753",
      "address": "228 E 45th St, New York, NY 10017, United States",
      "rating": "4.9",
      "review_count": "241"
    }
  ]
  ```
