"""Tests for storage/store.py lock-robustness.

Regression coverage for the 2026-09-23 incident where signal_monitor
crash-looped with ``sqlite3.OperationalError: database is locked``:
every Store() ran full schema DDL (a write) on each daemon tick, so the
hourly backup's long lock hold killed daemon ticks.

Covers:
  * schema applied once and version-stamped (PRAGMA user_version)
  * steady-state construction skips DDL (no write lock taken)
  * schema apply retries on "database is locked", then succeeds
  * non-lock OperationalErrors are NOT retried (fail fast)
  * persistent lock -> gives up (bounded, raises)
  * end-to-end: a backup-style SHARED lock no longer breaks Store()
  * connection busy timeout is the raised BUSY_TIMEOUT_S
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import store as store_mod  # noqa: E402
from storage.store import Store, SCHEMA_VERSION, BUSY_TIMEOUT_S  # noqa: E402


def _tmp_db():
    fd, path = tempfile.mkstemp(prefix="store-test-", suffix=".db")
    os.close(fd)
    os.unlink(path)  # Store creates it fresh
    return path


def _rm(path):
    if os.path.exists(path):
        os.unlink(path)


def _flaky_factory(calls, failures, exc):
    """Connection factory whose executescript fails `failures` times."""
    real_connect = sqlite3.connect

    class FlakyConn(sqlite3.Connection):
        def executescript(self, script):
            calls.append(script)
            if len(calls) <= failures:
                raise exc
            return super().executescript(script)

    def fake_connect(*args, **kwargs):
        kwargs["factory"] = FlakyConn
        return real_connect(*args, **kwargs)

    return fake_connect


def _hold_shared_lock(path):
    """Simulate the hourly backup: hold a SHARED lock on the DB."""
    locker = sqlite3.connect(path, check_same_thread=False)
    locker.execute("BEGIN")
    locker.execute("SELECT * FROM risk_state").fetchall()
    return locker


class SchemaVersionTests(unittest.TestCase):
    def test_schema_applied_and_version_stamped(self):
        path = _tmp_db()
        try:
            s = Store(path)
            try:
                ver = s._conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(ver, SCHEMA_VERSION)
                tables = {r[0] for r in
                          s._conn.execute("SELECT name FROM sqlite_master "
                                          "WHERE type='table'").fetchall()}
                for expected in ("risk_state", "kill_switch", "event_queue",
                                 "audit_log"):
                    self.assertIn(expected, tables)
            finally:
                s.close()
        finally:
            _rm(path)

    def test_steady_state_construction_skips_ddl(self):
        path = _tmp_db()
        try:
            s1 = Store(path)
            s1.close()
            calls = []
            fake = _flaky_factory(
                calls, 0, sqlite3.OperationalError("database is locked"))
            with mock.patch("sqlite3.connect", side_effect=fake):
                s2 = Store(path)
                try:
                    # version already current -> DDL never re-ran
                    self.assertEqual(calls, [])
                    s2.set_risk_state({"k": "v"})
                    self.assertEqual(s2.get_risk_state(), {"k": "v"})
                finally:
                    s2.close()
        finally:
            _rm(path)

    def test_busy_timeout_configured(self):
        path = _tmp_db()
        try:
            s = Store(path)
            try:
                ms = s._conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(ms, int(BUSY_TIMEOUT_S * 1000))
            finally:
                s.close()
        finally:
            _rm(path)


class SchemaRetryTests(unittest.TestCase):
    def test_apply_retries_on_locked_then_succeeds(self):
        path = _tmp_db()
        try:
            calls = []
            fake = _flaky_factory(
                calls, 2, sqlite3.OperationalError("database is locked"))
            with mock.patch("sqlite3.connect", side_effect=fake), \
                 mock.patch("time.sleep") as sleep_mock:
                s = Store(path)
                try:
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(sleep_mock.call_count, 2)
                    ver = s._conn.execute("PRAGMA user_version").fetchone()[0]
                    self.assertEqual(ver, SCHEMA_VERSION)
                finally:
                    s.close()
        finally:
            _rm(path)

    def test_apply_does_not_retry_other_errors(self):
        path = _tmp_db()
        try:
            calls = []
            fake = _flaky_factory(
                calls, 99, sqlite3.OperationalError("no such table: nope"))
            with mock.patch("sqlite3.connect", side_effect=fake), \
                 mock.patch("time.sleep") as sleep_mock:
                with self.assertRaises(sqlite3.OperationalError):
                    Store(path)
            self.assertEqual(len(calls), 1)   # failed fast, no retry
            self.assertEqual(sleep_mock.call_count, 0)
        finally:
            _rm(path)

    def test_apply_gives_up_after_retries(self):
        path = _tmp_db()
        try:
            calls = []
            fake = _flaky_factory(
                calls, 999, sqlite3.OperationalError("database is locked"))
            with mock.patch("sqlite3.connect", side_effect=fake), \
                 mock.patch("time.sleep"):
                with self.assertRaises(sqlite3.OperationalError):
                    Store(path)
            self.assertEqual(len(calls), store_mod._SCHEMA_APPLY_RETRIES)
        finally:
            _rm(path)


class RealLockTests(unittest.TestCase):
    """End-to-end with a real backup-style SHARED lock held."""

    def test_shared_lock_no_longer_breaks_store(self):
        # Exact incident scenario: hourly backup holds the DB while a
        # daemon tick constructs a Store. Construction + reads must not
        # raise; the write goes through once the transient lock releases.
        path = _tmp_db()
        try:
            s1 = Store(path)
            s1.set_risk_state({"a": 1})
            s1.close()
            locker = _hold_shared_lock(path)
            try:
                s2 = Store(path)  # used to run DDL here -> crash
                try:
                    self.assertEqual(s2.get_risk_state(), {"a": 1})
                finally:
                    s2.close()
            finally:
                locker.commit()
                locker.close()
            s3 = Store(path)
            try:
                s3.journal_add({"kind": "t", "msg": "x"})
            finally:
                s3.close()
        finally:
            _rm(path)

    def test_write_waits_out_brief_lock(self):
        # A lock held only briefly (the new cooperative backup's worst
        # case) must not fail the write: the busy timeout rides it out.
        path = _tmp_db()
        try:
            s1 = Store(path)
            s1.close()
            locker = _hold_shared_lock(path)
            releaser = threading.Timer(
                0.5, lambda: (locker.commit(), locker.close()))
            releaser.start()
            try:
                s2 = Store(path)
                try:
                    s2.journal_add({"kind": "t", "msg": "brief-lock"})
                    rows = s2.journal_query(kind="t", limit=5)
                    self.assertTrue(any(r.get("msg") == "brief-lock"
                                        for r in rows))
                finally:
                    s2.close()
            finally:
                releaser.join()
        finally:
            _rm(path)

    def test_shared_lock_blocks_only_first_time_schema_apply(self):
        # Fresh DB + lock held: DDL must wait/retry, not crash instantly.
        # Use a tiny busy timeout + no-op sleep so the test stays fast;
        # the lock is released before retries run out.
        path = _tmp_db()
        try:
            locker = sqlite3.connect(path)
            locker.execute("CREATE TABLE risk_state (key TEXT PRIMARY KEY, "
                           "value TEXT NOT NULL)")
            locker.execute("BEGIN")
            locker.execute("SELECT * FROM risk_state").fetchall()
            try:
                with mock.patch.object(store_mod, "BUSY_TIMEOUT_S", 0.05), \
                     mock.patch("time.sleep"):
                    # lock released on 3rd attempt -> apply succeeds
                    orig_executescript = sqlite3.Connection.executescript
                    attempts = []

                    release_at = 3

                    class ReleasingConn(sqlite3.Connection):
                        def executescript(self, script):
                            attempts.append(1)
                            if len(attempts) == release_at:
                                locker.commit()  # release the SHARED lock
                            return orig_executescript(self, script)

                    real_connect = sqlite3.connect

                    def fake_connect(*a, **k):
                        k["factory"] = ReleasingConn
                        return real_connect(*a, **k)

                    with mock.patch("sqlite3.connect", side_effect=fake_connect):
                        s = Store(path)
                        try:
                            ver = s._conn.execute(
                                "PRAGMA user_version").fetchone()[0]
                            self.assertEqual(ver, SCHEMA_VERSION)
                        finally:
                            s.close()
                    self.assertEqual(len(attempts), release_at)
            finally:
                try:
                    locker.commit()
                except Exception:
                    pass
                locker.close()
        finally:
            _rm(path)


class WriteGuardTests(unittest.TestCase):
    """Regression for the 2026-09-29 wedge incidents.

    A commit that fails with 'database is locked' (e.g. during the hourly
    backup's multi-minute SHARED lock) used to leave the Store's connection
    holding an open write transaction indefinitely. That single leak
    blocked every other writer; their commits then failed too and leaked
    as well, wedging all daemons' writes until a manual kill -9 — four
    times in one day. The _txn guard must roll back on ANY failure so a
    failed commit can never leave a transaction open.
    """

    def _locked_setup(self):
        """Fresh DB + a second connection holding a SHARED lock (backup)."""
        path = _tmp_db()
        self.addCleanup(_rm, path)
        setup = Store(path)
        setup.close()
        locker = sqlite3.connect(path, timeout=5)
        self.addCleanup(locker.close)
        locker.execute("BEGIN")
        locker.execute("SELECT COUNT(*) FROM journal").fetchone()
        return path, locker

    def _fast_store(self, path):
        s = Store(path)
        self.addCleanup(s.close)
        s._conn.execute("PRAGMA busy_timeout=200")
        return s

    def test_failed_commit_leaves_no_open_transaction(self):
        # INSERT takes RESERVED (compatible with the locker's SHARED);
        # the commit then needs EXCLUSIVE and fails for real.
        path, _locker = self._locked_setup()
        s = self._fast_store(path)
        with self.assertRaises(sqlite3.OperationalError):
            s.journal_add({"kind": "x"})
        # THE regression assertion: no leaked open transaction.
        self.assertFalse(s._conn.in_transaction)

    def test_failed_commit_is_usable_after_lock_clears(self):
        path, locker = self._locked_setup()
        s = self._fast_store(path)
        with self.assertRaises(sqlite3.OperationalError):
            s.journal_add({"kind": "x"})
        locker.rollback()  # backup finishes
        s.journal_add({"kind": "y"})  # must work immediately
        self.assertEqual(len(s.journal_query(kind="y")), 1)

    def test_failed_multi_write_rolls_back_without_data_loss(self):
        path = _tmp_db()
        self.addCleanup(_rm, path)
        seed = Store(path)
        self.addCleanup(seed.close)
        seed.enqueue_event({"n": 1})
        seed.enqueue_event({"n": 2})
        locker = sqlite3.connect(path, timeout=5)
        self.addCleanup(locker.close)
        locker.execute("BEGIN")
        locker.execute("SELECT COUNT(*) FROM journal").fetchone()
        s = self._fast_store(path)
        # dequeue_events = SELECT + DELETE + commit; the commit fails,
        # so the DELETE must be rolled back and no rows lost.
        with self.assertRaises(sqlite3.OperationalError):
            s.dequeue_events(limit=10)
        self.assertFalse(s._conn.in_transaction)
        locker.rollback()  # lock clears; the rolled-back rows must reappear
        rows = s.dequeue_events(limit=10)
        self.assertEqual(len(rows), 2)


class JournalPruneTests(unittest.TestCase):
    """Regression for the 2026-09-23 incident where daemon_state blobs
    (multi-hundred-KB, written every daemon tick, read back only as the
    newest row per daemon) grew the journal to 4.1GB and stalled the
    hourly SQLite backup. prune_journal keeps only the newest rows."""

    def _store(self):
        path = _tmp_db()
        self.addCleanup(_rm, path)
        s = Store(path)
        self.addCleanup(s.close)
        return s

    def test_prune_keeps_newest_n_per_symbol(self):
        s = self._store()
        for i in range(25):
            s.journal_add({"kind": "daemon_state", "symbol": "d1",
                           "detail": {"tick": i}})
        for i in range(5):
            s.journal_add({"kind": "daemon_state", "symbol": "d2",
                           "detail": {"tick": i}})
        deleted = s.prune_journal(kind="daemon_state", symbol="d1", keep=10)
        self.assertEqual(deleted, 15)
        rows = s.journal_query(kind="daemon_state", symbol="d1", limit=100)
        self.assertEqual(len(rows), 10)
        # newest survive, oldest gone
        self.assertEqual(rows[0]["detail"]["tick"], 24)
        self.assertEqual(rows[-1]["detail"]["tick"], 15)
        # other daemon untouched
        self.assertEqual(
            len(s.journal_query(kind="daemon_state", symbol="d2",
                                limit=100)), 5)

    def test_prune_without_symbol_prunes_whole_kind(self):
        s = self._store()
        for i in range(8):
            s.journal_add({"kind": "daemon_state", "symbol": "d%d" % i,
                           "detail": {"tick": i}})
        s.journal_add({"kind": "trade", "symbol": "X",
                       "detail": {"side": "buy"}})
        deleted = s.prune_journal(kind="daemon_state", keep=3)
        self.assertEqual(deleted, 5)
        self.assertEqual(
            len(s.journal_query(kind="daemon_state", limit=100)), 3)
        # other kinds never touched
        self.assertEqual(len(s.journal_query(kind="trade", limit=100)), 1)

    def test_prune_requires_kind(self):
        s = self._store()
        with self.assertRaises(ValueError):
            s.prune_journal(kind="")


if __name__ == "__main__":
    unittest.main()
