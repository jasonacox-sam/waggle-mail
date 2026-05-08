"""
Tests for the duplicate reply guard — three-state machine (PR #21).

State machine:
  (absent)     → pending:<ts>  : _begin_send_guarded writes pending under lock
  pending:<ts> → sent:<ts>     : _confirm_send_guarded after successful send
  pending:<ts> → (absent)      : _abort_send_guarded after failed send
  pending:<ts> → retry         : _expire_pending after > PENDING_TIMEOUT seconds
  retry        → sent:<ts>     : next _begin_send_guarded call (with disclaimer prefix)
  sent:<ts>    → blocks        : _begin_send_guarded raises RuntimeError (unless force=True)

Covers:
- First send: absent → pending → sent
- Duplicate blocked: sent state raises RuntimeError
- force=True bypasses sent block
- Send failure: pending → absent (retry allowed)
- Pending timeout: pending → retry after expiry
- Retry: allowed but injects disclaimer prefix
- Empty/None Message-ID: guard bypassed
- Corrupted DB: treated as empty, guard bypassed
- Concurrent TOCTOU: two threads race, only one sends
- Lock retry loop: succeeds after transient contention
- Lock exhausted: fails open with warning
- Pruning: sent entries older than 30 days removed; pending/retry kept
"""

import json
import datetime
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _seed_state(waggle_mod, message_id, state):
    """Test helper: directly write a state to the DB (bypasses lock)."""
    db = waggle_mod._load_reply_db()
    db[message_id.strip()] = state
    waggle_mod._save_reply_db(db)


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def fake_send(monkeypatch):
    """Replace send_email with a no-op that records calls."""
    import waggle
    sent = []
    monkeypatch.setattr(waggle, 'send_email', lambda **kw: sent.append(kw))
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})
    return sent


def make_msg(mid):
    return {
        'message_id': mid,
        'from_addr': 'jason@example.com',
        'reply_subject': 'Re: Test',
        'reply_references': '',
        'reply_cc': '',
    }


# ---------------------------------------------------------------------------
# Core state transitions
# ---------------------------------------------------------------------------

def test_first_send_absent_to_sent(tmp_db, fake_send):
    """Happy path: absent → pending → sent."""
    import waggle
    mid = '<test-001@example.com>'
    waggle.reply_all(make_msg(mid), body_md='Hello')
    db = json.loads(tmp_db.read_text())
    assert db[mid].startswith('sent:')
    assert len(fake_send) == 1


def test_duplicate_blocked_after_sent(tmp_db, fake_send):
    """sent state raises RuntimeError on second call."""
    import waggle
    mid = '<test-002@example.com>'
    waggle.reply_all(make_msg(mid), body_md='First')
    with pytest.raises(RuntimeError, match='Already replied'):
        waggle.reply_all(make_msg(mid), body_md='Duplicate')
    assert len(fake_send) == 1  # only one send


def test_force_true_bypasses_sent(tmp_db, fake_send):
    """force=True sends even when state is sent."""
    import waggle
    mid = '<test-003@example.com>'
    waggle.reply_all(make_msg(mid), body_md='First')
    waggle.reply_all(make_msg(mid), body_md='Intentional follow-up', force=True)
    assert len(fake_send) == 2


def test_send_failure_clears_pending(tmp_db, monkeypatch):
    """Send failure: pending is cleared so retry is allowed."""
    import waggle
    import smtplib
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})
    monkeypatch.setattr(waggle, 'send_email', lambda **kw: (_ for _ in ()).throw(smtplib.SMTPException('fail')))

    mid = '<test-004@example.com>'
    with pytest.raises(Exception):
        waggle.reply_all(make_msg(mid), body_md='Will fail')

    db = waggle._load_reply_db()
    assert mid not in db  # pending cleared, retry allowed


def test_different_message_ids_independent(tmp_db, fake_send):
    """Replying to one Message-ID doesn't block another."""
    import waggle
    waggle.reply_all(make_msg('<msg-A@example.com>'), body_md='A')
    waggle.reply_all(make_msg('<msg-B@example.com>'), body_md='B')
    assert len(fake_send) == 2


# ---------------------------------------------------------------------------
# Pending timeout → retry state
# ---------------------------------------------------------------------------

def test_pending_expires_to_retry(tmp_db):
    """Pending entry older than PENDING_TIMEOUT becomes 'retry'."""
    import waggle
    mid = '<test-pending@example.com>'
    # Seed a pending entry 2 hours old
    old_ts = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat()
    _seed_state(waggle, mid, f'pending:{old_ts}')

    db = waggle._load_reply_db()
    waggle._expire_pending(db)

    assert db[mid] == 'retry'


def test_pending_not_expired_yet(tmp_db):
    """Pending entry within timeout is NOT promoted to retry."""
    import waggle
    mid = '<test-pending-fresh@example.com>'
    fresh_ts = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat()
    _seed_state(waggle, mid, f'pending:{fresh_ts}')

    db = waggle._load_reply_db()
    waggle._expire_pending(db)

    assert db[mid].startswith('pending:')


def test_retry_state_allows_send_with_prefix(tmp_db, fake_send):
    """retry state allows sending and injects disclaimer prefix."""
    import waggle
    mid = '<test-retry@example.com>'
    _seed_state(waggle, mid, 'retry')

    waggle.reply_all(make_msg(mid), body_md='Retry body')

    assert len(fake_send) == 1
    sent_body = fake_send[0]['body_md']
    assert '⚠️' in sent_body
    assert 'duplicate' in sent_body.lower()
    assert 'Retry body' in sent_body

    db = json.loads(tmp_db.read_text())
    assert db[mid].startswith('sent:')


def test_pending_blocked_unless_force(tmp_db, fake_send):
    """Active pending entry blocks concurrent sender."""
    import waggle
    mid = '<test-in-progress@example.com>'
    fresh_ts = datetime.datetime.now().isoformat()
    _seed_state(waggle, mid, f'pending:{fresh_ts}')

    with pytest.raises(RuntimeError, match='in progress'):
        waggle.reply_all(make_msg(mid), body_md='Should block')
    assert len(fake_send) == 0


# ---------------------------------------------------------------------------
# Empty / None / missing Message-ID
# ---------------------------------------------------------------------------

def test_empty_message_id_bypasses_guard(tmp_db, fake_send):
    """Empty string Message-ID bypasses guard (sends normally)."""
    import waggle
    msg = make_msg('')
    waggle.reply_all(msg, body_md='No message id')
    assert len(fake_send) == 1


def test_none_message_id_bypasses_guard(tmp_db, fake_send):
    """None Message-ID bypasses guard."""
    import waggle
    msg = make_msg(None)
    msg['message_id'] = None
    waggle.reply_all(msg, body_md='No message id')
    assert len(fake_send) == 1


# ---------------------------------------------------------------------------
# Corrupted DB
# ---------------------------------------------------------------------------

def test_corrupted_db_allows_send(tmp_db, fake_send):
    """Corrupted JSON file bypasses guard (fail-open)."""
    import waggle
    tmp_db.write_text('{ this is broken json')
    waggle.reply_all(make_msg('<corrupt-test@example.com>'), body_md='Should work')
    assert len(fake_send) == 1


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_pruning_removes_old_sent(tmp_db):
    """sent entries older than 30 days are pruned."""
    import waggle
    old_ts = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat()
    db = {'<old@example.com>': f'sent:{old_ts}', '<new@example.com>': f'sent:{datetime.datetime.now().isoformat()}'}
    tmp_db.write_text(json.dumps(db))
    waggle._save_reply_db(waggle._load_reply_db())  # trigger prune
    saved = json.loads(tmp_db.read_text())
    assert '<old@example.com>' not in saved
    assert '<new@example.com>' in saved


def test_pruning_keeps_pending_and_retry(tmp_db):
    """pending and retry entries are NOT pruned (they're transient, need manual resolution)."""
    import waggle
    old_ts = (datetime.datetime.now() - datetime.timedelta(days=31)).isoformat()
    db = {
        '<pend@example.com>': f'pending:{old_ts}',
        '<retry@example.com>': 'retry',
    }
    tmp_db.write_text(json.dumps(db))
    waggle._save_reply_db(waggle._load_reply_db())
    saved = json.loads(tmp_db.read_text())
    assert '<pend@example.com>' in saved
    assert '<retry@example.com>' in saved


# ---------------------------------------------------------------------------
# TOCTOU: two threads, only one sends
# ---------------------------------------------------------------------------

def test_toctou_only_one_reply_sent(tmp_db, monkeypatch):
    """Two threads race on same Message-ID — exactly one sends, other raises."""
    import waggle, time

    sent = []
    errors = []

    def slow_send(**kw):
        time.sleep(0.05)  # simulate SMTP latency
        sent.append(kw)

    monkeypatch.setattr(waggle, 'send_email', slow_send)
    monkeypatch.setattr(waggle, '_build_cfg', lambda c=None: {'from_addr': 'sam@example.com'})

    mid = '<toctou@example.com>'
    msg = make_msg(mid)

    def try_reply():
        try:
            waggle.reply_all(msg, body_md='Race')
        except RuntimeError as e:
            errors.append(str(e))

    t1 = threading.Thread(target=try_reply)
    t2 = threading.Thread(target=try_reply)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(sent) == 1, f'Expected 1 send, got {len(sent)}'
    assert len(errors) == 1, f'Expected 1 error, got {errors}'
    assert 'Already replied' in errors[0] or 'in progress' in errors[0]


# ---------------------------------------------------------------------------
# Lock retry loop
# ---------------------------------------------------------------------------

def test_retry_loop_eventually_acquires(tmp_db):
    """Lock retry loop succeeds after transient contention."""
    import waggle, fcntl as fcntl_mod
    call_count = [0]
    original_flock = fcntl_mod.flock

    def flaky_flock(fd, op):
        if op & fcntl_mod.LOCK_NB:
            call_count[0] += 1
            if call_count[0] < 3:
                raise BlockingIOError('simulated contention')
        return original_flock(fd, op)

    import unittest.mock as mock
    with mock.patch('fcntl.flock', side_effect=flaky_flock):
        lock_fh = waggle._acquire_reply_lock()

    assert lock_fh is not None
    waggle._release_lock(lock_fh)
    assert call_count[0] >= 3


def test_lock_exhausted_returns_none(tmp_db, monkeypatch, caplog):
    """When lock cannot be acquired, returns None and logs warning."""
    import waggle, fcntl as fcntl_mod, logging

    def always_locked(fd, op):
        if op & fcntl_mod.LOCK_NB:
            raise BlockingIOError('always locked')

    monkeypatch.setattr(fcntl_mod, 'flock', always_locked)

    with caplog.at_level(logging.WARNING, logger='waggle.reply_guard'):
        lock_fh = waggle._acquire_reply_lock(retries=3, retry_ms=1)

    assert lock_fh is None
    assert any('still locked' in r.message for r in caplog.records)
