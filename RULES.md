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

- No persistent loop is started by the image by default. A loop
  entrypoint now exists (`loop.py`, see §12), but the Dockerfile
  CMD is still the disabled guard — the loop runs only when a
  deployment explicitly sets its start command to it.
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

## 12. Trading loop (loop.py — added 2026-11-01, code build only)

- **Entrypoint:** `loop.py` steps the §3 pipeline using engine.py
  rules ONLY (no new sizing, stop, take-profit, halt, or key rule).
  One step per 30 seconds (Screener cadence). Sells are evaluated
  before buys. On halt, the loop sells everything (each attempt
  retried once), parks in SOL, and STOPS — no new buys until James
  explicitly restarts it.
- **Gate order, before ANY trade step:** (1) `LIVE_ENABLED` must be
  exactly `"1"` or refuse; (2) `NETWORK` must parse to devnet or
  mainnet or refuse; (3) signer validated by
  `engine.load_signer_with_status()` — exactly one of
  `TRADING_PRIVATE_KEY` / `TRADING_KEYPAIR`, derived pubkey must
  match wallet §1 or refuse; (4) halt check — a stale or missing
  SOL price refuses (trading blocked), never "above halt";
  (5) price-freshness check (>30s or missing = no trade);
  (6) swap-route check (below). Every gate decision, buy/sell
  intent, halt, and refusal is logged with fixed error codes and
  public values only — no key material, ever.
- **Market speed:** if a coin's market speed cannot be determined,
  only hard-stop (−7%) and cap (+1000%) sells execute; slow-TP and
  peak-drop sells are held (see §4).
- **Swap execution — fail-closed abstraction:** swaps go through an
  injected `SwapExecutor`. The default executor refuses every swap.
  This build performs NO chain signing or sending itself and
  invents NO price and NO route. A deployment must supply a real
  executor on a proven route — it must never silently fall back
  to faking one.
- **Live feed — not in this build:** step inputs (candidates,
  prices, SOL price, market speed) are supplied by the caller.
  With no feed configured, `loop.py` refuses with
  `no_live_feed_configured` and trades nothing. A deployment must
  wire a real feed into `step_loop()` / `run_loop()` — never an
  invented price.
- **Smoke-test mode (read-only):** `python3 loop.py --smoke-test`
  reports network, RPC, wallet, live-gate status, and route
  status. It performs NO swap, NO signing, and NO trade, and does
  NOT require `LIVE_ENABLED=1` or a signer. In this build it exits
  non-zero with `route_unverified`, because devnet DEX routing
  was NOT verified in the sandbox (Jupiter v6 devnet quote
  returned no route) — that is the correct fail-closed result
  until a deployment proves a route (Orca/Raydium devnet pool,
  direct DEX instruction, or a devnet test token with a real pool).
- **How the loop is started (separate deployment step, NOT done
  by this build):** after the code is pushed, the service is
  deployed, the key is set by James (§8), and a smoke test
  proves a route + pubkey match, set the service's Custom Start
  Command to `python3 loop.py` and `LIVE_ENABLED=1`. Until then,
  the image's guard CMD runs and exits.
- **LIVE_ENABLED=1 alone does NOT start trading.** It is only
  one gate. Trading also requires: a loop start command, a real
  feed, a proven swap route, a validated signer, and James's
  go-ahead for that network. Any one missing = no trade.

## 13. Price feed + swap executor (feed.py / swap.py — added 2026-11-01, code build only)

- **Feed sources (verified live from the sandbox, 2026-11-01):**
  DexScreener `/latest/dex/tokens/{mint}` for token pairs
  (priceNative, priceUsd, liquidity, m5 buys/sells, priceChange),
  CoinGecko simple price as the SOL-USD fallback, and
  exchangerate-api for the CAD rate (fallback 0.712, approximate,
  §7). DexScreener's SOL pair was verified at $95.52 USD and
  CoinGecko at $95.49 USD; Jupiter v6 returned an empty reply
  in this sandbox (reachability gap, NOT a verified route).
- **30-second gain is computed, never read off a snapshot:**
  DexScreener gives m5/h1/h24 changes, not 30-second changes.
  feed.py's PriceTracker keeps timestamped sane marks per mint;
  gain_30s = price_now / price_~30s_ago − 1, Analyst
  confirmation = gained again in the last 5 seconds, Secondary
  = still gaining (non-decreasing). A mint with less than 30
  seconds of history is NOT a candidate — fail closed.
- **Bad-tick rejection:** a single mark jumping more than
  +500% in 30 seconds against the last sane mark is rejected,
  the last sane mark is frozen, and the mint is flagged
  stale_bad_tick. No candidate, buy, or sell is ever computed
  from a rejected mark. A non-positive price is rejected the
  same way.
- **Freshness:** a price is usable only if present, positive,
  and at most 30 seconds old (§5). Anything older or missing
  blocks the trade; the feed reports age on every snapshot.
- **Devnet feed limit (fail closed, §11):** DexScreener pairs
  are MAINNET pairs. They must NEVER price a devnet swap. On
  NETWORK=devnet the feed returns NO candidates and reports
  `devnet_liquidity_unverified` unless a deployment explicitly
  injects verified devnet pair data (a devnet test token with
  a real devnet pool). Devnet trenches liquidity was NOT
  verified in this sandbox.
- **Discovery limit (honest):** no verified free public
  endpoint in this sandbox reliably returns "all brand-new
  Solana trenches coins sorted by 30-second gain". The feed
  therefore tracks a caller-supplied watchlist (WATCH_MINTS,
  comma-separated) plus the loop's open-position mints. A
  deployment that wants autonomous discovery must supply a
  discovery callable; the default returns NO mints, so with no
  watchlist the feed produces NO candidates and trades nothing.
- **Executor — quote-gated, signer-gated, never faked:**
  swap.py requires, in order: (1) a validated signer
  (engine.load_signer_with_status, §8); (2) a fresh route
  quote (age at most 30 seconds, positive output, input/output
  mints matching the requested direction — SOL -> token for
  buys, token -> SOL for sells); (3) the slippage guard below.
  Only then is a deployment-supplied tx_builder called. With
  no tx_builder, every swap is refused
  (`executor_not_configured`) — never faked. This build itself
  performs NO signing and NO sending; signing/sending lives
  only in a deployment-supplied tx_builder.
- **Slippage guard — BUILDER DEFAULT, NOT James's number:**
  1.0% (SLIPPAGE_GUARD_PCT = 0.01). A swap is refused if the
  expected output at execution is more than 1.0% below the
  quoted output. James has NOT set a slippage number for this
  live engine; change this default only on his explicit
  say-so, and never present it as his number.
- **Route source status:** Jupiter v6 is the intended route
  source, but it is UNVERIFIED in this sandbox (empty reply
  for mainnet, no route for devnet). Orca/Raydium pools are
  NOT queried directly by this build (no pool SDK, stdlib
  only); a deployment wanting an Orca/Raydium route must
  supply its own quoter callable, which this module validates
  the same way (freshness, positive output, slippage guard).
- **Smoke-test mode (read-only):** `python3 loop.py
  --smoke-test --feed` reports feed status (SOL price source,
  CAD source) plus the loop's route status, with NO swap, NO
  signing, and NO trade. `swap.route_smoke_test(mint,
  direction)` reports route availability for one mint/direction
  the same way. A proven route here does NOT validate a
  signer and does NOT start trading.
- **Wiring:** loop.py now accepts a feed (LiveFeed instance
  or callable) and an executor/route checker
  (`--feed`, `--executor jupiter`, watchlist from WATCH_MINTS).
  With no feed it still refuses with
  `no_live_feed_configured`; with no tx_builder the swap
  executor still refuses every swap. The Dockerfile CMD is
  still the disabled guard — feed and executor change nothing
  about the image's default behaviour.

## 14. Transaction builder + devnet route (tx_builder.py — added 2026-11-01, code build only)

- **Transaction builder (tx_builder.py):** the ONE place that
  may turn a validated signer into a signed Solana transaction.
  It is wired into the loop ONLY with `--tx-builder solana`;
  without that flag the executor still refuses every swap,
  exactly as before, and the Dockerfile CMD is still the
  disabled guard. The current Railway start command
  (`python3 loop.py --feed --executor jupiter`) does NOT
  include `--tx-builder`, so the deployed loop still refuses
  — the flag is added only after a deployment smoke test
  proves a route (see below).
- **Gate order inside the builder, before ANY signing or
  sending:** (1) `LIVE_ENABLED` exactly `"1"`; (2) `NETWORK`
  parses to devnet/mainnet; (3) signer validated by
  `engine.load_signer_with_status` (exactly one key variable,
  derived pubkey must match wallet §1); (4) quote fresh
  (age ≤30s, positive output) with input/output mints
  matching the direction (SOL -> token for buys, token ->
  SOL for sells); (5) slippage guard (1.0%, BUILDER DEFAULT,
  §13): the plan's expected output must be ≥ min_output,
  AND min_output itself must be ≥ quote × (1 − guard) — a
  caller passing a looser min_output is refused, so the guard
  cannot be widened by a caller; (6) a swap-transaction
  provider must supply an unsigned transaction whose message's
  first account (fee payer) is the trading wallet and whose
  plan mints match the quote; (7) a recent blockhash is
  fetched from the network RPC (or taken from the supplied
  transaction's message), the message is signed, and the
  signed transaction is sent via RPC and (optionally)
  confirmed. Any failure at any gate = refusal, no signing,
  no sending, ledger unchanged.
- **Key handling in the builder (the only exception to §8's
  "marker only" design):** `engine.load_signer_with_status`
  deliberately returns no key bytes, so a builder that signs
  must read the key itself, once, from the same two
  environment variables, validate it the same way (derive
  the public key, compare to wallet §1), use the 32-byte
  seed only inside the signing call, and drop the reference
  immediately afterwards. The seed never enters a log, an
  error string, a return value, or a file. Signing is
  pure-Python Ed25519 (RFC 8032) and Solana transactions use
  the legacy message format, stdlib only — no external SDK,
  no pool SDK, no new dependency.
- **Jupiter swap provider:** `jupiter_swap_tx_provider` POSTs
  the quote's full quoteResponse to Jupiter's swap endpoint
  and returns its unsigned swap transaction. It needs the
  quote's raw response (swap.py's Quote now carries it); a
  quote without raw data is refused, never reconstructed.
  In this sandbox Jupiter returned an empty reply, so the
  provider fails and the builder refuses — correct,
  fail-closed behaviour, not a route.
- **Devnet route — RESEARCHED, STILL UNVERIFIED (read-only
  checks, 2026-11-01):** Jupiter v6 quote returned an empty
  reply for both a mainnet and a devnet quote; DexScreener
  returned pairs = null for the devnet token mint checked
  (4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU — named
  here as research evidence only, NOT a trading currency;
  this build is SOL-only); devnet RPC getAccountInfo showed
  the Orca Whirlpool program address has NO account on
  devnet, and the Raydium AMM and Jupiter aggregator program
  addresses are NOT executable programs on devnet
  (executable=false, owner = System Program) — so mainnet
  DEX programs cannot be used as a devnet route at their
  mainnet addresses; Orca and Raydium pool APIs returned
  MAINNET pool data (chainId 101), not devnet liquidity.
  **Conclusion: NO devnet SOL swap route is verified by this
  build.** A deployment proves one only by supplying
  verified devnet pool data (below) or a devnet-deployed
  program's swap transaction — and the smoke test must show
  it before the first devnet trade. Until then, devnet swaps
  refuse with `route_unverified`, by design. Do NOT present
  devnet trenches trading as proven or working.
- **Devnet pool quoter (DevnetPoolQuoter):** quotes ONLY from
  explicitly injected, verified devnet pool data — a mint
  with no entry, a non-positive price, or missing decimals
  returns NO quote. Mainnet pair data must NEVER be injected
  here. On devnet, loop.py wires this quoter (via
  `quote_for_executor`, which converts the loop's token-unit
  sell amounts to smallest units using the pool's decimals)
  instead of Jupiter, so route-checking and quoting cannot
  disagree and a mainnet quote can never price a devnet swap.
- **WATCH_MINTS (watchlist, already wired, now single-sourced):**
  `feed.watch_mints_from_env()` is the single place the
  watchlist is read: comma-separated env var, whitespace-only
  entries dropped, empty/unset = [] = NO candidates, trades
  nothing. With no watchlist and no open positions the feed
  produces NO candidates — that fail-closed default is kept,
  and NO autonomous trenches discovery was added (none was
  verified). A deployment supplies candidates by setting
  `WATCH_MINTS` (and, on devnet, `DEVNET_POOLS_JSON` with
  verified pool data for those mints).
- **Remaining deployment steps (none done by this build):**
  push, Railway deploy, start command (already set to
  `python3 loop.py --feed --executor jupiter`; add
  `--tx-builder solana` ONLY after the smoke test passes),
  key already deployed by James (pubkey match still
  unverified until a live-gated run checks it), deployment
  smoke test proving a devnet SOL swap route (route +
  pubkey), then `LIVE_ENABLED=1` under James's banked devnet
  go-ahead. That go-ahead does NOT authorise mainnet, and
  does NOT bypass the route/feed/smoke-test gates.
- **This build signs/sends NOTHING.** Tests use mocked
  providers/RPC and in-memory test-only keys only. No
  transaction was signed or sent in this build, on devnet or
  mainnet, and no key value was used, requested, or invented.

## 15. Live dashboard (dashboard.py — added 2026-11-01, code build only)

- **Live ONLY, never SolBot paper.** This dashboard presents
  the LIVE engine (devnet first, wallet §1, SOL only) and is
  labelled "LIVE 5-bot engine — LIVE ONLY, NOT SolBot paper"
  on every snapshot and page. It does NOT touch the SolBot
  app (artifact `swingbot`) or any SolBot paper data, and it
  must never be mixed into SolBot's paper view. Paper and
  live stay separate.
- **Read-only, no controls.** There are NO trading buttons,
  NO buy/sell actions, and NO enable/disable controls. The
  server serves GET / (HTML) and GET /snapshot.json (JSON)
  only; every other path is 404 and every non-GET method is
  405. There is no POST /buy, /sell, /trade, or /enable
  endpoint, and none may be added without James's explicit
  say-so. The dashboard cannot start, stop, enable, or trade
  for the engine.
- **Fields shown (from the live engine's own state):**
  Positions (open, per coin: invested SOL, tokens, entry,
  current mark, value, unrealised P&L, gain, peak); P&L
  (realised + unrealised, per coin and total, SOL); Balance
  (SOL cash, positions value, portfolio total in SOL and,
  on a fresh SOL price only, in CAD, plus halt distance to
  the $20 CAD threshold); Coins in the pipeline (Screener ->
  Analyst -> Secondary -> Executor/Risk, per coin, with the
  stage reached); Passed / Failed (per pipeline step, with
  reasons, including the loop's blocked reasons); W/L (wins
  / losses, per coin and total, from recorded sells — a sell
  is a win when proceeds exceed recorded invested SOL, a
  loss when below, neither when exactly break-even; a sell
  with no recorded entry invents no P&L); Gate status
  (live on/off, network, signer validated or not by error
  code only, feed fresh or stale, route status); Last step
  time. A sell's realised P&L uses the position's recorded
  invested SOL at the time the loop records it.
- **Snapshot (JSON, every loop step):** `dashboard.py`
  `build_snapshot` produces the snapshot dict, and the loop
  (`run_loop` with a dashboard state) writes it on every
  step. It contains ONLY public values. `scrub_public`
  drops any dict key naming key/seed/signature/secret
  material at any depth before the snapshot is stored,
  served, or written to a file. The snapshot file writer
  uses temp + replace so a reader never sees a half-written
  snapshot.
- **Freshness / honesty:** a snapshot older than the loop
  step interval (30s) or a feed older than 30s is marked
  STALE, with reasons. A missing mark shows as no mark
  (value at entry, per the engine's fail-closed rule) and a
  missing/stale SOL price gives NO CAD total and NO halt
  distance — never a stale-price CAD total that could hide
  a halt. Before the first step the snapshot is
  `engine_state: no_data_yet` with every value plainly empty
  or None; a refusing loop shows its gate error plainly.
  Positions, prices, and P&L are never invented.
- **Deployment options (Dockerfile CMD unchanged — disabled
  guard only; nothing auto-starts on image build):**
  A. Thread inside the loop process (recommended): start
  the loop with `--dashboard`; the server runs in a thread
  sharing the loop's dashboard state, so the page is always
  current. Port: $PORT (Railway) else $DASHBOARD_PORT else
  8000. Railway start command becomes, e.g.,
  `python3 loop.py --feed --executor jupiter --dashboard`
  (plus `--tx-builder solana` only after the smoke test
  passes, §14). Note: Railway exposes ONE port per service,
  so option A is the way the dashboard is reachable on the
  same service that runs the loop.
  B. Separate process, same image/service: the loop writes
  a snapshot JSON file on every step when
  DASHBOARD_SNAPSHOT_PATH is set, and
  `python3 dashboard.py` serves that file. The page shows
  "no data yet" until the first snapshot lands.
  C. Second Railway service: NOT viable on its own — a
  second service shares no file or memory with the loop
  service, so it would show "no data yet" forever unless a
  shared store is added (not built here). Do not deploy
  option C expecting live data.
- **Remaining deployment steps (none done by this build):**
  push, Railway deploy, start command (already set to
  `python3 loop.py --feed --executor jupiter`; add
  `--dashboard` and, only after the smoke test passes,
  `--tx-builder solana`), key already deployed by James
  (pubkey match still unverified until a live-gated run
  checks it), deployment smoke test proving a devnet SOL
  swap route (route + pubkey), then `LIVE_ENABLED=1` under
  James's banked devnet go-ahead. That go-ahead does NOT
  authorise mainnet, and does NOT bypass the route/feed/
  smoke-test gates.
- **This build signs/sends NOTHING and touches no SolBot
  data.** Tests use in-memory state and a local HTTP server
  only. No transaction was signed or sent in this build, on
  devnet or mainnet, and no key value was used, requested,
  or invented.

## 16. Devnet route research, corrected 2026-11-02 (orca_devnet.py + verify_devnet_pool.py — research + code build only)

- **CORRECTION — the 2026-11-01 claim that the Orca Whirlpool
  program had no account on devnet is WRONG.** Verified live
  on devnet RPC 2026-11-02 (getAccountInfo, public RPC):
  Whirlpool program `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc`
  IS executable on devnet (owner BPFLoaderUpgrade, 2,141,440
  lamports); devnet WhirlpoolsConfig
  `FcrweFY1G9HJAHG5inkGB6pKg1HZ6x9UC2WioAfWrGkR` exists
  and is owned by that program; a documented devnet test
  pool `3KBZiL2g8C7tiJ32hTv5v3KM7aK9htpqTw4cTXz1HvPt`
  exists, is owned by that program, and is funded (SOL
  vault ~398 SOL, second vault ~6,354 units at the check;
  both moved slightly on a re-check minutes later, so the
  pool is live, not a stale account). Pool parse (verified
  byte-for-byte): tickSpacing 64, tickSpacingSeed [64, 0],
  feeRate 2000, protocolFeeRate 300, liquidity
  76,581,459,811, sqrtPrice 0.14838, tickCurrent -38162,
  spot ≈ 22.02 second-mint units per SOL.
- **What this proves and what it does NOT prove.** It proves
  Orca devnet infrastructure exists and that a Whirlpool
  pool's spot price can be read from devnet RPC. It does
  NOT prove a trading route, for three reasons: (1) the
  documented test pool's second mint is a pegged test mint
  — under the SOL-only rule it must NOT be wired as a
  trading mint (proceeds park in SOL, never in a pegged
  mint); (2) no Whirlpool swap-transaction provider has
  been built or tested on devnet yet (tick arrays, oracle,
  owner token accounts, sqrtPriceLimit, amount threshold
  — all unbuilt); (3) the engine's trading route checker
  is therefore UNCHANGED: devnet swaps still refuse with
  `route_unverified`, by design.
- **Jupiter on devnet: dead end, confirmed.** The Jupiter
  aggregator program address returns NO account on devnet,
  and Jupiter's quote/swap APIs are mainnet-focused. A
  Jupiter quote, even if one were returned, references
  mainnet program accounts that do not exist on devnet,
  so a Jupiter-built swap cannot execute there. Do not
  present Jupiter as a devnet route.
- **Raydium devnet: programs exist, pools not verified.**
  Devnet CPMM `CPMDWBwJDtYax9qW7AyRuVC19Cc4L4Vcy4n2BHAbHkCW`,
  CLMM `devi51mZmdwUJGU9hjN27vEz64Gps7uUefqxg27EAtH`,
  AMM v4 `DRaya7Kj3aMWQSy19kSjvmuwq9docCHofyP9kanQGaav`
  are all executable on devnet, with their devnet config
  accounts present (CPMM config
  `9zSzfkYy6awexsHvmggeH36pfVUdDGyCcwmjT3AQPBj6`,
  CLMM config `CQYbhr6amxUER4p5SC44C63R4qw4NFc9Z4Db9vF4tZwG`).
  No Raydium devnet pool was verified here, and no
  Raydium swap provider is built here. Raydium CPMM is
  the most promising creation path for James (no OpenBook
  market required, unlike AMM v4), followed by Orca
  Whirlpool (permissionless pool creation via the
  official SDK).
- **Orca/Raydium pool-list APIs: mainnet only.** Both
  returned chainId 101 data. They must NEVER price a
  devnet swap. Only on-chain devnet RPC reads count.
- **New code (not wired into trading):** `orca_devnet.py`
  (Whirlpool parser in the verified layout, devnet RPC
  reader, quote-only quoter with source
  `orca_whirlpool_devnet_quote_only_no_execution`, and a
  status report whose `execution_verified` is ALWAYS
  False in this build) and `verify_devnet_pool.py`
  (read-only CLI that prints a pool report, a
  DEVNET_POOLS_JSON entry template, and a WATCH_MINTS
  value). Tests: 175/175 (was 165/165, +10 Orca tests,
  mocked RPC only). Dockerfile CMD unchanged (disabled
  guard); `orca_devnet.py` is not imported by the loop,
  so no trading path changed. Dockerfile COPY now
  includes `orca_devnet.py` (required: its tests import
  it; omitting it made the Railway build fail with a
  loader error at 166 tests). No key was used, no
  transaction signed or sent, nothing pushed or deployed
  by this build.
- **Exact next action for James (himself, throwaway
  devnet wallet only — NEVER the trading wallet):** see
  DEVNET_ROUTE_PLAN.md. In short: create a non-pegged
  devnet test token, create a pool for it with devnet
  SOL (Raydium CPMM or Orca Whirlpool via the official
  SDK on his desktop, with faucet SOL), verify the pool
  with `verify_devnet_pool.py`, then supply its verified
  data via DEVNET_POOLS_JSON + WATCH_MINTS. Only after a
  Whirlpool/Raydium swap provider is built and tested
  against that real pool (with a throwaway wallet first)
  can the deployment smoke test prove a devnet trading
  route — and only then may `--tx-builder solana` be
  added and `LIVE_ENABLED=1` be set, under the banked
  devnet go-ahead.
