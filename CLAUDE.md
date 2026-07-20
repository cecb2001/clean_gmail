# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the app
flask --app app run --debug

# Run all tests
pytest

# Run a single test file
pytest tests/test_scoring.py

# Run a single test by name
pytest tests/test_scoring.py::test_delete_confidence_basic
```

## Architecture

Single-process Flask app — no task queue, no worker processes. All background work (Gmail fetches, batch deletes) runs in Python threads (`threading.Thread`) started from route handlers.

**Key modules:**

- `app.py` — All Flask routes and global in-memory state (`_analysis_cache`, `_analysis_job`, `_delete_job`, `_last_analyzer`). The file is large; search by route name or the `# ---` section dividers.
- `gmail/analyzer.py` — `EmailAnalyzer`: fetches email metadata from Gmail API, builds per-sender stats, and scores each email.
- `gmail/scoring.py` — Pure functions (no I/O). Computes `protect_score`, `junk_score`, and `delete_confidence` for an email dict. Also applies preset filtering. All scoring weights live in `config.py`.
- `gmail/client.py` — Thin wrapper around the Gmail REST API.
- `gmail/auth.py` — OAuth 2.0 flow using `google-auth-oauthlib`. Writes `token.json` on success.
- `store.py` — SQLite persistence (`analysis.db`). One active run per Gmail address, auto-expires after `config.ANALYSIS_TTL_DAYS` (7 days). Stores raw email rows; scoring is re-run at load time so preset changes don't require re-fetching from Gmail.
- `preferences.py` — JSON file store (`preferences.json`) for allowlist, denylist, and the sent-recipients cache (engaged senders). Writes are atomic (temp file + `os.replace`).
- `config.py` — Single source of truth for scoring weights, preset definitions, regex patterns, and domain seed lists. Edit here to change scoring behavior.

**Data flow:**

1. User authenticates via OAuth → `token.json` written.
2. `/api/analyze` spawns a thread that calls `EmailAnalyzer.fetch_and_analyze()`, then saves results to SQLite via `store.save_run()`.
3. On subsequent loads `store.load_run()` returns persisted emails; the analyzer rescores them in-place (no Gmail API call).
4. Deletions go through `/api/delete` → Gmail API trash/permanent → `store.mark_deleted()` → in-memory aggregate patched without a full rescore.

**Frontend:**

- `templates/patterns.html` — primary UI (~3000 lines of inline JS). All pattern drill-downs, bulk-select, and delete flows live here.
- `static/style.css` — app styles.
- `static/app.js` — minimal shared JS (OAuth redirect helpers).
- Templates use Jinja2 and call Flask JSON endpoints from JavaScript (`fetch`).

## Scoring system

`delete_confidence = max(0, junk_score - protect_score)`. Presets gate on `min_confidence` and `max_protect_score` (see `config.CLEANUP_PRESETS`). Signals and weights are defined in `config.PROTECT_WEIGHTS` / `config.JUNK_WEIGHTS` and applied in `gmail/scoring.py`.

## Local credentials

`credentials.json` is the OAuth client secret downloaded from Google Cloud Console. It is gitignored. `token.json` is written after first authentication and is also gitignored.
