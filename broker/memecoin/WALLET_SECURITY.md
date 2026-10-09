# Wallet security — memecoin provider

**The short version:** your wallet's private key lives ONLY on your own
machine or VPS. It is read by the bot at runtime from an environment
variable or a locked-down file. It is never typed into chat, never sent
to anyone (including any AI assistant), never stored in this repo, and
never logged.

## The rule

Anyone who has your private key has your money. No exceptions, no
"just this once":

- **Never paste a private key into chat** — not to Muse, not to any
  support channel, not anywhere.
- **Never commit a key** to git, never put one in `defaults.yaml`,
  never email one to yourself.
- If a key was ever shared, pasted, or committed, **consider it burned**
  and rotate it (see below).

This repo enforces the rule in code: key material can never appear in
logs, exceptions, or `repr` (there are tests that assert this), and the
paper engine works with no key at all.

## How the bot reads your key (runtime only)

Configure `memecoin.wallet` in `config/defaults.yaml` (env vars
override the file):

```yaml
memecoin:
  wallet:
    key_source: env     # none | env | file
    key_file: ""        # only for key_source: file
```

| `key_source` | How it works |
|---|---|
| `none` (default) | No key is read. Paper mode needs no key — this is the default. |
| `env` | Reads `MEMECOIN_PRIVATE_KEY_<CHAIN>` (e.g. `MEMECOIN_PRIVATE_KEY_BASE`), falling back to `MEMECOIN_PRIVATE_KEY`. Set it in your shell or process manager on your own machine: `export MEMECOIN_PRIVATE_KEY_BASE=0x...` |
| `file` | Reads the key from `key_file` (env `MEMECOIN_WALLET_KEY_FILE`). The file **must** be a regular file owned by you with mode `0600` or stricter — the bot **refuses to load** anything group/world-readable. Create it with: `umask 077 && printf '%s' '0x...' > ~/.memecoin_key && chmod 600 ~/.memecoin_key` |

The key is read when a live order path needs it, used transiently, and
never stored. Status endpoints describe only *where* a key would come
from (`env:MEMECOIN_PRIVATE_KEY_BASE (set)`), never the value.

## What "live" means right now

Nothing. Every live path in this provider is **fail-closed**: even with
a key configured and arming enabled, live order submission raises
`BROKER_UNAVAILABLE`.

## Devnet testing and the key boundary

Devnet mode (`MEMECOIN_DEVNET=true`) is the **only** mode that signs
transactions, and it signs **devnet-only** transactions with a
**devnet-only** keypair:

* Generate a fresh keypair for devnet testing. It must never hold
  mainnet funds and must never be the same key as any mainnet wallet.
* The key is read at runtime via the same `key_source` mechanism above
  (env or `0600` file) — never pasted into chat, never committed.
* `MEMECOIN_WALLET_PUBKEY` pins the expected address; a mismatch
  fails closed before anything is signed.
* The devnet RPC client refuses non-devnet hosts by construction.

The standing rule still holds: this assistant must never be given a
private key — devnet or otherwise.

## Rotation guidance

- Generate a **fresh** key in your own wallet software (never reuse a
  key that was shared or committed).
- Fund it with only what the strategy needs — keep the bulk of funds
  in a separate cold wallet.
- Update the env var or key file on your machine, then restart the bot.
- If you suspect a key leaked: move funds to a new wallet **first**,
  then rotate the key in the bot config. A leaked key cannot be
  "un-leaked".
- Prefer one key per chain (`MEMECOIN_PRIVATE_KEY_BASE`,
  `MEMECOIN_PRIVATE_KEY_ETHEREUM`, …) so a rotation or compromise on
  one chain doesn't affect the others.

## Checklist before anything touches real money

1. [ ] Key lives only on your machine/VPS (env var or `0600` file).
2. [ ] Key was never pasted into chat, committed, or emailed.
3. [ ] Paper mode ran cleanly (fills journaled, kill switch tested).
4. [ ] You understand that the current code **cannot** trade live —
   any future live path will be a separate, explicitly confirmed step.
