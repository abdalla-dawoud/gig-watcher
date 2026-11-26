# Gig Watcher — freelance/remote-job digest automation

A small Python CLI that watches remote-job feeds, filters posts by your keywords, de-duplicates them with SQLite, and writes a clean Markdown/HTML digest. Ships with a GitHub Actions workflow that rebuilds the digest every morning and commits it to the repo.

**Zero dependencies** — Python 3.10+ standard library only.

## Quick start

```bash
python gig_watcher.py                      # defaults: digest.md from all sources
python gig_watcher.py --demo               # print to stdout, don't write files
python gig_watcher.py --keywords python junior arabic --since-hours 48
python gig_watcher.py --out digest.md --html digest.html
```

## How it works

1. **Fetch** — pulls RSS/Atom feeds (WeWorkRemotely, Himalayas) and the Remotive JSON API. One failing source never kills a run; failures are listed at the bottom of the digest.
2. **Filter** — case-insensitive keyword match over title + summary. Short keywords (3 chars or fewer) match whole words only, so `ai` does not fire on `email`.
3. **De-duplicate** — every reported link is stored in `seen.sqlite3`; repeat runs only show what's new.
4. **Publish** — writes `digest.md` (and optional `digest.html`). The included GitHub Action runs daily at 07:00 UTC (09:00 Cairo) and commits the result.

## Scheduled run (GitHub Actions)

`.github/workflows/daily.yml` is ready to go: push this folder to a GitHub repo, open the **Actions** tab, and the digest starts arriving in the repo every morning. Change the cron line or the keywords in the workflow to taste.

## Configuration

Edit the two constants at the top of `gig_watcher.py`:

```python
DEFAULT_SOURCES  = [...]   # (name, url, "rss" | "json")
DEFAULT_KEYWORDS = [...]   # your stack / interests
```

Or pass `--keywords` / `--since-hours` on the command line for one-off runs.

## Use it for anything

The same skeleton watches any RSS feeds: client leads, competitor changelogs, news topics, price drops. Swap `DEFAULT_SOURCES` and you're done.

MIT license.
