"""Tests for store.py — SQLite persistence of analysis runs."""

import importlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import config


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point config.ANALYSIS_DB_FILE at a tempdir DB and reimport store."""
    db = tmp_path / 'test_analysis.db'
    monkeypatch.setattr(config, 'ANALYSIS_DB_FILE', db)
    monkeypatch.setattr(config, 'ANALYSIS_TTL_DAYS', 7)
    import store
    importlib.reload(store)
    store.init()
    yield store


def _mk_email(id_, sender='user@example.com', domain='example.com',
              subject='hi', unread=False, starred=False, important=False,
              unsub=False, age_days=100, size=1024, labels=None):
    return {
        'id': id_,
        'thread_id': 't-' + id_,
        'sender_email': sender,
        'sender_name': 'User',
        'sender_domain': domain,
        'subject': subject,
        'date': datetime.now(timezone.utc) - timedelta(days=age_days),
        'age_days': age_days,
        'size_bytes': size,
        'labels': labels or [],
        'snippet': f'snippet for {id_}',
        'is_unread': unread,
        'is_starred': starred,
        'is_important': important,
        'has_unsubscribe': unsub,
    }


def test_save_and_load_roundtrip(isolated_store):
    store = isolated_store
    emails = [_mk_email(f'm{i}', unread=(i % 2 == 0)) for i in range(5)]
    store.save_run('me@nyt.com', query='in:inbox', preset='balanced', emails=emails)

    meta, loaded = store.load_run('me@nyt.com')
    assert meta is not None
    assert meta['preset'] == 'balanced'
    assert meta['total_emails'] == 5
    assert len(loaded) == 5
    ids = {e['id'] for e in loaded}
    assert ids == {f'm{i}' for i in range(5)}
    # Boolean fields round-tripped correctly
    m0 = next(e for e in loaded if e['id'] == 'm0')
    assert m0['is_unread'] is True
    m1 = next(e for e in loaded if e['id'] == 'm1')
    assert m1['is_unread'] is False
    # Date object present, age_days recomputed
    assert isinstance(m0['date'], datetime)
    assert isinstance(m0['age_days'], int)


def test_save_replaces_previous_run(isolated_store):
    store = isolated_store
    store.save_run('a@b.com', 'q1', 'conservative', [_mk_email('x1')])
    store.save_run('a@b.com', 'q2', 'aggressive', [_mk_email('x2')])
    meta, loaded = store.load_run('a@b.com')
    assert meta['preset'] == 'aggressive'
    assert [e['id'] for e in loaded] == ['x2']


def test_mark_deleted_hides_emails(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email(f'e{i}') for i in range(4)])
    updated = store.mark_deleted('u@x.com', ['e1', 'e3'])
    assert updated == 2
    _, loaded = store.load_run('u@x.com')
    assert {e['id'] for e in loaded} == {'e0', 'e2'}
    # Idempotent: re-marking doesn't error but affects 0 rows
    assert store.mark_deleted('u@x.com', ['e1']) == 0


def test_status_reports_counts(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'conservative',
                   [_mk_email(f'e{i}') for i in range(3)])
    store.mark_deleted('u@x.com', ['e0'])
    s = store.status('u@x.com')
    assert s['active_email_count'] == 2
    assert s['deleted_email_count'] == 1
    assert s['expired'] is False
    assert s['seconds_remaining'] > 0
    assert s['ttl_days'] == 7


def test_clear_removes_everything(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced', [_mk_email('e1')])
    store.clear('u@x.com')
    meta, loaded = store.load_run('u@x.com')
    assert meta is None
    assert loaded == []
    assert store.status('u@x.com') is None


def test_expired_run_is_treated_as_missing(isolated_store, monkeypatch):
    store = isolated_store
    # Force expiry by setting a negative TTL for save.
    monkeypatch.setattr(config, 'ANALYSIS_TTL_DAYS', -1)
    store.save_run('u@x.com', '', 'balanced', [_mk_email('e1')])
    meta, loaded = store.load_run('u@x.com')
    assert meta is None
    assert loaded == []
    # status still returns the row but marks expired=True
    s = store.status('u@x.com')
    assert s is not None
    assert s['expired'] is True


def test_prune_expired_removes_old_runs(isolated_store, monkeypatch):
    store = isolated_store
    monkeypatch.setattr(config, 'ANALYSIS_TTL_DAYS', -1)
    store.save_run('u@x.com', '', 'balanced', [_mk_email('e1')])
    monkeypatch.setattr(config, 'ANALYSIS_TTL_DAYS', 7)
    store.save_run('v@x.com', '', 'balanced', [_mk_email('e2')])
    removed = store.prune_expired()
    assert removed == 1
    # Deleted user is gone, healthy user still present.
    assert store.status('u@x.com') is None
    assert store.status('v@x.com') is not None


def test_touch_preset_updates_only_metadata(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced', [_mk_email('e1')])
    store.touch_preset('u@x.com', 'aggressive')
    meta, _ = store.load_run('u@x.com')
    assert meta['preset'] == 'aggressive'


def test_labels_and_flags_persist(isolated_store):
    store = isolated_store
    emails = [_mk_email('e1',
                        labels=['STARRED', 'IMPORTANT', 'CATEGORY_PROMOTIONS'],
                        starred=True, important=True, unsub=True)]
    store.save_run('u@x.com', '', 'balanced', emails)
    _, loaded = store.load_run('u@x.com')
    e = loaded[0]
    assert e['is_starred'] is True
    assert e['is_important'] is True
    assert e['has_unsubscribe'] is True
    assert 'STARRED' in e['labels']
    assert 'CATEGORY_PROMOTIONS' in e['labels']


# ---------- Incremental sync helpers ----------

def test_history_id_roundtrip(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced', [_mk_email('e1')], history_id='12345')
    meta, _ = store.load_run('u@x.com')
    assert meta['history_id'] == '12345'
    assert meta['last_synced_at']  # ISO string present


def test_upsert_adds_and_replaces(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email('e1', unread=True)], history_id='100')

    # Upsert a new row + a replacement for the existing one.
    replaced = _mk_email('e1', unread=False)  # flip unread flag
    added = _mk_email('e2', unread=True)
    n = store.upsert_emails('u@x.com', [replaced, added])
    assert n == 2

    _, loaded = store.load_run('u@x.com')
    by_id = {e['id']: e for e in loaded}
    assert set(by_id.keys()) == {'e1', 'e2'}
    assert by_id['e1']['is_unread'] is False  # replacement took effect
    assert by_id['e2']['is_unread'] is True


def test_hard_delete_removes_rows(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email(f'e{i}') for i in range(4)], history_id='100')
    n = store.hard_delete_emails('u@x.com', ['e0', 'e2'])
    assert n == 2
    _, loaded = store.load_run('u@x.com')
    assert {e['id'] for e in loaded} == {'e1', 'e3'}
    # Unlike mark_deleted, deleted_email_count should NOT include hard-deletes.
    s = store.status('u@x.com')
    assert s['deleted_email_count'] == 0


def test_hard_delete_and_mark_deleted_are_distinct(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email(f'e{i}') for i in range(3)])
    store.mark_deleted('u@x.com', ['e0'])     # soft
    store.hard_delete_emails('u@x.com', ['e1'])  # hard
    s = store.status('u@x.com')
    assert s['active_email_count'] == 1   # only e2 remains active
    assert s['deleted_email_count'] == 1  # only e0 is soft-deleted


def test_update_history_cursor_advances_and_extends_ttl(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email('e1')], history_id='100')
    meta0, _ = store.load_run('u@x.com')
    original_expiry = meta0['expires_at']

    store.update_history_cursor('u@x.com', '200')
    meta1, _ = store.load_run('u@x.com')
    assert meta1['history_id'] == '200'
    assert meta1['expires_at'] >= original_expiry  # TTL was refreshed


def test_update_history_cursor_can_skip_ttl_extension(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email('e1')], history_id='100')
    meta0, _ = store.load_run('u@x.com')
    original_expiry = meta0['expires_at']

    store.update_history_cursor('u@x.com', '200', extend_ttl=False)
    meta1, _ = store.load_run('u@x.com')
    assert meta1['history_id'] == '200'
    assert meta1['expires_at'] == original_expiry


def test_recount_matches_active_rows(isolated_store):
    store = isolated_store
    store.save_run('u@x.com', '', 'balanced',
                   [_mk_email(f'e{i}', size=1000) for i in range(5)])
    store.mark_deleted('u@x.com', ['e0'])
    store.hard_delete_emails('u@x.com', ['e1'])
    counts = store.recount('u@x.com')
    assert counts['total_emails'] == 3
    assert counts['total_size_bytes'] == 3000
    meta, _ = store.load_run('u@x.com')
    assert meta['total_emails'] == 3
    assert meta['total_size_bytes'] == 3000
