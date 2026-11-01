"""SolBot LIVE 5-bot trading engine — DISABLED mode only.

This module contains the trading logic for the previous 5-bot screening
team (Screener, Analyzer, Secondary Screening, Executor, Risk Management)
for Solana mainnet, as settled by James on 2026-10-31.

HARD SAFETY RULES (do not weaken these without James's explicit say-so):
  * LIVE is OFF by default. See is_live_enabled().
  * No private key or seed phrase is ever contained in, logged by, printed
    by, or accepted in chat/code/repo/files by this engine. The signing key
    comes ONLY from an environment variable set by James himself in the
    deployment environment — either TRADING_KEYPAIR (JSON array of 64
    integers, the original form) or TRADING_PRIVATE_KEY (base58 string,
    exactly as Phantom exports it). If both are set, signing is refused
    (ambiguous). If neither is set, signing is refused. Any supplied key
    is validated by deriving its public key and checking it matches the
    settled live wallet; a mismatch is refused, and no key material is
    ever included in any error, log, or output.
  * No leverage. No coin over 20% of bankroll. No buy without a full
    pipeline pass. No trade on a stale or missing price (fail-closed).
  * Halt check runs on every step. On halt, ALL positions are sold, the
    proceeds park in USDC, and buying stops until James restarts it.

Currency: positions and cash are tracked in USDC (the trading currency).
Portfolio value is converted to CAD for the halt check only, using
USD_PER_CAD (default 0.712, labelled approximate — see RULES.md). The
halt threshold is $20 CAD against a $85 CAD starting bankroll.

This file performs NO network access, NO signing, and NO sending. The
separate dry_run.py script performs read-only public price/quote reads
only. Nothing in this build starts a persistent loop.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Settled spec (James, 2026-10-31) — these are HIS numbers.
# ---------------------------------------------------------------------------
WALLET_PUBKEY = "9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ"
START_BANKROLL_CAD = 85.0
HALT_BANKROLL_CAD = 20.0
BUY_PCT_OF_BANKROLL = 0.20
MAX_OPEN_POSITIONS = 5

# Pipeline (SolBot Oct 30 chain, as corrected by James's live rules today).
SCREENER_WINDOW_SECONDS = 30
SCREENER_MIN_GAIN_30S = 0.02          # >= +2% in 30s
ANALYST_CONFIRM_SECONDS = 5           # confirm within 5s, gained again
HARD_STOP_PCT = 0.07                  # -7% hard stop
TP_SLOW_MIN = 0.10                    # slow market: sell at +10%..+30%
TP_SLOW_MAX = 0.30
TP_FAST_WINDOW_LOW = 0.31             # fast: hold to 31%..50% window
TP_FAST_WINDOW_HIGH = 0.50
TP_FAST_PEAK_DROP = 0.08              # sell on 8% drop from peak in window
TP_ABOVE_50_PEAK_DROP = 0.12          # sell on 12% drop past +50%
TP_CAP = 10.00                        # +1000% cap

# Builder defaults (NOT James's numbers — change in config before any
# go-ahead if he wants different values). See RULES.md.
MAX_PRICE_AGE_SECONDS = 30            # stale-price cutoff for buys
LIQUIDITY_FLOOR_USD = 10_000.0        # prior Analyzer floor, carried over
SELLER_DOMINANCE_RATIO = 1.2          # sellers >= 1.2x buyers = scrap/sell
CRASH_WATCH_COIN_PCT = 0.40           # >=40% of tracked coins...
CRASH_WATCH_DROP_1H = 0.05            # ...down >=5% in 1h = crash watch
SPIKE_MAX_GAIN_30S = 5.00             # >+500% in 30s treated as bad data

# CAD conversion: USD per CAD. Default 0.712 is the rate used for James's
# earlier CAD paper conversions (2026-10-28). It is APPROXIMATE, not a
# live rate. dry_run.py fetches a live rate when available and labels it.
USD_PER_CAD_DEFAULT = 0.712


# ---------------------------------------------------------------------------
# Base58 + Ed25519 public-key derivation (pure Python, no dependencies).
# Used ONLY to validate that a supplied key belongs to the settled live
# wallet. Key bytes are handled inside these helpers and never returned
# to callers, logged, or printed by load_signer*.
# ---------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def base58_encode(data: bytes) -> str:
    """Encode bytes as base58 (Bitcoin alphabet, as Phantom exports)."""
    n = int.from_bytes(data, "big") if data else 0
    if n == 0:
        return "1" * len(data)
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    # Leading zero bytes become leading '1's.
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def base58_decode(text: str) -> bytes:
    """Decode a base58 string. Raises ValueError on bad characters."""
    if not text:
        raise ValueError("empty base58 string")
    n = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise ValueError("invalid base58 character")
        n = n * 58 + _B58_INDEX[ch]
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in text:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full


# Ed25519 (RFC 8032) — public-key derivation from a 32-byte seed only.
_ED_Q = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_Q - 2, _ED_Q) % _ED_Q
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = x * _ED_I % _ED_Q
    if x % 2 != 0:
        x = _ED_Q - x
    return x


_ED_BY = 4 * pow(5, _ED_Q - 2, _ED_Q) % _ED_Q
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = [_ED_BX % _ED_Q, _ED_BY % _ED_Q]


def _ed_add(point_p, point_q):
    x1, y1 = point_p
    x2, y2 = point_q
    denom = pow(1 + _ED_D * x1 * x2 * y1 * y2 % _ED_Q, _ED_Q - 2, _ED_Q)
    x3 = (x1 * y2 + x2 * y1) * denom % _ED_Q
    denom2 = pow(1 - _ED_D * x1 * x2 * y1 * y2 % _ED_Q, _ED_Q - 2, _ED_Q)
    y3 = (y1 * y2 + x1 * x2) * denom2 % _ED_Q
    return [x3, y3]


def _ed_scalarmult(point, scalar: int):
    if scalar == 0:
        return [0, 1]
    half = _ed_scalarmult(point, scalar // 2)
    half = _ed_add(half, half)
    if scalar & 1:
        half = _ed_add(half, point)
    return half


def ed25519_publickey_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte Ed25519 public key from a 32-byte seed."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    hashed = bytearray(hashlib.sha512(seed).digest())
    hashed[0] &= 248
    hashed[31] &= 127
    hashed[31] |= 64
    scalar = int.from_bytes(hashed[:32], "little")
    point = _ed_scalarmult(_ED_BASE, scalar)
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def publickey_bytes_from_secret(secret: bytes) -> bytes:
    """Public key for a secret-key byte string.

    Accepts a 64-byte Solana secret (32-byte seed + 32-byte public key,
    the form Phantom exports as base58 and JSON arrays carry) or a
    32-byte seed alone. For 64-byte secrets, the embedded public key
    must match the seed-derived public key, otherwise the secret is
    malformed and ValueError is raised.
    """
    if len(secret) == 64:
        derived = ed25519_publickey_from_seed(secret[:32])
        if derived != secret[32:]:
            raise ValueError("secret key public-key half does not match seed")
        return secret[32:]
    if len(secret) == 32:
        return ed25519_publickey_from_seed(secret)
    raise ValueError("secret key must be 32 or 64 bytes")


def is_live_enabled() -> bool:
    """Live gate. Default OFF. Only env LIVE_ENABLED=1 enables trading.

    This is a code gate only. Funding confirmation and James's final
    explicit go-ahead are separate procedural gates (see README.md).
    """
    return os.environ.get("LIVE_ENABLED", "0").strip() == "1"


def load_signer_with_status(expected_pubkey: str = WALLET_PUBKEY):
    """Validate the configured signing key and return (marker, error).

    Key sources, in order of checking (never both at once):
      * TRADING_KEYPAIR — JSON array of 64 integers (original form).
      * TRADING_PRIVATE_KEY — base58 string, exactly as Phantom exports
        it (64-byte secret, or 32-byte seed, base58-encoded).

    If both are set, returns (None, "both_keys_set") — the engine
    refuses to run rather than silently preferring one. If neither is
    set, returns (None, "no_key_set"). If a key is malformed, returns
    (None, "invalid_key_format"). If the key's derived public key does
    not match ``expected_pubkey`` (default: the settled live wallet),
    returns (None, "pubkey_mismatch").

    Error strings are fixed codes plus a generic explanation; they NEVER
    contain any key material, fragment, or derived secret. On success
    the marker is opaque (source name + validated public key, which is
    public information) and contains no key bytes. The actual signing
    implementation lives in the deployment environment, not in this
    file; this disabled build never signs anything.
    """
    if not is_live_enabled():
        return None, "live_disabled"
    raw_pair = (os.environ.get("TRADING_KEYPAIR") or "").strip()
    raw_b58 = (os.environ.get("TRADING_PRIVATE_KEY") or "").strip()
    if raw_pair and raw_b58:
        return None, (
            "both_keys_set: set only one of TRADING_KEYPAIR or "
            "TRADING_PRIVATE_KEY, not both"
        )
    if not raw_pair and not raw_b58:
        return None, "no_key_set"
    secret = None
    source = ""
    try:
        if raw_pair:
            source = "TRADING_KEYPAIR"
            arr = json.loads(raw_pair)
            if not isinstance(arr, list) or len(arr) != 64:
                return None, "invalid_key_format"
            if not all(isinstance(x, int) and 0 <= x <= 255 for x in arr):
                return None, "invalid_key_format"
            secret = bytes(arr)
        else:
            source = "TRADING_PRIVATE_KEY"
            secret = base58_decode(raw_b58)
        derived_b58 = base58_encode(publickey_bytes_from_secret(secret))
    except (ValueError, json.JSONDecodeError):
        return None, "invalid_key_format"
    finally:
        # Key bytes must not escape this function in any form.
        secret = None
    if derived_b58 != expected_pubkey:
        return None, "pubkey_mismatch"
    return {
        "signer": "key present and validated (contents never logged)",
        "source": source,
        "validated_pubkey": expected_pubkey,
    }, None


def load_signer(expected_pubkey: str = WALLET_PUBKEY):
    """Return a signer marker ONLY if live is enabled AND a key is set
    and validated against the settled live wallet (see
    load_signer_with_status for the detailed refusal reason). Returns
    None in every other case. Never exposes key bytes."""
    marker, _error = load_signer_with_status(expected_pubkey)
    return marker


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
@dataclass
class Position:
    mint: str
    entry_price_usd: float
    invested_usd: float
    tokens: float
    peak_price_usd: float
    entered_at: float = 0.0  # monotonic/step timestamp supplied by caller

    def gain(self, price_usd: float) -> float:
        if self.entry_price_usd <= 0:
            return 0.0
        return price_usd / self.entry_price_usd - 1.0

    def peak_gain(self) -> float:
        if self.entry_price_usd <= 0:
            return 0.0
        return self.peak_price_usd / self.entry_price_usd - 1.0

    def value_usd(self, price_usd: float) -> float:
        return self.tokens * price_usd


@dataclass
class Portfolio:
    cash_usdc: float
    positions: dict = field(default_factory=dict)  # mint -> Position
    halted: bool = False
    halt_reason: str = ""
    sell_failures: list = field(default_factory=list)

    def total_value_usd(self, prices_usd: dict) -> float:
        total = self.cash_usdc
        for mint, pos in self.positions.items():
            price = prices_usd.get(mint)
            if price is None:
                # Fail-closed for valuation too: a missing price is valued
                # at entry (never at an invented price), so a halt cannot
                # be hidden by a missing feed.
                price = pos.entry_price_usd
            total += pos.value_usd(price)
        return total

    def total_value_cad(self, prices_usd: dict,
                        usd_per_cad: float = USD_PER_CAD_DEFAULT) -> float:
        if usd_per_cad <= 0:
            raise ValueError("usd_per_cad must be positive")
        return self.total_value_usd(prices_usd) / usd_per_cad


def buy_size_usd(portfolio_value_usd: float) -> float:
    """Exactly 20% of total current bankroll, compounding with the pot."""
    return BUY_PCT_OF_BANKROLL * portfolio_value_usd


def should_halt(portfolio_value_cad: float) -> bool:
    """Halt at $20 CAD or below. Exact $20.00 triggers; above does not."""
    return portfolio_value_cad <= HALT_BANKROLL_CAD


# ---------------------------------------------------------------------------
# Pricing: fail-closed
# ---------------------------------------------------------------------------
def price_is_fresh(price_usd, age_seconds) -> bool:
    """A price is usable only if present, positive, and fresh."""
    if price_usd is None or age_seconds is None:
        return False
    try:
        price = float(price_usd)
        age = float(age_seconds)
    except (TypeError, ValueError):
        return False
    return price > 0 and age <= MAX_PRICE_AGE_SECONDS


# ---------------------------------------------------------------------------
# Pipeline pass (Screener -> Analyzer -> Secondary -> Risk safeguards)
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    mint: str
    gain_30s: float            # Screener: real 30s gain
    gained_again_5s: bool      # Analyst: gained again within 5s
    still_gaining: bool        # Secondary: still gaining at check
    liquidity_usd: float
    buys: float = 0.0
    sells: float = 0.0
    price_usd: float = 0.0
    price_age_seconds: float = 0.0


def pipeline_pass(candidate: Candidate, crash_watch_active: bool = False) -> bool:
    """True only on a full pass. Any failure = no buy, no exception."""
    if crash_watch_active:
        return False
    if not price_is_fresh(candidate.price_usd, candidate.price_age_seconds):
        return False
    if candidate.gain_30s < SCREENER_MIN_GAIN_30S:
        return False
    if candidate.gain_30s > SPIKE_MAX_GAIN_30S:
        return False  # spike floor: absurd jump = bad data, fail closed
    if not candidate.gained_again_5s:
        return False
    if not candidate.still_gaining:
        return False
    if candidate.liquidity_usd < LIQUIDITY_FLOOR_USD:
        return False
    # Seller-dominance scrap: sellers >= 1.2x buyers.
    if candidate.buys > 0 and candidate.sells >= SELLER_DOMINANCE_RATIO * candidate.buys:
        return False
    return True


def crash_watch_active(coins_down_1h: int, coins_tracked: int) -> bool:
    """Crash watch: >=40% of tracked coins down >=5% in 1h (caller counts
    only coins meeting the drop; see RULES.md)."""
    if coins_tracked <= 0:
        return False
    return (coins_down_1h / coins_tracked) >= CRASH_WATCH_COIN_PCT


def can_buy(portfolio: Portfolio, candidate: Candidate,
            live_enabled: bool = False) -> bool:
    if not live_enabled:
        return False
    if portfolio.halted:
        return False
    if len(portfolio.positions) >= MAX_OPEN_POSITIONS:
        return False
    if candidate.mint in portfolio.positions:
        return False  # one position per coin
    return pipeline_pass(candidate)


def buy(portfolio: Portfolio, candidate: Candidate,
        portfolio_value_usd: float, live_enabled: bool = False):
    """Execute a paper-state buy (no chain interaction in this build).

    Returns the new Position, or None if buying is not allowed. The
    caller (live deployment) is responsible for the actual swap; this
    disabled build only models the ledger.
    """
    if not can_buy(portfolio, candidate, live_enabled=live_enabled):
        return None
    size = buy_size_usd(portfolio_value_usd)
    if size <= 0 or size > portfolio.cash_usdc:
        return None
    tokens = size / candidate.price_usd
    pos = Position(mint=candidate.mint, entry_price_usd=candidate.price_usd,
                   invested_usd=size, tokens=tokens,
                   peak_price_usd=candidate.price_usd)
    portfolio.positions[candidate.mint] = pos
    portfolio.cash_usdc -= size
    return pos


# ---------------------------------------------------------------------------
# Sell rules (Risk Management)
# ---------------------------------------------------------------------------
def evaluate_sell(position: Position, price_usd: float,
                  price_age_seconds, market_fast: bool = False):
    """Return a sell reason string, or None to hold.

    Boundaries (inclusive where stated, so tests can pin them):
      * hard stop: price <= entry * (1 - 7%)  -> sell
      * cap: gain >= +1000%                  -> sell
      * past +50% peak: price <= peak * (1 - 12%) -> sell
      * 31%..50% peak window: price <= peak * (1 - 8%) -> sell
      * slow market: gain >= +10%            -> sell (TP window 10-30%;
        a slow gain above 30% is handled by the peak-drop rules)
    Stale/missing price -> None (fail-closed: never sell on bad data,
    except the halt path, which uses its own retry/reporting logic).
    """
    if not price_is_fresh(price_usd, price_age_seconds):
        return None
    if position.peak_price_usd < price_usd:
        position.peak_price_usd = price_usd
    gain = position.gain(price_usd)
    peak_gain = position.peak_gain()

    # 1e-9 tolerance so exactly -7% (which is not exactly representable
    # in binary floating point) still triggers the inclusive boundary.
    if gain <= -HARD_STOP_PCT + 1e-9:
        return "hard_stop_-7%"
    if gain >= TP_CAP:
        return "take_profit_cap_+1000%"
    if peak_gain > TP_FAST_WINDOW_HIGH:
        if price_usd <= position.peak_price_usd * (1.0 - TP_ABOVE_50_PEAK_DROP):
            return "peak_drop_12%_past_+50%"
        return None
    if peak_gain >= TP_FAST_WINDOW_LOW:
        if price_usd <= position.peak_price_usd * (1.0 - TP_FAST_PEAK_DROP):
            return "peak_drop_8%_in_31-50%_window"
        return None
    if not market_fast and gain >= TP_SLOW_MIN:
        return "take_profit_slow_+10-30%"
    return None


def sell_all_on_halt(portfolio: Portfolio, prices_usd: dict,
                     price_provider):
    """Sell ALL positions on halt, park proceeds in USDC, stop buying.

    price_provider(mint) -> (price_usd, age_seconds); it may raise or
    return a stale price. Each position is attempted, retried once on
    failure, and every failure is recorded in portfolio.sell_failures
    (reported loudly by the caller — never silently ignored). A failed
    position is KEPT (not deleted) so the failure is visible, and the
    portfolio stays halted either way, so no new buys can follow.
    Returns (sold_mints, failed_mints).
    """
    portfolio.halted = True
    portfolio.halt_reason = "halt_at_$20_CAD"
    sold, failed = [], []
    for mint in list(portfolio.positions.keys()):
        pos = portfolio.positions[mint]
        proceeds = None
        last_error = ""
        for attempt in range(2):  # first try + one retry
            try:
                price_usd, age = price_provider(mint)
            except Exception as exc:  # noqa: BLE001 - must log, not crash
                last_error = str(exc)
                continue
            if not price_is_fresh(price_usd, age):
                last_error = "stale_or_missing_price"
                continue
            proceeds = pos.value_usd(float(price_usd))
            break
        if proceeds is None:
            failed.append(mint)
            portfolio.sell_failures.append(
                {"mint": mint, "error": last_error})
            continue
        portfolio.cash_usdc += proceeds
        del portfolio.positions[mint]
        sold.append(mint)
    return sold, failed


def check_halt(portfolio: Portfolio, prices_usd: dict,
               usd_per_cad: float = USD_PER_CAD_DEFAULT) -> bool:
    """Halt check for every step. Returns True if halted (new or existing)."""
    if portfolio.halted:
        return True
    if should_halt(portfolio.total_value_cad(prices_usd, usd_per_cad)):
        portfolio.halted = True
        portfolio.halt_reason = "halt_at_$20_CAD"
        return True
    return False
