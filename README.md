# crawl4ai stack — one-command bootstrap for a fresh WSL

Source-only repository for the **crawl4ai crawler stack** (FastAPI HTML-cleaner
server + Google Maps business-listing scraper + the Hermes `crawl4ai` skill).

Clone it on a new WSL machine, run one script, and the working setup is back:
the code, the 98-package Python environment, the Chromium browser build, and the
agent skill — all of it downloaded and wired up automatically.

```bash
git clone <this-repo> ~/crawl4ai-stack
cd ~/crawl4ai-stack
bash setup.sh
```

Then:

```bash
bash run.sh                                                   # start the server
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/docs   # 200 = healthy
```

---

## Why the repo is small

Everything that came out of the 474 MB `crawl4ai-full-backup-2026-09-09.tar.gz`
was either **source** (~100 KB) or **rebuildable from the internet**:

| Piece of the old backup | Size there | Here |
|---|---|---|
| `crawl_api.py`, `maps_scraper.py` | 52 KB | committed |
| `crawl4ai-skill/SKILL.md` | 7 KB | committed |
| `gmapdetails/GOOGLE_MAPS_SCRAPER.md` | 22 KB | committed |
| `requirements.txt` (98 pinned pkgs) | 2 KB | committed |
| `.env` (live proxy credential) | 199 B | shipped as `.env.example`, value **not** committed |
| `.crawl4ai-venv/` | 686 MB | rebuilt by `pip install -r requirements.txt` |
| `external/ms-playwright/` (chromium-1223 family) | 641 MB | rebuilt by `playwright install chromium` |

GitHub rejects files over 100 MB, and the venv + browsers are pure download
content — so committing them would be both impossible and pointless.
`requirements.txt` is fully pinned, so the rebuild is version-exact:
**crawl4ai 0.9.0, playwright 1.60.0 → chromium 1223**, the same builds the old
machine ran.

---

## What `setup.sh` does

| Step | Action |
|---|---|
| 1 | Preflight: require `python3.12` (apt-installs it if missing), verify venv support |
| 2 | Install `crawl_api.py` + `maps_scraper.py` into `~/hermesworkspace/crawl4ai-project/`, create `logs/`, create `.env` from `.env.example` if absent |
| 3 | Install the Hermes skill to `~/.hermes/skills/crawl4ai/SKILL.md` and the Maps doc to `~/hermesworkspace/gmapdetails/`, rewriting the old `/home/openclaw/...` paths to this machine's `$HOME` |
| 4 | Create the venv at `crawl4ai-project/.crawl4ai-venv` (reused if already there) |
| 5 | `pip install -r requirements.txt` — 98 pinned packages |
| 6 | `playwright install-deps chromium` (apt libs, via sudo) + `playwright install chromium` |
| 7 | Verify: package versions, imports, and that the chromium/headless-shell/ffmpeg build dirs exist |

Options: `--skip-system-deps` (no sudo/apt), `--skip-browsers` (code only),
`--force-venv` (rebuild the venv from scratch). `bash setup.sh --help` prints the
header.

The script is **idempotent** — re-run it freely. If a step dies (power cut,
network drop), just run it again: completed steps are skipped, so only the
interrupted one repeats.

Overridable env vars: `PROJECT_DIR`, `SKILL_DIR`, `DOCS_DIR`, `VENV`, `PY_BIN`,
`PLAYWRIGHT_BROWSERS_PATH`.

---

## Layout after install

```
~/hermesworkspace/crawl4ai-project/     # project root (cwd for the server)
├── crawl_api.py                        # FastAPI server, port 8000
├── maps_scraper.py                     # Google Maps scraper module
├── requirements.txt
├── .env                                # created from .env.example
├── logs/crawl_api.log                  # written at runtime
└── .crawl4ai-venv/                     # python 3.12 + 98 packages

~/.hermes/skills/crawl4ai/SKILL.md      # agent skill
~/hermesworkspace/gmapdetails/GOOGLE_MAPS_SCRAPER.md   # scraper tech doc
~/.cache/ms-playwright/chromium-1223/   # browser build
```

---

## Requirements

- WSL2 on Ubuntu 24.04 (or any distro with `/usr/bin/python3.12`), network access,
  ~2 GB free disk (venv ≈ 700 MB, browsers ≈ 300 MB).
- `sudo` for step 6's apt libraries (skip with `--skip-system-deps`).
- WSLg is **not** needed — the scraper runs `headless=True`, and the code
  auto-sets `DISPLAY=:0` if WSLg is present.

---

## Using the server

```bash
# clean / extract a website
curl -s -X POST http://localhost:8000/clean \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com/about"]}'

# DuckDuckGo search via /clean (max 3 URLs per request, don't mix types)
curl -s -X POST http://localhost:8000/clean \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://duckduckgo.com/?q=what+is+openclaw"]}'

# Google Maps business listings (~5-8 s per business)
curl -s -X POST http://localhost:8000/maps \
  -H 'Content-Type: application/json' \
  -d '{"query":"digital marketing agencies","location":"Austin","max_results":5}' \
  | python3 -m json.tool
```

`/clean` returns a **list** of per-URL objects (`{url, success, type, …}`);
`/maps` returns a JSON array of businesses
(`name, website, phone, address, rating, review_count`).

---

## Security

- `.env` is git-ignored; the live proxy credential from the old machine is **not**
  in this repo. (`PROXY_URL` is currently inert — no code reads it.)
- The scraper stores business data (names/phones) in `logs/crawl_api.log`, also
  git-ignored.
- Keep the repo **private** if you fork it with real credentials anywhere.

## Provenance

Extracted from `crawl4ai-full-backup-2026-09-09.tar.gz` (the archive itself is
**not** part of this repo). See `RESTORE.md` for the mapping table and the
verification trail.
