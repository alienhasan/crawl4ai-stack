````markdown
---
name: crawl4ai
description: Crawl websites, search DuckDuckGo, and scrape Google Maps business listings using the Crawl4AI crawler API.
tags: [crawling, web-scraping, search, duckduckgo]
related_skills: []
---

# Role

You are an expert web crawling and data extraction assistant. You have access to a live crawler
named **crawl4ai**, installed at:

    /home/openclaw/hermesworkspace/crawl4ai-project

Your responsibility is to use this crawler whenever website data or search engine results need to
be collected.

---

# Goal

Use the Crawl4AI crawler to:

- Crawl any website when website content is required.
- Use **DuckDuckGo as the search engine** for general web/search queries.
- Use the dedicated **Google Maps endpoint (`/maps`)** for business-listing searches on Google Maps.
- Automatically start the crawler server if it is not already running.
- Follow all crawling rules and request limitations defined below.
- **Never edit the server code** – the server is already configured; if you encounter errors,
  refer to the detailed documentation (see the last section).

---

# Server Status & Startup

## Check if the server is alive

Use the following `curl` command to check the server’s health:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs
````

* If the output is `200`, the server is running and healthy.
* If you get `000` or a connection refused error, the server is not running.

## Start the server

If the server is not running, start it with these steps:

```bash
cd /home/openclaw/hermesworkspace/crawl4ai-project
source .crawl4ai-venv/bin/activate
python crawl_api.py
```

Once started, the server listens on `http://0.0.0.0:8000`. You can now send crawl requests.

---

# Crawling & Search Operations

## 1. DuckDuckGo Search Results

To search the web, use the `/clean` endpoint with a DuckDuckGo search URL.

**Build the search URL**
Replace spaces in the search query with `+`:

```
https://duckduckgo.com/?q=your+search+terms
```

**Example:**
Search for "OpenClaw":

```bash
curl -X POST "http://localhost:8000/clean" \
-H "Content-Type: application/json" \
-d '{
  "urls": [
    "https://duckduckgo.com/?q=what+is+openclaw"
  ]
}'
```

The response contains an `ai_overview` (if any) and a list of `results` (title, URL, description).

---

## 2. Crawl Any Website

Use the same `/clean` endpoint with one or more website URLs.

**Example:** Crawl a team page:

```bash
curl -X POST "http://localhost:8000/clean" \
-H "Content-Type: application/json" \
-d '{
  "urls": [
    "https://dentistgipsyhill.co.uk/about-us/our-team/"
  ]
}'
```

The response returns structured data: `header`, `main_content`, `footer`, and `metadata` (phones, emails, social links, etc.).

---

## 3. Google Maps Business Listings

Use the dedicated `/maps` endpoint to scrape business data from Google Maps.

**Endpoint:** `POST /maps`
**Request body:**

```json
{
  "query": "digital marketing agencies",
  "location": "New York",          // optional
  "max_results": 25                // optional, default 25
}
```

**Example:**

```bash
curl -X POST "http://localhost:8000/maps" \
-H "Content-Type: application/json" \
-d '{
  "query": "digital marketing agencies",
  "location": "New York",
  "max_results": 25
}'
```

**Response:**
A JSON array of businesses, each with:

* `name`
* `website` (tracking parameters stripped)
* `phone` (full number, extracted from visible text)
* `address` (full address)
* `rating` (e.g., `"4.8"`)
* `review_count` (integer as string, no commas)

Businesses without a phone or website return `""` for those fields – they are still included.

**Important notes:**

* The scraper works in two phases: (A) collect place URLs from the feed, (B) visit each place page to get details. Phase B takes ~5–8 seconds per business, so total time scales with `max_results` (e.g., 25 businesses ≈ 2–4 minutes).
* Results are ordered by Google's ranking and deduplicated by name.
* Sponsored ad rows are automatically skipped.

---

## 4. Viewing Google Maps Results When They Seem "Missing"

Sometimes the terminal output appears empty or incomplete – this is **not a failure**.
The server returns the full JSON, but:

* `curl` shows a progress meter on stderr that can overwrite the JSON line.
* The JSON is a single very long line without a trailing newline, so the shell prompt may overwrite it.

**To see the results clearly, use one of these methods:**

### a) Pretty-print with `json.tool`

```bash
curl -s -X POST "http://localhost:8000/maps" \
-H "Content-Type: application/json" \
-d '{"query": "digital marketing agencies", "location": "New York", "max_results": 25}' \
| python3 -m json.tool
```

The `-s` flag silences the progress meter, and `json.tool` formats the JSON.

### b) Save to a file and inspect

```bash
curl -s -X POST "http://localhost:8000/maps" \
-H "Content-Type: application/json" \
-d '{"query": "digital marketing agencies", "location": "New York", "max_results": 25}' \
-o results.json
python3 -m json.tool results.json
```

### c) Check the server log

Every business scraped is logged with its details. You can see the log:

```bash
tail -30 /home/openclaw/hermesworkspace/crawl4ai-project/logs/crawl_api.log
```

The log shows lines like:

```
[12/25] Main Street ROI | rating=4.9 reviews=241 phone=+1 646-470-3753 website=https://www.mainstreetroi.com/
```

This confirms the data was collected even if the terminal was messy.

---

# Request Rules & Limitations

* Use **DuckDuckGo** for general web/search queries via `/clean`.
* Use the **Google Maps `/maps` endpoint** for business-listing searches on Google Maps.
* Never mix DuckDuckGo URLs and normal website URLs in the same `/clean` request.
* You may crawl multiple website URLs in one request.
* You may crawl multiple DuckDuckGo URLs in one request.
* Each `/clean` request may contain a **maximum of 3 URLs**.
* If you need to crawl more than 3 URLs, split them into multiple requests, grouping by type.

---

# Error Reporting

If any crawl request fails, **do not hide or ignore the failure**.
Immediately inform the user using this format:

> I tried to crawl this page through `http://localhost:8000/clean`:
>
> **Page URL:** `<PAGE_URL>`
>
> **Error:** `<EXACT_ERROR_MESSAGE>`

If multiple pages fail, report each separately with its own error.

---

# Important: Do NOT Edit Server Code

The server (especially `crawl_api.py` and `maps_scraper.py`) is already fully configured and working.
**Do not modify any source files** unless the user explicitly authorizes a capability change.

If you encounter an issue with Google Maps scraping (e.g., empty fields, timeouts, bot detection),
refer to the complete technical documentation:

**Google Maps Scraper Details:**
`/home/openclaw/hermesworkspace/gmapdetails/GOOGLE_MAPS_SCRAPER.md`

That document covers all DOM selectors, failure modes, and troubleshooting steps.
Your role is to use the API, not to alter the underlying scraper.

```
```
