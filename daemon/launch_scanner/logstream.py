"""Sub-second launch detection via Solana program-log subscription.

Polling pump.fun / DexScreener sees a launch seconds after it lands.
``logsSubscribe`` on the pump.fun program streams new-mint transactions
the moment they are processed. A background daemon thread holds the
websocket; each scan cycle the scanner drains freshly detected mints into
the normal candidate pipeline (instant CA + confirmation judge).

Stdlib only — a minimal RFC 6455 client with HTTP CONNECT proxy support,
so no new dependency is needed on the VPS either. If the websocket is
unreachable (proxy blocks it, no route, handshake fails) the thread backs
off and the scanner keeps working on polling alone: detection degrades,
it never crashes.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("forex_agent.daemon.launch_scanner.logstream")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL_MINT = "So11111111111111111111111111111111111111112"
WS_URL = "wss://api.mainnet-beta.solana.com"
HTTP_RPC_URL = "https://api.mainnet-beta.solana.com"
# Failover pool for transaction fetches: the first reachable endpoint
# wins. Health-scored per URL so a flaky endpoint stops being tried
# first. All are public, no API key needed.
HTTP_RPC_URLS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana.public-rpc.com",
    "https://rpc.ankr.com/solana",
)


def _rpc_label(url: str) -> str:
    """Safe log label: strips query strings (may carry API keys)."""
    return url.split("?", 1)[0]
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    """Websocket transport failure."""


class WSClient:
    """Minimal RFC 6455 client: text frames, ping/pong, close, proxy CONNECT."""

    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    # ------------------------------------------------------------------
    # connect
    # ------------------------------------------------------------------
    @staticmethod
    def _proxy_for(secure: bool) -> Optional[Tuple[str, int]]:
        raw = (os.environ.get("https_proxy" if secure else "http_proxy")
               or os.environ.get("HTTPS_PROXY" if secure else "HTTP_PROXY"))
        if not raw:
            return None
        parts = urlsplit(raw)
        if not parts.hostname:
            return None
        return parts.hostname, parts.port or 8080

    @staticmethod
    def _send_all(sock: socket.socket, data: bytes) -> None:
        view = memoryview(data)
        while view:
            sent = sock.send(view)
            view = view[sent:]

    def _read_line(self, sock: socket.socket) -> str:
        buf = self._buf
        while b"\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            buf += chunk
        line, self._buf = buf.split(b"\r\n", 1)
        return line.decode("latin-1")

    def connect(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise WSError("unsupported scheme %s" % parts.scheme)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        proxy = self._proxy_for(secure)
        if proxy:
            ph, pp = proxy
            sock = socket.create_connection((ph, pp), timeout=self.timeout)
            self._send_all(
                sock,
                ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                 % (host, port, host, port)).encode())
            status = self._read_line(sock)
            if not (status.startswith("HTTP/1.1 200")
                    or status.startswith("HTTP/1.0 200")):
                raise WSError("proxy CONNECT rejected: %s" % status.strip())
            while self._read_line(sock).strip():
                pass
        else:
            sock = socket.create_connection((host, port), timeout=self.timeout)

        if secure:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode()
        self._send_all(sock, (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
            % (path, host, port, key)).encode())
        status = self._read_line(sock)
        if "101" not in status:
            raise WSError("websocket handshake rejected: %s" % status.strip())
        accept = None
        while True:
            line = self._read_line(sock)
            if not line.strip():
                break
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        expect = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if accept != expect:
            raise WSError("bad Sec-WebSocket-Accept")
        sock.settimeout(self.timeout)
        self._sock = sock

    # ------------------------------------------------------------------
    # frames
    # ------------------------------------------------------------------
    def _recv_exactly(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        assert self._sock is not None
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])  # FIN + text opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._send_all(self._sock, bytes(header) + masked)

    def _pong(self, payload: bytes) -> None:
        assert self._sock is not None
        n = len(payload)
        if n >= 126:
            return  # oversized ping: ignore
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._send_all(self._sock, bytes([0x8A, 0x80 | n]) + mask + masked)

    def recv_text(self) -> Optional[str]:
        """Next complete text message, or None on clean server close."""
        fragments: List[bytes] = []
        while True:
            head = self._recv_exactly(2)
            b1, b2 = head[0], head[1]
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exactly(8))[0]
            mask = self._recv_exactly(4) if (b2 & 0x80) else None
            payload = self._recv_exactly(length) if length else b""
            if mask:
                payload = bytes(c ^ mask[i % 4]
                                for i, c in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
                continue
            # binary / unknown opcodes: skip

    def close(self) -> None:
        try:
            if self._sock is not None:
                # masked empty close frame
                self._send_all(self._sock, b"\x88\x80" + os.urandom(4))
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None


# ----------------------------------------------------------------------
# mint extraction
# ----------------------------------------------------------------------
def extract_mint(tx: Dict[str, Any]) -> Optional[str]:
    """Find the newly created token mint in a jsonParsed transaction."""
    try:
        msg = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}

        def _walk(ixs: Any) -> Optional[str]:
            for ix in ixs or []:
                if not isinstance(ix, dict):
                    continue
                parsed = ix.get("parsed") or {}
                if parsed.get("type") in ("initializeMint", "initializeMint2"):
                    mint = (parsed.get("info") or {}).get("mint")
                    if mint:
                        return str(mint)
            return None

        all_ix: List[Any] = list(msg.get("instructions") or [])
        for inner in meta.get("innerInstructions") or []:
            if isinstance(inner, dict):
                all_ix.extend(inner.get("instructions") or [])
        hit = _walk(all_ix)
        if hit:
            return hit

        # Fallback: mint present in post balances but not pre balances.
        pre = {b.get("mint") for b in meta.get("preTokenBalances") or []
               if isinstance(b, dict) and b.get("mint")}
        for b in meta.get("postTokenBalances") or []:
            if not isinstance(b, dict):
                continue
            mint = b.get("mint")
            if mint and mint not in pre and mint != WSOL_MINT:
                return str(mint)
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------
# background subscriber
# ----------------------------------------------------------------------
class LogStream:
    """Background program-log subscriber.

    ``drain()`` returns ``[{mint, signature, ts}]`` detected since the last
    drain. Never raises out of its thread; when the websocket is down the
    scanner simply keeps polling.
    """

    def __init__(self, ws_url: str = WS_URL, http_rpc: Optional[str] = None,
                 programs: Tuple[str, ...] = (PUMP_PROGRAM,),
                 http_rpcs: Optional[List[str]] = None):
        self._ws_url = ws_url
        if http_rpcs:
            urls = [u for u in http_rpcs if u]
        elif http_rpc:
            urls = [http_rpc]
        else:
            urls = list(HTTP_RPC_URLS)
        if not urls:
            urls = list(HTTP_RPC_URLS)
        self._http_rpc_url = urls[0]  # legacy single-url attribute
        self._http_rpc_urls = urls
        self._rpc_health = {u: 1.0 for u in urls}
        self._rpc_active = urls[0]
        self._programs = programs
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._healthy = False
        self._seen_sigs = set()
        # RPC politeness / failure accounting (worker thread only).
        self._last_rpc_ts = 0.0
        self._rpc_fail_count = 0
        self._last_rpc_warn = 0.0

    @property
    def rpc_urls(self) -> List[str]:
        """Endpoints ordered by current health (healthiest first)."""
        return sorted(self._http_rpc_urls,
                      key=lambda u: self._rpc_health.get(u, 0.0),
                      reverse=True)

    def _bump_rpc_health(self, url: str, ok: bool) -> None:
        h = self._rpc_health.get(url, 1.0)
        self._rpc_health[url] = (min(1.0, h + 0.3) if ok
                                 else max(0.0, h - 0.4))
        if ok and url != self._rpc_active:
            logger.warning("logstream rpc failover: now using %s",
                           _rpc_label(url))
            self._rpc_active = url

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="degen-logstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def drain(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    # -- internals ------------------------------------------------------
    def _set_healthy(self, value: bool) -> None:
        with self._lock:
            self._healthy = value

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._serve()
            except Exception as exc:
                logger.warning("logstream connection failed: %s", exc)
            self._set_healthy(False)
            if self._stop.is_set():
                break
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 120.0)

    def _serve(self) -> None:
        ws = WSClient(self._ws_url)
        try:
            ws.connect()
            req_id = 0
            for prog in self._programs:
                req_id += 1
                ws.send_text(json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [prog]},
                               {"commitment": "processed"}],
                }))
            self._set_healthy(True)
            logger.warning("logstream subscribed to %d program(s)",
                           len(self._programs))
            while not self._stop.is_set():
                msg = ws.recv_text()
                if msg is None:
                    raise WSError("server closed the stream")
                self._handle_message(msg)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except Exception:
            return
        if data.get("method") != "logsNotification":
            return  # subscribe acks etc.
        try:
            value = ((data.get("params") or {}).get("result", {})
                     .get("value", {})) or {}
        except Exception:
            return
        if value.get("err"):
            return  # failed transaction — ignore
        sig = value.get("signature")
        if not sig or not isinstance(sig, str) or sig in self._seen_sigs:
            return
        self._seen_sigs.add(sig)
        if len(self._seen_sigs) > 5000:
            self._seen_sigs = set(list(self._seen_sigs)[-2500:])
        mint = self._mint_for_signature(sig)
        if mint:
            logger.warning("logstream detected new mint %s (sig %s...)",
                           mint, sig[:12])
            self._q.put({"mint": mint, "signature": sig,
                         "ts": time.time()})

    def _mint_for_signature(self, sig: str) -> Optional[str]:
        # Politeness: at most ~2 transaction fetches/sec so the public RPC
        # doesn't rate-limit us during launch bursts.
        now = time.monotonic()
        delta = now - self._last_rpc_ts
        if delta < 0.5:
            time.sleep(0.5 - delta)
        self._last_rpc_ts = time.monotonic()
        tx = self._http_rpc("getTransaction",
                            [sig, {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0}])
        if not isinstance(tx, dict):
            return None
        return extract_mint(tx)

    def _note_rpc_failure(self, method: str, exc: Any) -> None:
        # Loud enough to notice, quiet enough not to flood the log during
        # a flaky-network stretch: one line per minute with a count.
        now = time.monotonic()
        self._rpc_fail_count += 1
        if now - self._last_rpc_warn >= 60.0:
            logger.warning("logstream rpc %s failing (%d failures, "
                           "latest: %s)", method, self._rpc_fail_count, exc)
            self._rpc_fail_count = 0
            self._last_rpc_warn = now

    def _http_rpc(self, method: str, params: List[Any]) -> Any:
        """JSON-RPC with endpoint failover.

        Tries endpoints healthiest-first, two attempts each. A dead
        endpoint is skipped next time; the first success re-arms it.
        Returns the result, or None when every endpoint failed.
        """
        payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}).encode()
        transient = (http.client.IncompleteRead,
                     http.client.RemoteDisconnected,
                     http.client.BadStatusLine, socket.timeout,
                     urllib.error.URLError, ConnectionError)
        last_exc: Optional[BaseException] = None
        for url in self.rpc_urls:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "forex-agent/1.0",
                         "Connection": "close"})
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(
                            resp.read().decode("utf-8", "replace"))
                    self._bump_rpc_health(url, True)
                    return data.get("result")
                except transient as exc:
                    last_exc = exc
                    time.sleep(0.5 * (attempt + 1))
                except Exception as exc:
                    self._note_rpc_failure(method, exc)
                    return None
            self._bump_rpc_health(url, False)
        self._note_rpc_failure(method, last_exc)
        return None
