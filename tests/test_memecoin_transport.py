"""Unit tests for the curl-first HTTP path in broker/memecoin/transport.py.

_http_get_json prefers curl (reliable through this VM's MITM egress
proxy) and falls back to stdlib urllib. All network interaction is
mocked — these tests never touch the network.
"""

import io
import json
import subprocess
import urllib.error

import pytest

from broker.memecoin import transport as tmod


def _curl_result(returncode=0, stdout=b'{"ok": true}', stderr=b""):
    return subprocess.CompletedProcess(args=["curl"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_curl_success_returns_json(monkeypatch):
    """curl available + exit 0 -> parsed JSON, urllib never called."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(tmod.subprocess, "run",
                        lambda *a, **k: _curl_result())
    def _boom(*a, **k):
        raise AssertionError("urllib must not be used when curl succeeds")
    monkeypatch.setattr(tmod.urllib.request, "urlopen", _boom)
    assert tmod._http_get_json("https://example.invalid/x") == {"ok": True}


def test_curl_failure_falls_back_to_urllib(monkeypatch):
    """curl exit != 0 -> urllib fallback is used."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(tmod.subprocess, "run",
                        lambda *a, **k: _curl_result(returncode=7,
                                                    stdout=b"",
                                                    stderr=b"connect failed"))

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"via": "urllib"}'

    monkeypatch.setattr(tmod.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    assert tmod._http_get_json("https://example.invalid/x") == {"via": "urllib"}


def test_curl_crash_falls_back_to_urllib(monkeypatch):
    """curl raising (e.g. timeout) -> urllib fallback, never raises."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: "/usr/bin/curl")
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="curl", timeout=1)
    monkeypatch.setattr(tmod.subprocess, "run", _raise)

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"via": "urllib"}'

    monkeypatch.setattr(tmod.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    assert tmod._http_get_json("https://example.invalid/x") == {"via": "urllib"}


def test_no_curl_uses_urllib_directly(monkeypatch):
    """curl not installed -> straight to urllib, no subprocess call."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: None)
    def _boom(*a, **k):
        raise AssertionError("subprocess must not run when curl is missing")
    monkeypatch.setattr(tmod.subprocess, "run", _boom)

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"via": "urllib"}'

    monkeypatch.setattr(tmod.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    assert tmod._http_get_json("https://example.invalid/x") == {"via": "urllib"}


def test_both_fail_returns_none(monkeypatch):
    """curl fails AND urllib fails -> None, never raises."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(tmod.subprocess, "run",
                        lambda *a, **k: _curl_result(returncode=7))
    def _fail(*a, **k):
        raise urllib.error.URLError("connection reset")
    monkeypatch.setattr(tmod.urllib.request, "urlopen", _fail)
    assert tmod._http_get_json("https://example.invalid/x") is None


def test_curl_bad_json_falls_back_to_urllib(monkeypatch):
    """curl exit 0 but body isn't JSON -> treated as a transport failure,
    urllib fallback is used (the function never raises)."""
    monkeypatch.setattr(tmod.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(tmod.subprocess, "run",
                        lambda *a, **k: _curl_result(stdout=b"not json"))

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"via": "urllib"}'

    monkeypatch.setattr(tmod.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    assert tmod._http_get_json("https://example.invalid/x") == {"via": "urllib"}
