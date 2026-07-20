# clean_gmail

A local Flask app that analyzes your Gmail inbox and recommends emails to delete, grouped by sender patterns. It uses the Gmail API to read metadata (never email bodies), scores each sender by engagement signals, and lets you bulk-trash or permanently delete low-value mail.

## Prerequisites

- Python 3.11+
- A Google Cloud project with the **Gmail API** enabled
- OAuth 2.0 credentials for a **Web application** (not Desktop)

## Initial setup

### 1. Create Google Cloud credentials

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create an **OAuth 2.0 Client ID** → Application type: **Web application**.
3. Under **Authorized redirect URIs**, add `http://localhost:5000/oauth2callback`.
4. Download the JSON file and save it as `credentials.json` in the project root.

### 2. Install dependencies

**macOS / Linux**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If PowerShell says "running scripts is disabled", run this once to allow local scripts:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Then re-run `.venv\Scripts\Activate.ps1`. Alternatively, use **Command Prompt** instead (no policy restriction).

**Windows (Command Prompt)**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

### 3. Run the app

```bash
flask --app app run --debug
```

Open `http://localhost:5000`. The app will redirect you through Google's OAuth consent screen on first run. After granting access, `token.json` is written locally — subsequent runs skip the OAuth step.

> **Note:** `credentials.json` and `token.json` are gitignored. Never commit them.

## Usage

1. **Analyze** — click **Analyze Inbox** to fetch email metadata from Gmail. Results are cached locally in `analysis.db` for 7 days; re-opening the app within that window skips the Gmail fetch.
2. **Review patterns** — senders are grouped by pattern (promotions, newsletters, zero-read-rate, etc.). Each group shows email count, total size, and a safety indicator.
3. **Change preset** — choose **Conservative**, **Balanced**, or **Aggressive** to control how aggressively low-signal emails are recommended for deletion. Preset changes rescore without re-fetching from Gmail.
4. **Allowlist / Denylist** — pin senders to always keep or always recommend deleting via the sender detail panel. Saved to `preferences.json`.
5. **Delete** — select emails or entire groups and click **Delete**. You can move to Trash or permanently delete. Deletions update the local cache immediately.

## Files created at runtime (all gitignored)

| File | Contents |
|------|----------|
| `credentials.json` | Google OAuth client secret |
| `token.json` | Stored OAuth token (auto-refreshed) |
| `preferences.json` | Allowlist, denylist, engaged-sender cache |
| `analysis.db` | SQLite cache of fetched email metadata (7-day TTL) |

## Running tests

```bash
pytest
```
