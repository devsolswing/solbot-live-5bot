# SolBot LIVE 5-bot — Single Rulebook (DISABLED build)

**Status: DISABLED. No real trades, no real key, no mainnet transactions
in this build.** This file is the single rulebook for the live build. Where
any older rulebook, SolBot copy, or Python engine copy conflicts with this
file, **this file wins for the live build**, because it records James's
live rules settled on 2026-10-31.

## 1. Team, network, wallet (James, 2026-10-31)

- Team: the **previous 5-bot screening team only** for live trading:
  Screener → Analyzer → Secondary Screening → Executor → Risk Management.
  The new Phantom 3-bot team is NOT live in this build; it stays paper in
  SolBot.
- Network: **Solana mainnet**, autonomous (no per-trade approval tap).
- Wallet: **separate wallet only**, pubkey
  `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`.
  Never the main Phantom wallet, never game treasury, never launch funds,
  never the SolSwing bot bankroll. Mainnet balance checked 2026-10-31:
  0.00158242 SOL; bankroll **not yet funded** at build time.
- This build does not hold a key for that wallet (see §6).

## 2. Money (James, 2026-10-31)

- Starting bankroll: **$85 CAD** worth.
- **Everything in USDC.** Buys pay USDC, sells park in USDC, cash sits in
  USDC. SOL in the wallet is **only for transaction fees**.
- Buy size: **exactly 20% of total current bankroll per coin** (total =
  USDC cash + market value of open positions). Size compounds: a bigger
  pot means bigger buys, a smaller pot means smaller buys. This supersedes
  the SolBot Oct 30 chain's 10% buy size.
- Max **5 open positions**, **one position per coin**.
- **No leverage. No coin over 20% of bankroll. No buy without a full
  pipeline pass.**

## 3. Pipeline (SolBot Oct 30 chain, corrected by §2)

1. **Screener** — scans trenches every **30 seconds** for real 30-second
   gains **>= +2%**.
2. **Analyst** — confirms **within 5 seconds**, only if the coin gained
   again in that window.
3. **Secondary Screening** — passes only coins **still gaining** at check.
4. **Executor** — the only buyer, and only of full-pass coins, at 20%.
5. **Risk Management** — every sell rule in §4, plus safeguards in §5.

## 4. Sell rules (Risk Management)

- **Hard stop: −7%** from entry. Sells 100% of the position immediately.
  This bypasses any cooldown or confirmation.
- **Slow market take profit: +10% to +30%.** In a slow market, sell at
  +10% (the first take-profit level).
- **Fast market: hold to the 31%–50% window.** In that window, sell on an
  **8% drop from the peak**.
- **Past +50% peak: sell on a 12% drop from the peak.**
- **Cap: +1000%.** Sell at +1000% gain regardless of window.
- **Every sell parks proceeds in USDC.** No conversion back to SOL on exit.

"Slow" vs "fast" is a per-position market-speed input to the sell
evaluator (see engine.py `market_fast`). The live deployment must feed
it from the coin's recent momentum; it must never be defaulted to
"slow" silently to force an early sale, nor to "fast" to delay one.
If speed cannot be determined, the deployment must fail closed (hold
under the hard stop only, and log the missing input).

## 5. Safeguards (fail-closed)

- **Stale/missing price = no trade.** No buy and no (non-halt) sell on a
  price older than 30 seconds or missing entirely. Never invent a price.
- **Spike floor:** a 30-second gain above +500% is treated as bad data
  and blocks the buy (builder default — James has not set a live spike
  number).
- **Liquidity floor:** builder default **$10,000** (the prior Analyzer
  floor, carried over). James has not set a live liquidity number; change
  it in config before any go-ahead if he wants a different floor.
- **Seller-dominance scrap:** sellers >= **1.2× buyers** blocks/scraps the
  coin (builder default from the prior Risk tightening).
- **Crash watch:** if **>=40% of tracked coins are down >=5% in 1 hour**,
  no new buys (builder default from the prior crash-watch tightening).
  A crash-watch trip does not by itself force-sell existing positions;
  existing positions still follow §4.
- **One position per coin, max 5 open** (§2) is enforced on every buy.

## 6. Halt (James, 2026-10-31)

- **Halt threshold: total portfolio value falls to $20 CAD.**
  Exact $20.00 CAD triggers; above $20.00 CAD does not.
- Portfolio value for the halt is measured in **CAD**: USD value divided
  by the USD-per-CAD rate (default 0.712, approximate — see §7).
- On halt: **sell ALL positions, park everything in USDC, and STOP.**
  No new buys until James explicitly restarts it.
- Every halt sell is attempted and **retried once** on failure. A failure
  is recorded and reported loudly; the failed position is kept visible
  (not deleted), the portfolio stays halted, and buying stays blocked.
  A single failed sell must never silently allow trading to continue.

## 7. CAD conversion (approximate, not exact)

- Starting bankroll $85 CAD and halt $20 CAD are CAD figures; trading
  itself happens in USDC. Conversion uses a USD-per-CAD rate.
- Default rate in code: **0.712 USD per CAD** (the rate used for James's
  earlier CAD paper conversions, 2026-10-28). It is a fallback only.
- The live deployment must fetch a live rate when available and label
  the source in its logs. The CAD value of the portfolio — and therefore
  the exact moment the $20 CAD halt trips — is **approximate**, because
  the rate moves. Never present a CAD halt figure as exact to the dollar
  without stating the rate used.

## 8. Key handling — CRITICAL (never weaken)

- The engine **never contains, logs, prints, or accepts** a private key
  or seed phrase in chat, code, repo, or files.
- The signing key comes **only** from an environment variable set by
  **James himself** in the deployment environment — the same pattern
  as the existing Railway adjudicator key. Exactly one of these two
  forms, never both:
  - `TRADING_PRIVATE_KEY` — a **base58 private-key string, exactly as
    Phantom exports it** (Phantom app → wallet → export private key;
    paste the exported string into the Railway variable, no conversion
    step on James's side). This is the recommended form for James,
    because it is the form Phantom gives him.
  - `TRADING_KEYPAIR` — a **JSON array of 64 integers** (the original
    form; the same key, in array form).
- If **both** are set, the engine **refuses to run** (ambiguous) and
  reports `both_keys_set` — it never silently prefers one.
- Any supplied key is **validated before any signing**: the engine
  derives its public key (pure-Python Ed25519, no dependency) and
  checks it matches the settled live wallet (§1). A mismatch is
  refused (`pubkey_mismatch`) and reported **without printing any key
  material**. A malformed key is refused (`invalid_key_format`).
- If **no** key variable is set, the engine **refuses to sign and stays
  disabled**.
- **Do not generate a real keypair for mainnet use.** Any keypair created
  for unit tests is test-only, in-memory only, never written to a file,
  and never used for mainnet.
- The wallet's public key (§1) is public information and may be shared;
  the private key / seed phrase must never be shared with anyone,
  including in chat with this assistant.

## 9. Live gate — DISABLED by default

- Code gate: environment variable `LIVE_ENABLED` must equal `1` for any
  trade path to run. **Default is off (absent = off).** Even with a key
  present, `LIVE_ENABLED` off means no trading.
- Procedural gates (separate from the code gate, all still outstanding
  at build time):
  1. **Funding confirmation** — the $85 CAD bankroll (USDC on Solana,
     plus a little SOL for fees) actually landed in wallet §1.
  2. **Key set by James** in the deployment environment (§8).
  3. **Final explicit go-ahead from James** after funding is confirmed.
- Until all three are done, this build stays disabled. Enabling the code
  gate alone does **not** authorise a trade.

## 10. What this build does NOT do

- No persistent loop is started by this build.
- No mainnet transaction is sent, signed, or funded by this build.
- No live Jupiter swap execution is verified by this build (see
  README.md: read-only quote/price checks only; swap execution itself
  is unverified in this sandbox and must be proven in the deployment
  environment before any go-ahead).
- Paper SolBot records are untouched by this build.
