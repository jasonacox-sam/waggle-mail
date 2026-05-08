"""
Tests for the duplicate reply guard (PR #21).

Covers:
- First send is allowed and logged
- Second send to same Message-ID is blocked
- force=True overrides the block
- Send failure does NOT log the Message-ID (so retries are allowed)
- Empty/None Message-ID bypasses guard (doesn't block or crash)
- Pruning removes entries older than 30 days
- Corrupted DB returns empty dict (logs warning, doesn't crash)
- Concurrent callers: file lock prevents race condition
"""

import json
import datetime
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers — patch the DB path to a temp dir for each test
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect the reply DB to a temp directory."""
    db_path = tmp_path / 'waggle-replied.json'
    lock_path = tmp_path / 'waggle-replied.lock'
    import waggle
    monkeypatch.setattr(waggle, '_REPLY_DB_PATH', db_path)
    monkeypatch.setattr(waggle, '_REPLY_DB_LOCK_PATH', lock_path)
    return db_path


# ---------------------------------------------------------------------------
# Core guard behavior
# ---------------------------------------------------------------------------

def test_first_reply_allowed(tmp_db):
    """check_already_replied returns False for a new Message-ID."""
    import waggle
    already, when = waggle.check_already_replied('<test-001@example.com>')
    assert already is False
    assert when is None


def test_mark_then_check_blocked(tmp_db):
    """After _mark_replied, check_already_replied returns True."""
    import waggle
    mid = '<test-002@example.com>'
    waggle._mark_replied(mid)
    already, when = waggle.check_already_replied(mid)
    assert already is True
    assert when is not None


def test_different_message_ids_independent(tmp_db):
    """Replying to one Message-ID doesn't block another."""
    import waggle
    waggle._mark_replied('<msg-A@example.com>')
    already, _ = waggle.check_already_replied('<msg-B@example.com>')
    assert already is False


# ---------------------------------------------------------------------------
# Empty / missing Message-ID
# ---------------------------------------------------------------------------

def test_empty_message_id_bypasses_guard(tmp_db):
    """Empty string Message-ID doesn't block (or crash)."""
    import waggle
    already, when = waggle.check_already_replied('')
    assert already is False
    assert when is None


def test_none_message_id_bypasses_guard(tmp_db):
    """None Message-ID doesn't block (or crash)."""
    import waggle
    already, when = waggle.check_already_replied(None)
    assert already is False


def test_mark_replied_empty_is_noop(tmp_db):
    """_mark_replied('') doesn't write anything or crash."""
    import waggle
    waggle._mark_replied('')
    assert not tmp_db.exists() or json.loads(tmp_db.read_text()) == {}


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_pruning_removes_old_entries(tmp_db):
    """Entries older than 30 days are pruned on save."""
    import waggle
    old_ts = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat()
    new_ts = datetime.datetime.now().isoformat()
    db = {'<old@example.com>': old_ts, '<new@example.com>': new_ts}
    tmp_db.write_text(json.dumps(db))

    # Trigger a save (via _mark_replied on a new message)
    waggle._mark_replied('<trigger@example.com>')

    saved = json.loads(tmp_db.read_text())
    assert '<old@example.com>' not in saved
    assert '<new@example.com>' in saved
    assert '<trigger@example.com>' in saved


def test_pruning_keeps_recent_entries(tmp_db):
    """Entries within 30 days are kept."""
    import waggle
    mid = '<recent@example.com>'
    waggle._mark_replied(mid)
    waggle._mark_replied('<another@example.com>')  # trigger another save
    saved = json.loads(tmp_db.read_text())
    assert mid in saved


# ---------------------------------------------------------------------------
# Corrupted DB
# ---------------------------------------------------------------------------

def test_corrupted_db_returns_empty(tmp_db):
    """Corrupted JSON file returns empty dict (no crash)."""
    import waggle
    tmp_db.write_text('{ this is not valid json !!!')
    db = waggle._load_reply_db()
    assert db == {}


def test_corrupted_db_allows_send(tmp_db):
    """Corrupted DB means guard is bypassed — doesn't block sends."""
    import waggle
    tmp_db.write_text('{ corrupted')
    already, _ = waggle.check_already_replied('<test@example.com>')
    assert already is False


# ---------------------------------------------------------------------------
# force=True override
# ---------------------------------------------------------------------------

def test_force_true_bypasses_guard(tmp_db, monkeypatch):
    """reply_all with force=True sends even when already replied."""
    import waggle
    mid = '<force-test@example.com>'
    waggle._mark_replied(mid)

    # Confirm it's blocked normally
    already, _ = waggle.check_already_replied(mid)
    assert already is True

    # Simulate reply_all with force=True — should NOT raise
    sent = []
    monkeypatch.setattr(waggle, 'send_email', lambda **kw: sent.append(kw))
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})

    msg = {
        'message_id': mid,
        'from_addr': 'jason@example.com',
        'reply_subject': 'Re: Test',
        'reply_references': '',
        'reply_cc': '',
    }
    waggle.reply_all(msg, body_md='Forced reply', force=True)
    assert len(sent) == 1


def test_force_false_raises_on_duplicate(tmp_db, monkeypatch):
    """reply_all without force=True raises RuntimeError on duplicate."""
    import waggle
    mid = '<dupe-test@example.com>'
    waggle._mark_replied(mid)

    monkeypatch.setattr(waggle, 'send_email', lambda **kw: None)
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})

    msg = {
        'message_id': mid,
        'from_addr': 'jason@example.com',
        'reply_subject': 'Re: Test',
        'reply_references': '',
        'reply_cc': '',
    }
    with pytest.raises(RuntimeError, match='Already replied'):
        waggle.reply_all(msg, body_md='Should be blocked')


# ---------------------------------------------------------------------------
# Send failure semantics
# ---------------------------------------------------------------------------

def test_send_failure_does_not_mark_replied(tmp_db, monkeypatch):
    """If send_email raises, Message-ID is NOT logged (retry should work)."""
    import waggle
    mid = '<fail-test@example.com>'

    def boom(**kw):
        raise smtplib.SMTPException("Connection refused")

    import smtplib
    monkeypatch.setattr(waggle, 'send_email', boom)
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})

    msg = {
        'message_id': mid,
        'from_addr': 'jason@example.com',
        'reply_subject': 'Re: Test',
        'reply_references': '',
        'reply_cc': '',
    }
    with pytest.raises(Exception):
        waggle.reply_all(msg, body_md='Should fail')

    # Message-ID should NOT be in DB
    already, _ = waggle.check_already_replied(mid)
    assert already is False


# ---------------------------------------------------------------------------
# Concurrency: retry loop + file lock
# ---------------------------------------------------------------------------

def test_concurrent_mark_replied_no_data_loss(tmp_db):
    """Multiple threads marking different Message-IDs don't corrupt the DB."""
    import waggle

    mids = [f'<concurrent-{i}@example.com>' for i in range(20)]
    threads = [threading.Thread(target=waggle._mark_replied, args=(mid,)) for mid in mids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db = json.loads(tmp_db.read_text())
    for mid in mids:
        assert mid in db, f'{mid} missing from DB after concurrent writes'


def test_retry_loop_eventually_acquires(tmp_db, monkeypatch):
    """Retry loop succeeds after a few BlockingIOError attempts."""
    import waggle
    import fcntl as fcntl_mod

    call_count = [0]
    original_flock = fcntl_mod.flock

    def flaky_flock(fd, op):
        # Fail non-blocking attempts twice, then succeed
        if op & fcntl_mod.LOCK_NB:
            call_count[0] += 1
            if call_count[0] < 3:
                raise BlockingIOError('simulated lock contention')
        return original_flock(fd, op)

    monkeypatch.setattr(fcntl_mod, 'flock', flaky_flock)
    mid = '<retry-test@example.com>'
    waggle._mark_replied(mid)  # should succeed on 3rd attempt
    already, _ = waggle.check_already_replied(mid)
    assert already is True
    assert call_count[0] >= 3


def test_retry_exhausted_logs_warning_and_continues(tmp_db, monkeypatch, caplog):
    """After all retries fail, logs warning and does NOT raise."""
    import waggle
    import fcntl as fcntl_mod
    import logging

    def always_locked(fd, op):
        if op & fcntl_mod.LOCK_NB:
            raise BlockingIOError('always locked')
        return fcntl_mod.flock(fd, op)  # allow LOCK_UN

    monkeypatch.setattr(fcntl_mod, 'flock', always_locked)
    mid = '<exhausted-retry@example.com>'

    with caplog.at_level(logging.WARNING, logger='waggle.reply_guard'):
        waggle._mark_replied(mid, _retries=3, _retry_ms=1)  # fast for tests

    assert any('still locked' in r.message for r in caplog.records)
    # Should NOT be in DB since all retries failed
    already, _ = waggle.check_already_replied(mid)
    assert already is False
