"""Transaction builder for the SolBot LIVE 5-bot engine (SOL-only).

This module is the ONE place in the live engine that may turn a
validated signer into a signed Solana transaction. It is wired
into the loop ONLY when a deployment explicitly passes
``--tx-builder solana`` (see loop.py). With no tx_builder wired,
swap.py still refuses every swap — this file changes nothing
about the default behaviour, and the Dockerfile CMD is still
the disabled guard.

Gate order, before ANY signing or sending (all refusals return
a fixed error code, never key material):
  1. LIVE_ENABLED must be exactly "1" (engine.is_live_enabled).
  2. NETWORK must parse to devnet or mainnet.
  3. Signer must validate via engine.load_signer_with_status:
     exactly one of TRADING_PRIVATE_KEY / TRADING_KEYPAIR, and
     the derived public key must match the settled wallet
     9g9R…UTFQ (or the expected_pubkey a test supplies).
  4. Quote must be fresh (age <= 30s, positive output) and its
     input/output mints must match the requested direction
     (SOL -> token for buys, token -> SOL for sells).
  5. Slippage guard (BUILDER DEFAULT 1.0%, NOT James's number):
     the plan's expected output must be >= min_output, and
     min_output itself must be >= quote.output * (1 - guard).
     A caller passing a looser min_output is refused — the
     guard cannot be widened by a caller.
  6. A swap-transaction provider must supply an unsigned
     transaction whose message's first account (fee payer) is
     the trading wallet, and whose plan mints match the quote.
  7. A recent blockhash is fetched from the network RPC (or
     taken from the supplied transaction's message, if it is
     already present and non-zero), the message is signed with
     the validated key, and the signed transaction is sent
     via RPC. A send failure, or a failure to confirm, is
     reported — never silently treated as success.

Key handling in THIS file (read §8 of RULES.md first)
----------------------------------------------------
engine.load_signer_with_status deliberately returns only an
opaque marker, never key bytes — so a builder that signs must
read the key itself, once, from the same two environment
variables, validate it the same way (derive the public key,
compare to the expected wallet), use the 32-byte seed only
inside the signing call, and drop the reference immediately
afterwards. That transient seed never enters a log, an error
string, a return value, or a file. If the seed cannot be
derived and validated, the builder refuses without signing.

Signing is pure-Python Ed25519 (RFC 8032) over the helpers in
engine.py (derivation only there; signing here), and Solana
transactions use the legacy message format, stdlib only. No
external SDK, no pool SDK, no dependency is introduced.

What this builder does NOT do
------------------------------
* It does NOT invent a route, a pool, or a price. Its default
  swap-transaction provider is Jupiter's swap API; in this
  sandbox that API returned an empty reply, so the provider
  fails and the builder refuses — correct, fail-closed
  behaviour, not a route.
* It does NOT price a devnet swap from a mainnet pair. The
  DevnetPoolQuoter below quotes ONLY from explicitly injected,
  verified devnet pool data (see §2 of this module). With no
  injected data it returns no quote, and every devnet swap is
  refused with route_unverified.
* It does NOT sign or send anything in tests: tests use mocked
  providers/RPC and in-memory test-only keys, never a real
  key, never a real chain send. In THIS build (code only, no
  deployment) no transaction is signed or sent at all — the
  builder is only exercised by its tests, with mocks.
* It does NOT log signatures, key material, or seed material.
  After a real send, the signature is returned to the caller
  (swap.py / loop.py) as a value, not logged here.

Devnet route status (researched read-only, 2026-11-01)
-------------------------------------------------------
* Jupiter v6 quote: EMPTY REPLY in this sandbox for both a
  mainnet SOL->token quote and a devnet SOL->token quote.
  Jupiter's current APIs are mainnet-focused; no devnet route
  was proven through Jupiter.
* DexScreener for the devnet token mint
  4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU: pairs = null.
  DexScreener does not index devnet pairs for that mint.
  (That mint is named here as research evidence only — it is
  NOT a trading currency in this build, which is SOL-only.)
* Solana devnet RPC getAccountInfo for the mainnet DEX program
  addresses (2026-11-01 check): Raydium AMM program and
  Jupiter aggregator program -> accounts that are NOT
  executable (executable=false, owner = System Program,
  space 0) — i.e. those addresses are not programs on
  devnet. Mainnet DEX programs cannot be used as a devnet
  route at their mainnet addresses.
* Orca pools API and Raydium pools API returned MAINNET pool
  data (chainId 101) — not devnet liquidity, and not usable
  for a devnet swap.
CORRECTION (verified live on devnet RPC, 2026-11-02 — see
orca_devnet.py, which supersedes the 2026-11-01 Orca claim
above): the Orca Whirlpool program DOES exist on devnet as
an executable program, its devnet config account exists,
and a documented devnet test pool exists, is owned by that
program, and is funded. A generic Whirlpool pool quoter now
exists in orca_devnet.py (quote only — it reads a pool
account and computes its spot price). That quoter is NOT
wired into the trading route checker, because no Whirlpool
swap-transaction provider has been built or tested on
devnet yet. Quote data on devnet is therefore proven for
a real pool; swap EXECUTION on devnet is not.
CONCLUSION: NO devnet SOL swap route for TRADING is verified
by this build. A deployment proves one only by supplying a
tested Whirlpool (or Raydium) swap provider plus verified
devnet pool data (a devnet test token with a real devnet
pool, via DevnetPoolQuoter / DEVNET_POOLS_JSON) — and the
smoke test must show it before the first devnet trade.
Until then, devnet swaps refuse with route_unverified, by
design.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    SOL_MINT, WALLET_PUBKEY, base58_decode, base58_encode,
    is_live_enabled, load_signer_with_status, parse_network,
    publickey_bytes_from_secret, _ed_add, _ed_scalarmult,
    _ed_xrecover, _ED_BASE, _ED_Q,
)
from swap import MAX_QUOTE_AGE_SECONDS, SLIPPAGE_GUARD_PCT  # noqa: E402

logger = logging.getLogger("solbot-live-tx")

# --- Fixed error codes (never include key material) ---
ERR_LIVE_DISABLED = "live_disabled"
ERR_INVALID_NETWORK = "invalid_network"
ERR_NO_SIGNER = "no_signer"
ERR_STALE_QUOTE = "stale_quote"
ERR_QUOTE_MISMATCH = "quote_mint_mismatch"
ERR_SLIPPAGE_GUARD = "slippage_guard_exceeded"
ERR_ROUTE_UNVERIFIED = "route_unverified"
ERR_TX_BUILDER_NOT_CONFIGURED = "tx_builder_not_configured"
ERR_TX_PLAN_MISMATCH = "tx_plan_mismatch"
ERR_FEE_PAYER_MISMATCH = "fee_payer_mismatch"
ERR_RPC_FAILED = "rpc_failed"
ERR_TX_SEND_FAILED = "tx_send_failed"
ERR_TX_CONFIRM_FAILED = "tx_confirm_failed"

JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Ed25519 group order (RFC 8032).
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493


# ---------------------------------------------------------------------------
# Ed25519 signing (pure Python, RFC 8032)
# ---------------------------------------------------------------------------
def _encode_point(point):
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(data: bytes):
    if len(data) != 32:
        raise ValueError("point must be 32 bytes")
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    sign = (data[31] >> 7) & 1
    x = _ed_xrecover(y)
    if (x & 1) != sign:
        x = (_ED_Q - x) % _ED_Q
    return [x, y]


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    """Sign a message with a 32-byte seed. Returns 64-byte signature."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    hashed = hashlib.sha512(seed).digest()
    a_bytes = bytearray(hashed[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    a = int.from_bytes(a_bytes, "little")
    prefix = hashed[32:]
    public = _encode_point(_ed_scalarmult(_ED_BASE, a))
    r = int.from_bytes(
        hashlib.sha512(prefix + message).digest(), "little") % _ED_L
    r_point = _encode_point(_ed_scalarmult(_ED_BASE, r))
    h = int.from_bytes(
        hashlib.sha512(r_point + public + message).digest(),
        "little") % _ED_L
    s = (r + h * a) % _ED_L
    return r_point + s.to_bytes(32, "little")


def ed25519_verify(public_key_bytes: bytes, message: bytes,
                   signature: bytes) -> bool:
    """Verify a 64-byte Ed25519 signature. Pure function, for tests."""
    if len(signature) != 64 or len(public_key_bytes) != 32:
        return False
    r_bytes, s_bytes = signature[:32], signature[32:]
    s = int.from_bytes(s_bytes, "little")
    if s >= _ED_L:
        return False
    try:
        r_point = _decode_point(r_bytes)
        a_point = _decode_point(public_key_bytes)
    except (ValueError, IndexError):
        return False
    h = int.from_bytes(
        hashlib.sha512(r_bytes + public_key_bytes + message).digest(),
        "little") % _ED_L
    left = _ed_scalarmult(_ED_BASE, s)
    right = _ed_add(r_point, _ed_scalarmult(a_point, h))
    return left == right


# ---------------------------------------------------------------------------
# Solana legacy transaction (compact-u16 / shortvec) helpers
# ---------------------------------------------------------------------------
def compact_u16_encode(value: int) -> bytes:
    out = bytearray()
    rem = value
    while True:
        byte = rem & 0x7F
        rem >>= 7
        if rem == 0:
            out.append(byte)
            break
        out.append(byte | 0x80)
    return bytes(out)


def compact_u16_decode(data: bytes, offset: int):
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated compact-u16")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            break
        shift += 7
    return value, offset


def build_message(fee_payer_b58: str, instructions, blockhash_b58: str) -> bytes:
    """Build a legacy Solana message.

    instructions: list of {program_id (base58), accounts ([base58]),
    data (bytes)}. Account order: fee payer first (signer, writable),
    then other accounts in first-seen order, then program ids
    (readonly, unsigned). Header: 1 required signature, 0 readonly
    signed, N readonly unsigned (= number of program ids).
    """
    program_ids = []
    for ins in instructions:
        if ins["program_id"] not in program_ids:
            program_ids.append(ins["program_id"])
    account_keys = [fee_payer_b58]
    for ins in instructions:
        for acct in ins["accounts"]:
            if acct not in account_keys:
                account_keys.append(acct)
    for pid in program_ids:
        if pid not in account_keys:
            account_keys.append(pid)
    header = bytes([1, 0, len(program_ids)])
    out = bytearray(header)
    out += compact_u16_encode(len(account_keys))
    for key in account_keys:
        out += base58_decode(key)
    out += base58_decode(blockhash_b58)
    out += compact_u16_encode(len(instructions))
    for ins in instructions:
        prog_idx = account_keys.index(ins["program_id"])
        out.append(prog_idx)
        out += compact_u16_encode(len(ins["accounts"]))
        for acct in ins["accounts"]:
            out.append(account_keys.index(acct))
        out += compact_u16_encode(len(ins["data"]))
        out += ins["data"]
    return bytes(out)


def parse_message(message: bytes):
    """Parse a legacy message. Returns dict with header, account_keys
    (base58 list), blockhash (base58), instructions. Raises
    ValueError on malformed input."""
    if len(message) < 3:
        raise ValueError("message too short")
    header = list(message[:3])
    offset = 3
    n_keys, offset = compact_u16_decode(message, offset)
    keys = []
    for _ in range(n_keys):
        if offset + 32 > len(message):
            raise ValueError("truncated account keys")
        keys.append(base58_encode(message[offset:offset + 32]))
        offset += 32
    if offset + 32 > len(message):
        raise ValueError("truncated blockhash")
    blockhash = base58_encode(message[offset:offset + 32])
    offset += 32
    n_ins, offset = compact_u16_decode(message, offset)
    instructions = []
    for _ in range(n_ins):
        if offset >= len(message):
            raise ValueError("truncated instruction")
        prog_idx = message[offset]
        offset += 1
        n_accts, offset = compact_u16_decode(message, offset)
        accts = []
        for _ in range(n_accts):
            if offset >= len(message):
                raise ValueError("truncated instruction accounts")
            accts.append(message[offset])
            offset += 1
        n_data, offset = compact_u16_decode(message, offset)
        if offset + n_data > len(message):
            raise ValueError("truncated instruction data")
        data = message[offset:offset + n_data]
        offset += n_data
        instructions.append({"program_id_index": prog_idx,
                             "accounts": accts, "data": data})
    return {"header": header, "account_keys": keys,
            "blockhash": blockhash, "instructions": instructions}


def parse_serialized_tx(serialized_b64: str):
    """Split a base64 serialized transaction into (signatures,
    message_bytes). Raises ValueError on malformed input."""
    raw = __import__("base64").b64decode(serialized_b64, validate=True)
    n_sigs, offset = compact_u16_decode(raw, 0)
    if offset + n_sigs * 64 > len(raw):
        raise ValueError("truncated signatures")
    sigs = [raw[offset + i * 64: offset + (i + 1) * 64]
            for i in range(n_sigs)]
    return sigs, raw[offset + n_sigs * 64:]


def sign_serialized_tx(serialized_b64: str, seed: bytes,
                       expected_fee_payer_b58: str) -> str:
    """Re-sign a serialized (unsigned) transaction's message.

    Verifies the message's first account key is the expected fee
    payer before signing, then returns a base64 transaction with
    exactly one signature. Raises ValueError on any mismatch.
    """
    _sigs, message = parse_serialized_tx(serialized_b64)
    parsed = parse_message(message)
    if not parsed["account_keys"]:
        raise ValueError("message has no account keys")
    if parsed["account_keys"][0] != expected_fee_payer_b58:
        raise ValueError(ERR_FEE_PAYER_MISMATCH)
    signature = ed25519_sign(seed, message)
    import base64 as _b64
    return _b64.b64encode(
        compact_u16_encode(1) + signature + message).decode("ascii")


# ---------------------------------------------------------------------------
# Signing-seed loading (transient, this file only)
# ---------------------------------------------------------------------------
def load_signing_seed(expected_pubkey: str = WALLET_PUBKEY):
    """Return (seed_bytes_or_None, error_code_or_None).

    Reads exactly one key variable, derives its public key, and
    compares it to expected_pubkey. The returned seed is the
    32-byte seed (from a 64-byte secret's first half, or a 32-byte
    seed directly). Callers must drop the reference after signing.
    Never logs or returns key material on failure — only a code.
    """
    if not is_live_enabled():
        return None, ERR_LIVE_DISABLED
    raw_pair = (os.environ.get("TRADING_KEYPAIR") or "").strip()
    raw_b58 = (os.environ.get("TRADING_PRIVATE_KEY") or "").strip()
    if raw_pair and raw_b58:
        return None, "both_keys_set"
    if not raw_pair and not raw_b58:
        return None, "no_key_set"
    try:
        if raw_pair:
            arr = json.loads(raw_pair)
            if not isinstance(arr, list) or len(arr) != 64:
                return None, "invalid_key_format"
            if not all(isinstance(x, int) and 0 <= x <= 255 for x in arr):
                return None, "invalid_key_format"
            secret = bytes(arr)
        else:
            secret = base58_decode(raw_b58)
        derived = base58_encode(publickey_bytes_from_secret(secret))
    except (ValueError, json.JSONDecodeError):
        return None, "invalid_key_format"
    if derived != expected_pubkey:
        return None, "pubkey_mismatch"
    if len(secret) == 64:
        return secret[:32], None
    if len(secret) == 32:
        return secret, None
    return None, "invalid_key_format"


# ---------------------------------------------------------------------------
# RPC client (stdlib JSON-RPC; injectable for tests)
# ---------------------------------------------------------------------------
class SolanaRpcClient:
    """Minimal Solana JSON-RPC client: recent blockhash, send,
    confirm. http_post, if supplied, must be (url, payload_dict)
    -> response_dict (tests inject it; production uses urllib)."""

    def __init__(self, rpc_url: str, http_post=None, timeout=20):
        self.rpc_url = rpc_url
        self.http_post = http_post or self._default_post
        self.timeout = timeout

    def _default_post(self, url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "solbot-live-tx"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call(self, method, params):
        try:
            resp = self.http_post(
                self.rpc_url,
                {"jsonrpc": "2.0", "id": 1, "method": method,
                 "params": params})
        except Exception:  # noqa: BLE001 - RPC failure = refuse
            return None, ERR_RPC_FAILED
        if not isinstance(resp, dict) or "error" in resp:
            return None, ERR_RPC_FAILED
        return resp.get("result"), None

    def get_recent_blockhash(self):
        result, err = self._call("getRecentBlockhash", [])
        if err or not result:
            return None, ERR_RPC_FAILED
        blockhash = (result.get("value") or {}).get("blockhash")
        if not blockhash:
            return None, ERR_RPC_FAILED
        return blockhash, None

    def send_transaction(self, signed_b64: str):
        result, err = self._call(
            "sendTransaction",
            [signed_b64, {"encoding": "base64",
                          "preflightCommitment": "confirmed"}])
        if err or not result:
            return None, ERR_TX_SEND_FAILED
        return result, None

    def confirm_transaction(self, signature: str, attempts: int = 5,
                            delay_seconds: float = 1.0):
        for _ in range(attempts):
            result, err = self._call(
                "getSignatureStatuses", [[signature]])
            if not err and result and result[0]:
                status = result[0]
                if status.get("err") is not None:
                    return False, ERR_TX_SEND_FAILED
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, None
            time.sleep(delay_seconds)
        return False, ERR_TX_CONFIRM_FAILED


# ---------------------------------------------------------------------------
# Swap-transaction providers
# ---------------------------------------------------------------------------
def jupiter_swap_tx_provider(direction, mint, input_amount_smallest,
                             min_output, quote, wallet_pubkey):
    """Fetch an unsigned Jupiter swap transaction for a quote.

    Returns (plan_dict_or_None, error_code_or_None). The plan's
    expected output is the quote's output (Jupiter's swap response
    does not reliably restate it); the builder re-checks it
    against min_output. Without quote.raw (the full quoteResponse
    Jupiter's swap endpoint requires), this provider refuses —
    it cannot reconstruct a quoteResponse. In this sandbox the
    Jupiter API returns an empty reply, so this provider fails
    and the builder refuses — correct, fail-closed behaviour.
    """
    raw_quote = getattr(quote, "raw", None)
    if not raw_quote:
        return None, "quote_raw_missing"
    payload = {
        "userPublicKey": wallet_pubkey,
        "quoteResponse": raw_quote,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        req = urllib.request.Request(
            JUPITER_SWAP_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "solbot-live-tx"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - provider failure = no route
        return None, ERR_ROUTE_UNVERIFIED
    serialized = data.get("swapTransaction")
    if not serialized:
        return None, ERR_ROUTE_UNVERIFIED
    return {
        "serialized_tx_b64": serialized,
        "expected_output": float(quote.output_amount),
        "input_mint": quote.input_mint,
        "output_mint": quote.output_mint,
        "source": "jupiter_v6_swap",
    }, None


class DevnetPoolQuoter:
    """Quote from EXPLICITLY INJECTED, verified devnet pool data only.

    pools: {mint: {"price_sol": float, "liquidity_usd": float,
    "decimals": int, "pool_address": str, "program_id": str,
    "source": str, "verified_at": str}}. A mint with no entry, a
    non-positive price, or missing decimals returns NO quote.
    Mainnet pair data must never be injected here — this quoter
    is for devnet pools verified by a deployment, and only those.
    """

    def __init__(self, pools=None, network="devnet"):
        self.pools = pools or {}
        self.network = network

    def quote(self, input_mint, output_mint, input_amount_smallest):
        if self.network != "devnet":
            return None
        if input_mint == SOL_MINT:
            mint, is_buy = output_mint, True
        elif output_mint == SOL_MINT:
            mint, is_buy = input_mint, False
        else:
            return None
        pool = self.pools.get(mint)
        if not pool:
            return None
        try:
            price_sol = float(pool["price_sol"])
            decimals = int(pool["decimals"])
        except (KeyError, TypeError, ValueError):
            return None
        if price_sol <= 0 or decimals < 0:
            return None
        amount = float(input_amount_smallest)
        if amount <= 0:
            return None
        if is_buy:
            # Lamports in -> tokens out (smallest units).
            tokens = (amount / 1e9) / price_sol
            output_smallest = tokens * (10 ** decimals)
        else:
            # Token smallest units in -> lamports out.
            tokens = amount / (10 ** decimals)
            output_smallest = tokens * price_sol * 1e9
        if output_smallest <= 0:
            return None
        from swap import Quote  # late import: swap imports engine only
        return Quote(input_mint=input_mint, output_mint=output_mint,
                     input_amount=amount, output_amount=output_smallest,
                     source=pool.get("source", "devnet_pool_verified"))

    def quote_for_executor(self, input_mint, output_mint, amount):
        """Quoter callable for swap.py's SwapExecutor.

        SwapExecutor passes buy amounts in lamports (already
        smallest units) but sell amounts in TOKEN UNITS (see
        swap.py execute_sell / loop.py sell path, which pass
        Position.tokens). This wrapper converts a token-unit
        sell amount to smallest units using the pool's decimals
        before quoting, so the quote's input/output are in
        smallest units on both sides. If the mint has no pool
        entry or no decimals, it returns None (fail closed).
        """
        if input_mint == SOL_MINT:
            return self.quote(input_mint, output_mint, amount)
        pool = self.pools.get(input_mint)
        if not pool:
            return None
        try:
            decimals = int(pool["decimals"])
        except (KeyError, TypeError, ValueError):
            return None
        return self.quote(input_mint, output_mint,
                          float(amount) * (10 ** decimals))

    def check_route(self, mint, direction="buy", amount=1_000_000):
        if direction == "buy":
            input_mint, output_mint = SOL_MINT, mint
        elif direction == "sell":
            input_mint, output_mint = mint, SOL_MINT
        else:
            return False, ERR_QUOTE_MISMATCH
        quote = self.quote(input_mint, output_mint, amount)
        if quote is None:
            return False, ERR_ROUTE_UNVERIFIED
        if not quote.is_fresh():
            return False, ERR_STALE_QUOTE
        return True, f"route_proven:{quote.source}"


def devnet_pools_from_env():
    """Parse DEVNET_POOLS_JSON (deployment-injected verified devnet
    pool data, mint -> pool entry as in DevnetPoolQuoter). Returns
    a dict, or {} on missing/malformed input — never a guess."""
    raw = (os.environ.get("DEVNET_POOLS_JSON") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def devnet_route_report(pools=None):
    """Read-only devnet route status report (no swap, no signing)."""
    pools = pools if pools is not None else devnet_pools_from_env()
    if not pools:
        return {
            "network": "devnet",
            "route_verified": False,
            "reason": ERR_ROUTE_UNVERIFIED,
            "note": ("No verified devnet pool data injected. Devnet "
                     "routing was NOT verified in the sandbox "
                     "(Jupiter empty reply, DexScreener pairs null "
                     "for devnet mint, mainnet DEX programs not "
                     "executable on devnet). Supply verified devnet "
                     "pool data via DEVNET_POOLS_JSON, or a "
                     "devnet-deployed program's swap transaction, "
                     "and re-run the smoke test."),
        }
    return {
        "network": "devnet",
        "route_verified": True,
        "reason": "route_proven:devnet_pool_injected",
        "mints": sorted(pools.keys()),
        "note": ("Route is proven ONLY for the injected mints, and "
                 "only as far as the deployment verified that pool "
                 "data. This report does not validate a signer and "
                 "does not start trading."),
    }


# ---------------------------------------------------------------------------
# The builder (swap.py tx_builder callable interface)
# ---------------------------------------------------------------------------
class SolanaTxBuilder:
    """Real transaction builder, callable as a swap.py tx_builder:
    (direction, mint, input_amount, min_output, quote) ->
    (ok: bool, error_code_or_None).

    input_amount is in SOL (buys) or token units (sells) for
    logging; the quote's smallest-unit amounts drive the plan
    check. On success, last_signature holds the RPC-returned
    signature (not logged here). On any refusal, no signing and
    no sending happen, and the ledger is left unchanged by the
    caller.
    """

    def __init__(self, network="devnet", rpc_url=None,
                 swap_tx_provider=None, rpc_client=None,
                 expected_pubkey: str = WALLET_PUBKEY,
                 slippage_guard_pct: float = SLIPPAGE_GUARD_PCT,
                 confirm: bool = True):
        self.network = network
        self.rpc_url = rpc_url or (
            "https://api.devnet.solana.com" if network == "devnet"
            else "https://api.mainnet-beta.solana.com")
        self.swap_tx_provider = swap_tx_provider
        self.rpc_client = rpc_client or SolanaRpcClient(self.rpc_url)
        self.expected_pubkey = expected_pubkey
        self.slippage_guard_pct = slippage_guard_pct
        self.confirm = confirm
        self.last_signature = None

    def __call__(self, direction, mint, input_amount, min_output, quote):
        ok, err = self.build_and_send(
            direction, mint, input_amount, min_output, quote)
        return ok, err

    def build_and_send(self, direction, mint, input_amount,
                       min_output, quote):
        # Gate 1-3: live, network, signer marker.
        if not is_live_enabled():
            logger.warning("tx_refused reason=%s", ERR_LIVE_DISABLED)
            return False, ERR_LIVE_DISABLED
        try:
            parse_network(self.network)
        except ValueError:
            logger.warning("tx_refused reason=%s", ERR_INVALID_NETWORK)
            return False, ERR_INVALID_NETWORK
        _marker, signer_err = load_signer_with_status(self.expected_pubkey)
        if signer_err:
            code = signer_err.split(":")[0]
            logger.warning("tx_refused reason=%s", code)
            return False, code
        # Gate 4: quote fresh + mints match the direction.
        if direction == "buy":
            exp_in, exp_out = SOL_MINT, mint
        elif direction == "sell":
            exp_in, exp_out = mint, SOL_MINT
        else:
            return False, ERR_QUOTE_MISMATCH
        if quote is None or not quote.is_fresh():
            logger.warning("tx_refused direction=%s mint=%s reason=%s",
                           direction, mint, ERR_STALE_QUOTE)
            return False, ERR_STALE_QUOTE
        if quote.input_mint != exp_in or quote.output_mint != exp_out:
            logger.warning("tx_refused direction=%s mint=%s reason=%s",
                           direction, mint, ERR_QUOTE_MISMATCH)
            return False, ERR_QUOTE_MISMATCH
        # Gate 5: slippage guard, both ways.
        if float(min_output) < float(quote.output_amount) * (
                1.0 - self.slippage_guard_pct):
            logger.warning("tx_refused direction=%s mint=%s reason=%s",
                           direction, mint, ERR_SLIPPAGE_GUARD)
            return False, ERR_SLIPPAGE_GUARD
        # Gate 6: a swap-transaction provider must exist.
        if self.swap_tx_provider is None:
            logger.warning("tx_refused direction=%s mint=%s reason=%s",
                           direction, mint, ERR_TX_BUILDER_NOT_CONFIGURED)
            return False, ERR_TX_BUILDER_NOT_CONFIGURED
        try:
            plan, plan_err = self.swap_tx_provider(
                direction, mint, quote.input_amount, min_output,
                quote, self.expected_pubkey)
        except Exception:  # noqa: BLE001 - provider failure = refuse
            return False, ERR_ROUTE_UNVERIFIED
        if plan is None:
            return False, plan_err or ERR_ROUTE_UNVERIFIED
        if (plan.get("input_mint") != exp_in
                or plan.get("output_mint") != exp_out):
            return False, ERR_TX_PLAN_MISMATCH
        expected_output = float(plan.get("expected_output", 0) or 0)
        if expected_output < float(min_output):
            return False, ERR_SLIPPAGE_GUARD
        serialized = plan.get("serialized_tx_b64")
        if not serialized:
            return False, ERR_TX_PLAN_MISMATCH
        # Sign (transient seed, dropped immediately afterwards).
        seed, seed_err = load_signing_seed(self.expected_pubkey)
        if seed is None:
            return False, seed_err or ERR_NO_SIGNER
        try:
            signed_b64 = sign_serialized_tx(
                serialized, seed, self.expected_pubkey)
        except ValueError as exc:
            code = str(exc)
            return False, (code if code in (
                ERR_FEE_PAYER_MISMATCH,) else ERR_TX_PLAN_MISMATCH)
        finally:
            seed = None
        # Send, then (optionally) confirm. A send is never logged
        # with its signature here; the caller holds it as a value.
        logger.info("tx_intent direction=%s mint=%s input=%s "
                    "min_output=%s wallet=%s",
                    direction, mint, input_amount, min_output,
                    self.expected_pubkey)
        signature, send_err = self.rpc_client.send_transaction(signed_b64)
        if send_err or not signature:
            return False, send_err or ERR_TX_SEND_FAILED
        self.last_signature = signature
        if self.confirm:
            confirmed, confirm_err = self.rpc_client.confirm_transaction(
                signature)
            if not confirmed:
                return False, confirm_err or ERR_TX_CONFIRM_FAILED
        return True, None
