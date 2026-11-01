# SolBot LIVE 5-bot — Single Rulebook (DISABLED build)

**Status: DISABLED. No real trades, no real key, no transactions
in this build.** This file is the single rulebook for the live build. Where
any older rulebook, SolBot copy, or Python engine copy conflicts with this
file, **this file wins for the live build**, because it records James's
live rules settled on 2026-10-31.

## 0. SUPERSEDED — stablecoin rules removed (James, 2026-10-31)

James ordered, 2026-10-31: **"scrap the USDC, go straight to SOL."**
That order supersedes every earlier stablecoin rule in this build.
The previous rules — "everything in USDC", "buys pay USDC", "sells park
in USDC", the mainnet and devnet stablecoin mints, and stablecoin
funding/faucet steps — are **removed from the live rules** and must not
be followed. This paragraph is history only, not a live instruction:
no stablecoin mint, conversion, or parking remains anywhere in the
live engine, dry run, or rules below. If any older copy still shows
stablecoin rules, this file wins.

## 1. Team, network, wallet (James, 2026-10-31)

- Team: the **previous 5-bot screening team only** for live trading:
  Screener → Analyzer → Secondary Screening → Executor → Risk Management.
  The new Phantom 3-bot team is NOT live in this build; it stays paper in
  SolBot.
- Network: **DEVNET FIRST (James, 2026-10-31, reversing his earlier
  mainnet-straight-away order).** Launch on Solana devnet, prove it
  there, and go to mainnet only after devnet is proven and James gives
  a separate mainnet go-ahead. See §11 for the devnet section.
  Network is selected by the `NETWORK` environment variable
  (`devnet` | `mainnet`; unset = mainnet, the original behaviour).
  The mainnet code path is unchanged when `NETWORK=mainnet`.
- Trading is autonomous (no per-trade approval tap) in either network
  once the live gates in §9 are met for that network.
- Wallet: **separate wallet only**, pubkey
  `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`.
  Never the main Phantom wallet, never game treasury, never launch funds,
  never the SolSwing bot bankroll. Mainnet balance checked 2026-10-31:
  0.00158242 SOL; bankroll **not yet funded** at build time.
- This build does not hold a key for that wallet (see §6).

## 2. Money — SOL ONLY (James, 2026-10-31)

- Starting bankroll: **$85 CAD equivalent in SOL** (see §7 for how the
  SOL amount is computed from the live SOL price — no SOL amount is
  hard-coded as James's number).
- **Everything in SOL.** Buys are SOL -> token, sells are token -> SOL,
  cash sits in SOL. **There is no stablecoin anywhere in this build.**
- Transaction fees are paid in SOL from the same wallet. The deployment
  must keep enough SOL aside for fees and must not count a fee reserve
  as trading cash, or buys could fail for lack of fee SOL.
- Buy size: **exactly 20% of total current bankroll per coin** (total =
  SOL cash + SOL market value of open positions). Size compounds: a bigger
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
- **Every sell parks proceeds in SOL.** No conversion to any other
  currency on exit.

"Slow" vs "fast" is a per-position market-speed input to the sell
evaluator (see engine.py `market_fast`). The live deployment must feed
it from the coin's recent momentum; it must never be defaulted to
"slow" silently to force an early sale, nor to "fast" to delay one.
If speed cannot be determined, the deployment must fail closed (hold
under the hard stop only, and log the missing input).

## 5. Safeguards (fail-closed)

- **Stale/missing price = no trade.** No buy and no (non-halt) sell on a
  price older than 30 seconds or missing entirely. Never invent a price.
- **Stale/missing SOL price = trading blocked.** The halt is computed
  from the SOL portfolio via the live SOL price (§6). If that SOL price
  is stale (>30s) or missing, the engine raises instead of returning a
  value — the deployment must treat that as trading blocked, never as
  "portfolio is above halt".
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

## 6. Halt (James, 2026-10-31, adapted to SOL-only)

- **Halt threshold: total portfolio value falls to $20 CAD.**
  Exact $20.00 CAD triggers; above $20.00 CAD does not.
- Portfolio value for the halt is measured in **CAD**, computed from
  the SOL-denominated portfolio: (SOL cash + SOL value of positions) ×
  live SOL price in USD ÷ USD-per-CAD rate (default 0.712, approximate
  — see §7). A stale or missing SOL price raises (fail-closed, §5)
  instead of returning a value.
- On halt: **sell ALL positions, park everything in SOL, and STOP.**
  No new buys until James explicitly restarts it.
- Every halt sell is attempted and **retried once** on failure. A failure
  is recorded and reported loudly; the failed position is kept visible
  (not deleted), the portfolio stays halted, and buying stays blocked.
  A single failed sell must never silently allow trading to continue.
- **Consequence of scrapping the stablecoin, stated plainly:** the
  portfolio now stays exposed to SOL price moves even between trades.
  With no stablecoin park, a SOL price drop alone — with no position
  losing at all — moves the portfolio toward the $20 CAD halt, and a
  SOL rise alone moves it away. The halt can therefore trip on SOL
  movement, not only on trading losses. That is the direct consequence
  of going straight to SOL.

## 7. CAD conversion (approximate, not exact)

- Starting bankroll $85 CAD and halt $20 CAD are CAD figures; trading
  itself happens in SOL. Conversion method (no SOL amount is James's
  number): SOL amount = CAD amount × USD-per-CAD rate ÷ live SOL price
  in USD. Example at a live rate of 0.7143 USD per CAD: $85 CAD ≈
  $60.71 USD, and $20 CAD ≈ $14.29 USD; divide each by the live SOL
  price in USD to get the SOL figures at that moment. Both rates move,
  so the SOL figures move with them — never present a SOL bankroll or
  halt figure as fixed.
- Default rate in code: **0.712 USD per CAD** (the rate used for James's
  earlier CAD paper conversions, 2026-10-28). It is a fallback only.
- The live deployment must fetch a live SOL price and a live CAD rate
  when available and label the sources in its logs. The CAD value of
  the portfolio — and therefore the exact moment the $20 CAD halt
  trips — is **approximate**, because both rates move. Never present a
  CAD halt figure as exact to the dollar without stating the rates used.

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
  at build time, per network):
  1. **Funding confirmation** — the bankroll (§2 / §11) actually landed
     in wallet §1 on that network, plus enough SOL for fees (same
     wallet, same currency — see §2).
  2. **Key set by James** in the deployment environment (§8).
  3. **Final explicit go-ahead from James** after funding is confirmed
     — a devnet go-ahead authorises devnet only; mainnet needs its own
     separate go-ahead later.
- Until all three are done for that network, this build stays disabled.
  Enabling the code gate alone does **not** authorise a trade.

## 10. What this build does NOT do

- No persistent loop is started by this build.
- No transaction is sent, signed, or funded by this build, on devnet
  or mainnet.
- No live swap execution is verified by this build on either network
  (see README.md: read-only price checks only; swap execution itself
  is unverified in this sandbox and must be proven in the deployment
  environment before any go-ahead — and on devnet, even routing is
  unverified, see §11).
- Paper SolBot records are untouched by this build.

## 11. DEVNET FIRST (James, 2026-10-31 — supersedes mainnet-first)

James chose: **launch on devnet first, mainnet later after devnet is
proven.** This section is the devnet rulebook; §§2–8 apply unchanged
on devnet except where this section says otherwise.

- **Network switch:** `NETWORK=devnet` in the deployment environment
  (Railway Variables). `NETWORK=mainnet` (or unset) keeps the exact
  original mainnet behaviour. An invalid `NETWORK` value fails closed
  (engine refuses to select a network) — a typo must never silently
  pick a network.
- **Cluster / RPC:** Solana devnet, public RPC
  `https://api.devnet.solana.com`.
- **Devnet funding is SOL only (supersedes the old stablecoin faucet
  step — see §0):** devnet SOL from the Solana faucet
  (faucet.solana.com, or `solana airdrop` on devnet) to wallet §1.
  No other funding token is needed or used. Never send real mainnet
  funds to a devnet address, and never send devnet tokens anywhere
  expecting real value.
- **Wallet:** the same public wallet as §1,
  `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ` — the same keypair
  works on both networks, but devnet balances are separate and
  **worthless**. Devnet balance checked 2026-10-31: 0 devnet SOL in
  that wallet (mainnet 0.00158242 SOL is a different balance).
- **Test bankroll / halt — ASSUMPTION for James to correct, NOT his
  words:** the same numbers as mainnet, now in SOL — start **$85 CAD
  equivalent in SOL**, halt at **$20 CAD equivalent**, both converted
  at the live SOL price at funding/test time by the §7 method (no SOL
  amount is hard-coded). Devnet funds are worthless faucet tokens, so
  these CAD figures are a rule-test scale only, not real money at risk.
  If James wants different devnet numbers, he says so and this section
  is corrected — do not silently carry mainnet money figures into a
  devnet test as if they were real.
- **Devnet DEX routing — UNVERIFIED, fail closed:** in this sandbox a
  read-only Jupiter v6 quote returned no route for a devnet swap.
  Jupiter's current Swap/Ultra APIs are mainnet-focused. Before any
  devnet live test, the deployment MUST prove a devnet SOL swap route
  — an Orca/Raydium devnet pool, a direct DEX instruction, or a devnet
  test token with a real pool — and the engine must trade **only** on
  a proven route. If no route can be proven, the devnet test stops at
  funding + wallet + halt/sizing verification, and trenches trading on
  devnet must NOT be presented as proven or working.
- **Devnet go-ahead gates (all outstanding at build time):**
  1. Railway **devnet** service deployed from the repo, with
     `NETWORK=devnet`.
  2. Faucet SOL funding landed in wallet §1 on devnet.
  3. Key set by James himself in the Railway devnet service (§8) —
     the same keypair, validated against the same pubkey.
  4. **Devnet go-ahead from James** (explicit, after funding is
     confirmed), then `LIVE_ENABLED=1` + a devnet smoke test in
     Railway proving a SOL swap route before the first devnet trade.
  A devnet go-ahead does NOT authorise mainnet. Mainnet still needs
  its own funding confirmation, key check, and final go-ahead later.
