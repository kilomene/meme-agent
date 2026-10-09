"""Shared daemon plumbing: PID files, lifecycle, config/store/adapter access.

Every daemon is a separate OS process running ``python3 -m daemon.<name>``.
PID files live in ``$FOREX_AGENT_HOME/run/<name>.pid`` — the same files
``forex.get_health`` and ``scripts/forex-daemons`` read.

A daemon never imports another daemon. All cross-area access goes through
the helpers here (which reuse agent/tools/backend seams where they exist).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("forex_agent.daemon")


def get_home() -> str:
    home = os.environ.get("FOREX_AGENT_HOME", "")
    if home:
        return home
    home = os.path.join(os.path.expanduser("~"), ".forex-agent")
    # The home tree holds the secrets file and the local DB: restrict
    # directory listings (mode applies only when the dir is created).
    os.makedirs(home, exist_ok=True, mode=0o700)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return home


def run_dir() -> str:
    path = os.path.join(get_home(), "run")
    os.makedirs(path, exist_ok=True)
    return path


def pid_file(name: str) -> str:
    return os.path.join(run_dir(), name + ".pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(name: str) -> Optional[int]:
    """PID recorded for a daemon, or None if no live process."""
    path = pid_file(name)
    try:
        with open(path) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _await_owner(name: str, timeout: float = 1.0) -> Optional[int]:
    """Poll for a pidfile owner that may still be writing its pid.

    A process that just won the atomic create writes its pid within
    microseconds; an empty/unparseable pidfile is given a short grace
    period before being treated as stale (crashed mid-claim)."""
    end = time.monotonic() + timeout
    while True:
        pid = read_pid(name)
        if pid is not None:
            return pid
        if time.monotonic() >= end:
            return None
        time.sleep(0.02)


def acquire_pidfile(name: str) -> bool:
    """Idempotent PID acquisition. Returns True if THIS process now owns
    the pidfile; False if another live process already does (caller
    should exit quietly). Stale pidfiles are reclaimed.

    The claim itself is atomic (O_CREAT|O_EXCL): N processes racing
    acquire_pidfile() produce exactly one winner, so a duplicate daemon
    can never start even if two supervisors fire at once.
    """
    path = pid_file(name)
    if read_pid(name) is not None:
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Lost the create race, or a stale pidfile appeared between the
        # liveness check and the claim. A fresh winner writes its pid
        # immediately, so poll briefly before concluding the file is
        # stale; a truly stale file is reclaimed and the claim retried.
        if _await_owner(name) is not None:
            return False
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
        fh.flush()
        os.fsync(fd)
    return True


def release_pidfile(name: str) -> None:
    path = pid_file(name)
    try:
        with open(path) as fh:
            if int(fh.read().strip()) != os.getpid():
                return  # not ours; leave it alone
    except (OSError, ValueError):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def load_config():
    from config.config import load_config  # noqa: PLC0415
    return load_config()


# Process-wide shared Store (one sqlite3 connection per daemon process,
# keyed by DB path so test harnesses that repoint DEFAULT_DB_PATH per
# test still get an isolated store). See get_store() below.
_process_store = None
_process_store_path = None


def get_store():
    """Process-wide shared Store (one sqlite3 connection per DB path).

    Daemons used to build a fresh Store on every cycle and never closed
    them, churning hundreds of abandoned connections per process per day.
    The Store is documented thread-safe (single connection + RLock), so
    sharing one per process is the intended use — and it means a
    transaction can never be orphaned on a forgotten connection.
    """
    global _process_store, _process_store_path
    from storage import Store  # noqa: PLC0415
    import storage.store as store_mod  # noqa: PLC0415
    path = str(store_mod.DEFAULT_DB_PATH)
    if _process_store is None or _process_store_path != path:
        if _process_store is not None:
            try:
                _process_store.close()
            except Exception:
                pass
        _process_store = Store()
        _process_store_path = path
    return _process_store


def close_process_store():
    """Close the shared process store (daemon shutdown)."""
    global _process_store, _process_store_path
    store, _process_store = _process_store, None
    _process_store_path = None
    if store is not None:
        try:
            store.close()
        except Exception:
            pass


def get_adapter(config=None):
    """The configured broker adapter (same construction as agent tools)."""
    from agent.tools import backend  # noqa: PLC0415
    return backend.broker_adapter()


def get_daemon_state(store, name: str) -> dict:
    """Last persisted state blob for a daemon (survives restarts)."""
    try:
        rows = store.journal_query(kind="daemon_state", symbol=name, limit=1)
    except Exception:
        return {}
    if not rows:
        return {}
    detail = rows[0].get("detail")
    return dict(detail) if isinstance(detail, dict) else {}


def save_daemon_state(store, name: str, state: dict, keep: int = 10) -> None:
    try:
        store.journal_add({"kind": "daemon_state", "symbol": name,
                           "detail": dict(state)})
        # Readers only ever fetch the newest row per daemon (limit=1), so
        # prune history on every write — otherwise multi-hundred-KB state
        # blobs appended each tick grow the journal without bound and
        # eventually stall the SQLite backup API (2026-09-23: 4.1GB DB).
        store.prune_journal(kind="daemon_state", symbol=name, keep=keep)
    except Exception:
        logger.exception("daemon %s: could not persist state", name)


class Daemon:
    """Base class: acquire pidfile, loop run_once() every interval,
    exit cleanly on SIGTERM/SIGINT. Subclasses implement run_once()."""

    name = "daemon"
    interval = 60.0

    def __init__(self, interval: Optional[float] = None):
        self._stop = False
        if interval is not None:
            self.interval = interval

    def run_once(self) -> None:
        raise NotImplementedError

    def _handle_signal(self, signum, frame):
        logger.warning("daemon %s: received signal %s, stopping", self.name, signum)
        self._stop = True

    def run_forever(self) -> int:
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        if not acquire_pidfile(self.name):
            logger.warning("daemon %s: already running (pid %s), exiting",
                           self.name, read_pid(self.name))
            return 0
        logger.warning("daemon %s: started (pid %d)", self.name, os.getpid())
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        try:
            from agent.events import bus as event_bus  # noqa: PLC0415
            try:
                event_bus.configure(store=get_store())
                event_bus.publish({"event": "daemon.started", "daemon": self.name,
                                   "pid": os.getpid()})
            except Exception:
                logger.exception("daemon %s: could not publish daemon.started",
                                 self.name)
            while not self._stop:
                started = time.monotonic()
                try:
                    self.run_once()
                except Exception:
                    logger.exception("daemon %s: run_once crashed (continuing)",
                                     self.name)
                elapsed = time.monotonic() - started
                wait = self.interval - elapsed
                end = time.monotonic() + max(0.0, wait)
                while not self._stop and time.monotonic() < end:
                    time.sleep(min(1.0, end - time.monotonic()))
        finally:
            release_pidfile(self.name)
            try:
                from agent.events import bus as event_bus  # noqa: PLC0415
                event_bus.configure(store=get_store())
                event_bus.publish({"event": "daemon.stopped", "daemon": self.name,
                                   "reason": "shutdown"})
            except Exception:
                pass
            close_process_store()
            logger.warning("daemon %s: stopped", self.name)
        return 0


def main(daemon_factory: Callable[[], Daemon], argv=None) -> int:
    """Entry point for ``python3 -m daemon.<name>``. ``--once`` runs a
    single cycle (used by tests and the installer smoke test)."""
    daemon = daemon_factory()
    if argv and "--once" in argv:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        daemon.run_once()
        return 0
    return daemon.run_forever()
