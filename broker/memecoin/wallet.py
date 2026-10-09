"""Runtime wallet-key pattern for the multi-chain memecoin provider.

The wallet SECRET is never a config value, never logged, never stored
in the repo, and never leaves the operator's own machine/VPS. Providers
below only *read* a key at runtime, transiently, from one of:

  * NullKeyProvider — always None. Paper mode needs no key; this is
    the default.
  * EnvKeyProvider  — MEMECOIN_PRIVATE_KEY_<CHAIN> (e.g.
    MEMECOIN_PRIVATE_KEY_BASE), falling back to MEMECOIN_PRIVATE_KEY.
  * FileKeyProvider — a file path; REFUSES to load unless the file is
    a regular file owned by the current user with mode 0600 or stricter
    (no group/other permission bits at all). Missing file -> None.

Hard rules, enforced here:
  * Key material never appears in logs, exceptions, or repr — providers
    describe only *where* a key would come from ("set"/"unset",
    "present"/"missing"), never the value.
  * The key is consulted transiently (truthiness only) and never stored
    on any long-lived object.

`build_key_provider(source, key_file)` constructs one from the
`memecoin.wallet` config block. See broker/memecoin/WALLET_SECURITY.md
for the operator-facing guide.
"""

import abc
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Optional

from broker import CONFIG_INVALID, CREDENTIALS_INVALID, BrokerError

logger = logging.getLogger("broker.memecoin.wallet")


class WalletKeyProvider(abc.ABC):
    """Reads a wallet secret at runtime. Never exposes key material."""

    @abc.abstractmethod
    def get_key(self, chain_name: str) -> Optional[str]:
        """Return the secret for a chain, or None if not configured."""

    def describe_source(self, chain_name: str) -> str:
        """Human-readable key *location* (never the value) for status
        messages, e.g. 'env:MEMECOIN_PRIVATE_KEY_BASE (set)'."""
        return type(self).__name__

    def __repr__(self) -> str:  # never leak key material
        return f"{type(self).__name__}(redacted)"


class NullKeyProvider(WalletKeyProvider):
    """Always None — paper mode needs no key."""

    def get_key(self, chain_name: str) -> Optional[str]:
        return None

    def describe_source(self, chain_name: str) -> str:
        return "none (paper mode needs no key)"


class EnvKeyProvider(WalletKeyProvider):
    """MEMECOIN_PRIVATE_KEY_<CHAIN> (uppercased), else MEMECOIN_PRIVATE_KEY.

    Empty or whitespace-only values count as unset (-> None).
    """

    FALLBACK_VAR = "MEMECOIN_PRIVATE_KEY"

    @staticmethod
    def _var_for(chain_name: str) -> str:
        slug = re.sub(r"[^A-Z0-9]", "_", (chain_name or "").upper())
        return f"MEMECOIN_PRIVATE_KEY_{slug}"

    def get_key(self, chain_name: str) -> Optional[str]:
        for var in (self._var_for(chain_name), self.FALLBACK_VAR):
            val = os.environ.get(var, "")
            if val and val.strip():
                return val.strip()
        return None

    def describe_source(self, chain_name: str) -> str:
        var = self._var_for(chain_name)
        specific = "set" if (os.environ.get(var) or "").strip() else "unset"
        fallback = ("set" if (os.environ.get(self.FALLBACK_VAR) or "").strip()
                    else "unset")
        return f"env:{var} ({specific}), fallback {self.FALLBACK_VAR} ({fallback})"


class FileKeyProvider(WalletKeyProvider):
    """Reads the key from a file. Refuses unsafe files.

    The file must be a regular file, owned by the current user, with no
    group/other permission bits (0600 or stricter). Missing file -> None.
    Accepted content: either the raw key on its own, or KEY=VALUE lines
    (takes MEMECOIN_PRIVATE_KEY / PRIVATE_KEY / KEY, first match wins).
    """

    _KEY_NAMES = ("MEMECOIN_PRIVATE_KEY", "PRIVATE_KEY", "KEY")

    def __init__(self, path: str):
        # Only the path is stored — never key material.
        self._path = Path(path).expanduser()

    def _check_permissions(self) -> None:
        p = self._path
        try:
            st = p.stat()
        except OSError as exc:
            raise BrokerError(CREDENTIALS_INVALID,
                              f"wallet key file {p} is not readable: {exc}")
        if not stat.S_ISREG(st.st_mode):
            raise BrokerError(
                CREDENTIALS_INVALID,
                f"wallet key file {p} is not a regular file — refusing to load")
        if st.st_mode & 0o177:
            raise BrokerError(
                CREDENTIALS_INVALID,
                f"wallet key file {p} has permissions "
                f"{oct(stat.S_IMODE(st.st_mode))} (group/other accessible) — "
                f"must be 0600 or stricter; refusing to load")
        try:
            euid = os.geteuid()
        except AttributeError:  # non-POSIX: skip the ownership check
            euid = None
        if euid is not None and st.st_uid != euid:
            raise BrokerError(
                CREDENTIALS_INVALID,
                f"wallet key file {p} is not owned by the current user — "
                f"refusing to load")

    @classmethod
    def _parse(cls, text: str) -> Optional[str]:
        stripped = text.strip()
        # JSON keypair (e.g. solana-keygen array) — accept even with
        # whitespace, since json.dump emits spaces after commas.
        if stripped[:1] in ("[", '"'):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, (list, str)):
                return stripped
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip().upper() in cls._KEY_NAMES and v.strip():
                    return v.strip().strip('"').strip("'")
            elif " " not in line and "\t" not in line:
                # A lone token with no '=' is the raw key.
                return line
        return None

    def get_key(self, chain_name: str) -> Optional[str]:
        _ = chain_name  # file provider is chain-agnostic by design
        if not self._path.exists():
            return None
        self._check_permissions()
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            # Never echo file contents — only the failure.
            raise BrokerError(CREDENTIALS_INVALID,
                              f"wallet key file {self._path} could not be "
                              f"read: {type(exc).__name__}")
        return self._parse(text)

    def describe_source(self, chain_name: str) -> str:
        _ = chain_name
        state = "present" if self._path.exists() else "missing"
        return f"file:{self._path} ({state}, 0600 required)"

    def __repr__(self) -> str:  # path is not secret; the key never appears
        return f"{type(self).__name__}(path={str(self._path)!r}, redacted)"


def build_key_provider(key_source: str = "none",
                       key_file: str = "") -> WalletKeyProvider:
    """Construct a WalletKeyProvider from the memecoin.wallet config.

    key_source: none | env | file. Raises CONFIG_INVALID for anything
    else, or for key_source=file without a key_file path.
    """
    source = (key_source or "none").strip().lower()
    if source == "none":
        return NullKeyProvider()
    if source == "env":
        return EnvKeyProvider()
    if source == "file":
        if not (key_file or "").strip():
            raise BrokerError(
                CONFIG_INVALID,
                "memecoin.wallet.key_source=file requires "
                "memecoin.wallet.key_file (MEMECOIN_WALLET_KEY_FILE)")
        return FileKeyProvider(key_file.strip())
    raise BrokerError(
        CONFIG_INVALID,
        f"unknown memecoin.wallet.key_source {key_source!r} "
        f"(expected none|env|file)")
