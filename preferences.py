"""User preferences store — allowlist, denylist, and sent-recipients cache.

Persisted to preferences.json next to token.json. Reads are cheap and cached; writes
go through an atomic replace (tmp file + os.replace) under a single-writer thread lock.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import config


_LOCK = threading.Lock()


_DEFAULT_STATE = {
    'allowlist': [],   # [{'type': 'sender_email'|'sender_domain', 'value': str, 'added_at': iso}]
    'denylist': [],
    'decisions': [],   # reserved for future adaptive learning (v2)
    'sent_cache': {    # engaged-senders index
        'recipients': [],
        'updated_at': None,
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return Path(config.PREFERENCES_FILE)


def load() -> dict:
    """Load preferences from disk. Missing file returns default state."""
    p = _path()
    if not p.exists():
        return json.loads(json.dumps(_DEFAULT_STATE))  # fresh copy
    try:
        with p.open('r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Preferences] Failed to load {p}: {e}; using defaults", flush=True)
        return json.loads(json.dumps(_DEFAULT_STATE))

    # Normalize: ensure all top-level keys exist
    for key, default in _DEFAULT_STATE.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    return data


def _atomic_write(state: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.preferences-', suffix='.json', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(state: dict) -> None:
    """Persist the given state atomically."""
    with _LOCK:
        _atomic_write(state)


def _normalize_entry(entry: dict) -> dict:
    return {
        'type': entry['type'],
        'value': (entry.get('value') or '').strip().lower(),
        'added_at': entry.get('added_at') or _now_iso(),
    }


def _add_to_list(list_name: str, entry_type: str, value: str) -> dict:
    """Add an entry to allowlist/denylist. Idempotent."""
    if entry_type not in ('sender_email', 'sender_domain'):
        raise ValueError(f"Unsupported entry type: {entry_type}")
    value = (value or '').strip().lower()
    if not value:
        raise ValueError("value is required")
    with _LOCK:
        state = load()
        existing = state.get(list_name, [])
        for e in existing:
            if e.get('type') == entry_type and (e.get('value') or '').lower() == value:
                return state  # already present
        existing.append(_normalize_entry({'type': entry_type, 'value': value}))
        state[list_name] = existing
        _atomic_write(state)
        return state


def _remove_from_list(list_name: str, entry_type: str, value: str) -> dict:
    value = (value or '').strip().lower()
    with _LOCK:
        state = load()
        state[list_name] = [
            e for e in state.get(list_name, [])
            if not (e.get('type') == entry_type and (e.get('value') or '').lower() == value)
        ]
        _atomic_write(state)
        return state


def add_to_allowlist(entry_type: str, value: str) -> dict:
    return _add_to_list('allowlist', entry_type, value)


def remove_from_allowlist(entry_type: str, value: str) -> dict:
    return _remove_from_list('allowlist', entry_type, value)


def add_to_denylist(entry_type: str, value: str) -> dict:
    return _add_to_list('denylist', entry_type, value)


def remove_from_denylist(entry_type: str, value: str) -> dict:
    return _remove_from_list('denylist', entry_type, value)


# --- Sent-recipients cache ---

def get_cached_sent_recipients() -> set:
    """Return the cached set of recipient emails you've written to, or empty set if
    missing/stale. Freshness threshold from config.SENT_CACHE_TTL_HOURS."""
    state = load()
    cache = state.get('sent_cache') or {}
    updated_at = cache.get('updated_at')
    recipients = cache.get('recipients') or []
    if not updated_at:
        return set()
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return set()
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    if age_hours > config.SENT_CACHE_TTL_HOURS:
        return set()
    return {r.lower() for r in recipients if r}


def update_sent_recipients(recipients: Iterable[str]) -> None:
    """Overwrite the sent-recipients cache with a fresh set."""
    normalized = sorted({(r or '').strip().lower() for r in recipients if r})
    with _LOCK:
        state = load()
        state['sent_cache'] = {
            'recipients': normalized,
            'updated_at': _now_iso(),
        }
        _atomic_write(state)


def is_sent_cache_fresh() -> bool:
    """Cheap check whether the cache is still within TTL."""
    state = load()
    cache = state.get('sent_cache') or {}
    updated_at = cache.get('updated_at')
    if not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return age_hours <= config.SENT_CACHE_TTL_HOURS
