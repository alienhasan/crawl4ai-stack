# RESTORE — crawl4ai stack (source-only repository)

This repo is the **setup half** of `crawl4ai-full-backup-2026-09-09.tar.gz`
(474 MB, made 2026-09-09 on WSL2 / Ubuntu 24.04). The archive's contents were
split into "source" (committed here, ~100 KB) and "downloadable" (rebuilt by
`setup.sh` from PyPI + the Playwright CDN). The 474 MB tarball is **not** in this
repo and is not needed to reproduce the setup.

## Mapping: backup path → this repo → restore destination

| Backup path | This repo | Restores to |
|---|---|---|
| `crawl4ai-project/crawl_api.py` | `crawl4ai-project/crawl_api.py` | `~/hermesworkspace/crawl4ai-project/crawl_api.py` |
| `crawl4ai-project/maps_scraper.py` | `crawl4ai-project/maps_scraper.py` | `~/hermesworkspace/crawl4ai-project/maps_scraper.py` |
| `crawl4ai-project/requirements.txt` | `crawl4ai-project/requirements.txt` | same dir |
| `crawl4ai-project/.env` | `.env.example` (credential stripped) | `~/hermesworkspace/crawl4ai-project/.env` (mode 600) |
| `crawl4ai-project/logs/crawl_api.log` | — (runtime output, git-ignored) | recreated on first run |
| `crawl4ai-project/.crawl4ai-venv/` (686 MB) | — | **rebuilt**: `python3.12 -m venv` + `pip install -r requirements.txt` |
| `external/ms-playwright/chromium-1223*` (641 MB) | — | **rebuilt**: `playwright install chromium` (playwright 1.60.0 pins 1223) |
| `crawl4ai-skill/SKILL.md` | `crawl4ai-skill/SKILL.md` | `~/.hermes/skills/crawl4ai/SKILL.md` |
| `gmapdetails/GOOGLE_MAPS_SCRAPER.md` | `gmapdetails/GOOGLE_MAPS_SCRAPER.md` | `~/hermesworkspace/gmapdetails/GOOGLE_MAPS_SCRAPER.md` |

Two deltas versus the archive: the venv's `.pyc` byte-caches and the browser
binaries are not shipped (regenerable), and the skill/doc files are installed
with the old `/home/openclaw` paths rewritten to the new `$HOME` (the repo itself
keeps the original text so it is diff-able against the backup).

## Restore (fresh WSL)

```bash
git clone <this-repo> ~/crawl4ai-stack && cd ~/crawl4ai-stack
bash setup.sh                      # ~5-8 min: venv + 98 packages + chromium
bash run.sh                        # start server on :8000
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs   # 200
```

Non-default destinations:

```bash
PROJECT_DIR=$HOME/work/c4a SKILL_DIR=$HOME/.hermes/skills/crawl4ai \
  DOCS_DIR=$HOME/work/gmapdetails bash setup.sh
```

## Machine requirements

- `/usr/bin/python3.12` present (Ubuntu 24.04 default; the pinned wheels —
  playwright 1.60.0, pydantic 2.13.4, numpy 2.5.1 — are py3.12 builds).
- Network access to PyPI and `cdn.playwright.dev` (or `mirror.ghproxy.com` style
  mirror via `PLAYWRIGHT_DOWNLOAD_HOST`).
- ~2 GB free disk.
- `sudo` for `playwright install-deps` (only if chromium refuses to launch).

## Verification trail (this repo, built 2026-09-10)

- `requirements.txt` extracted verbatim from the archive (98 `name==version`
  lines, generated from dist-info metadata on 2026-09-09).
- `crawl_api.py`, `maps_scraper.py`, `SKILL.md`, `GOOGLE_MAPS_SCRAPER.md` copied
  byte-identical from the archive (SKILL.md confirmed identical to the live
  `~/.hermes/skills/crawl4ai/SKILL.md`).
- Repo code contains **no** hardcoded `/home/openclaw` paths (grep clean) — only
  the skill + doc did, and `setup.sh` rewrites those at install time.
- `bash -n setup.sh` syntax-clean; `setup.sh` dry-run/integration result is
  recorded in the project chat (sandbox install into a throwaway dir, endpoint
  smoke test).

## If you ever want the byte-identical machine back

Attach the 474 MB tarball to a **GitHub Release** of this repo (release assets
accept up to 2 GiB and don't count against repo size), then:

```bash
curl -L -o /tmp/c4a.tar.gz <release-asset-url>
tar -xzf /tmp/c4a.tar.gz -C /tmp            # RESTORE.md inside has the old steps
```

Do **not** put the tarball in normal git history (100 MB per-file hard limit) or
Git LFS (free tier = 1 GB storage / 1 GB monthly bandwidth; one clone burns
half of it).
