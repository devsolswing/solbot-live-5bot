# SolBot LIVE 5-bot engine — DISABLED build

Build for James's ordered live 5-bot trading on Solana, in
**disabled mode only**. See RULES.md for the single rulebook.

**Launch order (James, 2026-10-31): DEVNET FIRST.** The engine was
first built for mainnet, then converted to support a devnet-first
launch without breaking the mainnet path. Network is selected by the
`NETWORK` environment variable (`devnet` | `mainnet`; unset = mainnet).
Mainnet is later, only after devnet is proven and James gives a
separate mainnet go-ahead.

**Currency (James, 2026-10-31): SOL ONLY.** James ordered "scrap the
USDC, go straight to SOL". That supersedes the earlier "everything in
USDC / sells park in USDC" rules (see RULES.md §0, history only).
Bankroll, buys, sells, and parking are all in SOL: buys SOL -> token,
sells token -> SOL, proceeds park in SOL. No stablecoin is used
anywhere in this build — no stablecoin mint, conversion, or parking
remains in the live engine, dry run, or rules.

## Files

- `engine.py` — core logic: sizing, pipeline, sell rules, halt, live gate,
  key-shape validation (never logs key material). No network, no signing.
  SOL-only ledger; halt via a live SOL price (fail-closed on a stale
  or missing SOL price).
- `loop.py` — trading loop entrypoint (added 2026-11-01, code build
  only, NOT started by the image — see "Loop" below). Steps the §3
  pipeline using engine.py rules only, with gates in order
  (LIVE_ENABLED, NETWORK, signer, halt, price freshness, swap
  route), sells before buys, halt sells-all + stop, and a
  read-only `--smoke-test` mode. Default swap executor and route
  checker refuse every trade (no proven route in this build).
- `feed.py` — live price feed (added 2026-11-01, code build
  only). DexScreener token pairs + CoinGecko SOL-price fallback
  + exchangerate-api CAD rate; PriceTracker computes 30s gains
  and 5s confirmation from its own marks, rejects >+500%
  single-window bad ticks, enforces 30s freshness. On devnet it
  returns no candidates without injected verified devnet pair
  data (DexScreener pairs are mainnet pairs — see RULES.md §13).
- `swap.py` — quote-gated, signer-gated swap executor (added
  2026-11-01, code build only). Jupiter v6 quote source
  (unverified in this sandbox), 30s quote freshness, 1.0%
  slippage guard (BUILDER DEFAULT, not James's number), and a
  deployment-supplied tx_builder as the only place that may
  sign/send. With no tx_builder every swap is refused — never
  faked. This build itself signs/sends nothing.
- `dashboard.py` — live-only, read-only dashboard (added
  2026-11-01, code build only). Serves the loop's public
  snapshot as phone-readable HTML (GET /) plus JSON (GET
  /snapshot.json): positions, P&L, balance + halt distance,
  pipeline coins, passed/failed, W/L, gate status, last step
  time. NO trading buttons, NO enable controls, NO key
  material — every other path is 404, every non-GET method
  is 405. It is separate from the SolBot paper app and never
  touches SolBot paper data. See "Live dashboard" below.
- `dry_run.py` — read-only public checks: wallet SOL balance, SOL price,
  CAD/USD rate, and halt arithmetic in SOL. No signing, no sending,
  no funding.
- `tests/test_engine.py` — unit tests for every rule in RULES.md.
- `RULES.md` — the rulebook. It wins over older copies on any conflict.

## Run the tests

```sh
cd ~/workspace/solbot-live-5bot
python3 -m unittest discover -s tests -v
```

## Loop (code build only — NOT started by the image)

```sh
python3 loop.py --smoke-test   # read-only: no swap, no signing, no trade
python3 loop.py                # refuses without a feed (see below)
```

`loop.py` is the missing loop, built 2026-11-01 as code only. It
does NOT run automatically: the Dockerfile CMD is still the disabled
guard, and setting `LIVE_ENABLED=1` alone does NOT start trading.

Gate order before any trade step: (1) `LIVE_ENABLED` exactly `"1"`,
(2) `NETWORK` parses to devnet/mainnet, (3) signer validated by
engine (exactly one key variable, derived pubkey must match
`9g9R…UTFQ`), (4) halt check (stale/missing SOL price = trading
blocked), (5) price freshness (>30s = no trade), (6) swap-route
check. Sells first, then buys at exactly 20% per coin, max 5 open,
one per coin, proceeds park in SOL. On halt it sells all, parks in
SOL, and stops — no new buys until James restarts it.

What the loop does NOT supply on its own: feed.py and swap.py
now exist (see "Feed + executor" below), but the loop still
refuses without them wired in — with no feed, `loop.py`
refuses with `no_live_feed_configured` and trades nothing;
with no tx_builder, the swap executor refuses every trade.
Devnet DEX routing was NOT verified in the sandbox (Jupiter v6
devnet quote returned no route), so a deployment must still
prove a devnet route — never an invented price or a faked swap.

## Feed + executor (code build only — NOT started by the image)

```sh
python3 loop.py --smoke-test --feed   # read-only: feed status + route status, no swap, no signing
python3 loop.py --feed --executor jupiter   # wires feed.py + swap.py; still refuses without a tx_builder
```

`feed.py` tracks a watchlist (WATCH_MINTS, comma-separated)
plus open positions, computes 30s gains / 5s confirmation from
its own marks, rejects >+500% bad ticks, and enforces 30s
freshness. On devnet it returns no candidates without injected
verified devnet pair data. `swap.py` requires a validated
signer, a fresh (≤30s) quote for the exact direction, and the
1.0% slippage guard (BUILDER DEFAULT, not James's number)
before calling a deployment-supplied tx_builder — and with no
tx_builder it refuses every swap. This build signs/sends
nothing itself, and the Dockerfile CMD is still the disabled
guard.

How it is started later (separate deployment step, NOT done here):
after push, Railway deploy, key set by James, and a smoke test
proving a route + pubkey match, set the Railway service's Custom
Start Command to `python3 loop.py --feed --executor jupiter
--tx-builder solana` and `LIVE_ENABLED=1`. (The start command
already set in Railway is `python3 loop.py --feed --executor
jupiter` WITHOUT `--tx-builder` — the flag is added only after
the smoke test passes, so the deployed loop still refuses.)

## Transaction builder + devnet route (code build only — NOT deployed, NO signing, NO sending)

```sh
python3 loop.py --feed --executor jupiter --tx-builder solana   # wires tx_builder.py; still refuses without a proven route + validated signer
```

`tx_builder.py` is the one place that may turn a validated
signer into a signed Solana transaction: pure-Python Ed25519
signing, legacy Solana message signing/sending, a Jupiter
swap-transaction provider, and a DevnetPoolQuoter for
explicitly injected devnet pool data. Gate order before any
signing/sending: live, network, signer (pubkey must match
`9g9R…UTFQ`), fresh quote with matching mints, slippage guard
(1.0%, BUILDER DEFAULT), provider-supplied unsigned
transaction whose fee payer is the trading wallet, recent
blockhash, then send + confirm. Any failure = refusal, no
signing, no sending. The signing seed is read once from the
same key variables, validated the same way, used only inside
the signing call, and dropped immediately — never logged.

Devnet route: RESEARCHED, STILL UNVERIFIED. Jupiter returned
an empty reply, DexScreener returned no pairs for the devnet
mint checked, and the mainnet DEX program addresses are not
executable programs on devnet — so no devnet route is proven
by this build. On devnet the loop quotes/routes ONLY from
injected verified pool data (`DEVNET_POOLS_JSON`); with none
injected it returns no quote and refuses with
`route_unverified`. WATCH_MINTS (comma-separated, read by
`feed.watch_mints_from_env()`) is the watchlist; empty =
no candidates, trades nothing. This build itself signed/sent
nothing — tests use mocked providers/RPC and in-memory
test-only keys only.

## Live dashboard (code build only — NOT deployed, NO trading, NO controls)

```sh
python3 loop.py --feed --executor jupiter --dashboard   # loop + dashboard thread, port $PORT else $DASHBOARD_PORT else 8000
python3 dashboard.py                                    # standalone server for a snapshot file the loop writes (DASHBOARD_SNAPSHOT_PATH)
```

`dashboard.py` is the live-only dashboard James asked for: a
separate, read-only view of the LIVE engine — **not** the SolBot
paper app, which it never touches. It shows Positions (open, per
coin, size/entry/mark/value/unrealised P&L), P&L (realised +
unrealised, per coin and total, SOL), Balance (SOL cash +
portfolio total, CAD total and halt distance only on a fresh
SOL price), Coins in the pipeline (Screener → Analyst →
Secondary → Executor/Risk, per coin), Passed / Failed (per
pipeline step, with reasons), W/L (wins/losses, per coin and
total, from recorded sells), gate status (live on/off, network,
signer validated or not, feed fresh or stale, route status),
and last step time. The loop writes a public-only JSON snapshot
on every step (scrubbed of any key/seed/signature/secret keys);
stale data (feed >30s, snapshot older than the 30s step
interval) is marked STALE, and "no data yet" / refusing states
are shown plainly — never invented positions, prices, or P&L.

Deployment: Dockerfile CMD is unchanged (disabled guard only).
Recommended: thread inside the loop process (`--dashboard`) on
the same Railway service, since Railway exposes one port per
service — the start command becomes `python3 loop.py --feed
--executor jupiter --dashboard` (plus `--tx-builder solana`
only after the smoke test passes). Alternative: the loop writes
a snapshot file when `DASHBOARD_SNAPSHOT_PATH` is set and
`python3 dashboard.py` serves it. A second Railway service on
its own is NOT viable — it shares no file or memory with the
loop service and would show "no data yet" forever unless a
shared store is added (not built here). There are no trading
buttons, no enable controls, and no POST endpoints of any kind.

## Dry run (read-only)

```sh
python3 dry_run.py
```

This only reads public data. If a public endpoint is unreachable from
this sandbox, the script reports that plainly instead of inventing data.
If the SOL price is unavailable, the halt arithmetic in SOL is skipped
(fail-closed) — no SOL amount is invented.

## How live would be enabled (NOT done by this build)

All of these, in order, are required before any trade **on that
network**. A devnet go-ahead authorises devnet only; mainnet needs its
own separate go-ahead later.

### Devnet first (current launch order)

1. **Network** — `NETWORK=devnet` in the deployment environment
   (Railway Variables for the devnet service).
2. **Funding confirmed on devnet** — faucet SOL actually in wallet
   `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ` on devnet
   (Solana faucet only — no other funding token is needed or used).
   Devnet balance at conversion time: 0 devnet SOL.
   Test bankroll scale (assumption for James to correct, see
   RULES.md §11): $85 CAD equivalent in SOL, halt at $20 CAD
   equivalent, both converted at the live SOL price at funding/test
   time. These are worthless faucet funds, not real money.
3. **Key set by James himself** — exactly one of the following, set in
   the deployment environment only (Railway Variables, see below).
   Never in chat, code, repo, or files. Never both at once.
   - `TRADING_PRIVATE_KEY` — base58 string, exactly as Phantom exports
     it (recommended: no conversion step on James's side).
   - `TRADING_KEYPAIR` — JSON array of 64 integers (original form).
   The engine derives the key's public key and refuses to sign unless
   it matches wallet `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`.
4. **Devnet go-ahead from James** after devnet funding is confirmed.
5. **Code gate + smoke test** — `LIVE_ENABLED=1` in the devnet service,
   plus a devnet smoke test in Railway proving a devnet SOL swap route
   (routing is UNVERIFIED in this sandbox — see RULES.md §11; fail
   closed if no route) before the first devnet trade.

### Mainnet later (not authorised by a devnet go-ahead)

1. **Network** — `NETWORK=mainnet` (or unset) in a mainnet service.
2. **Funding confirmed on mainnet** — $85 CAD worth of SOL on Solana,
   plus enough SOL for fees from the same wallet balance, actually in
   wallet `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`
   (mainnet balance at build time: 0.00158242 SOL, bankroll not funded).
3. **Key set by James himself** in the mainnet service (§8 of RULES.md).
4. **Final explicit mainnet go-ahead from James** after mainnet
   funding is confirmed.
5. **Code gate** — `LIVE_ENABLED=1` in the mainnet service, plus a
   live-deployment smoke test of a real SOL swap execution (which
   this sandbox build explicitly does NOT verify).

Until all gates for a network are done, `LIVE_ENABLED` defaults to off
and the engine refuses to trade. `LIVE_ENABLED=1` alone does NOT
start trading — the loop also needs a start command, a real feed,
and a proven swap route (see "Loop" above and RULES.md §12).

## Railway deployment (prepared, NOT deployed by this build)

This folder is deployment-ready but **no Railway project or service
has been created by this build**. James himself performs every step
below; the assistant does not set any variable and does not ask James
for a key.

### Files

- `Dockerfile` — Python 3.12 slim, stdlib only (no dependencies to
  install). Build-time: runs the full unit test suite; the image fails
  to build if any test fails. Runtime: `LIVE_ENABLED=0` is set in the
  image, no `TRADING_*` variable is set in the image, and the container
  runs a disabled-mode guard (live gate off, no signer) and exits
  cleanly. **This image does NOT start the trading loop by
  default** — `loop.py` now exists in the image, but the CMD is
  still the disabled guard; the loop runs only when a deployment
  explicitly sets its start command to `python3 loop.py` (see
  "Loop" above). Deploying it now only hosts the disabled code
  and sends no transaction.
- `.dockerignore` — excludes git metadata, caches, `.env` files, any
  keypair/secret files, and state files, so none of those can be baked
  into the image by accident.

### Steps James takes (in order)

1. **Push this folder to a GitHub repo** (the assistant has NOT pushed
   it in this conversion task — the parent pushes after review).
   Railway deploys from a repo, so the folder must be in one first.
2. **Log in at railway.app**, create a **New Project → Deploy from
   GitHub repo**, and select that repo.
3. **Service settings:** set the service's **Root Directory** to the
   folder containing this `Dockerfile` (e.g. `solbot-live-5bot` if that
   is the folder name in the repo), and confirm the builder is
   **Dockerfile**. If the folder is at the repo root, no Root Directory
   change is needed.
4. **Variables (Railway → service → Variables), in this order:**
   a. Set **`NETWORK`** — `devnet` for the devnet-first service
      (mainnet service later uses `mainnet` or unset). An invalid
      value fails closed.
   b. Set **exactly one** key variable, pasted by James himself from
      his own Phantom export / keypair — never in chat, never in code,
      never in the repo:
      - `TRADING_PRIVATE_KEY` = the base58 Phantom export string
        (recommended), **OR**
      - `TRADING_KEYPAIR` = the JSON array of 64 integers.
      Never set both: the engine refuses to run if both are present.
   c. Leave **`LIVE_ENABLED` at `0` / unset** for now. It must stay off
      until funding is confirmed AND James gives a go-ahead for that
      network (devnet go-ahead ≠ mainnet go-ahead).
5. **Deploy and check the logs:** the service should report the
   disabled-mode guard (live off, no signer) and send nothing. If the
   logs show anything else, stop and investigate before proceeding.
6. **Only after** (i) funding for that network is confirmed landed in
   wallet `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ` (devnet:
   faucet SOL only; mainnet: $85 CAD equivalent in SOL, fees from the
   same balance), (ii) the key variable above is set, and (iii) James
   gives an explicit go-ahead for that network: set `LIVE_ENABLED=1`
   in Railway Variables and run a smoke test in Railway (a real SOL
   swap execution, which this sandbox build explicitly does NOT
   verify — and on devnet, routing itself is unverified, see
   RULES.md §11) before the first trade on that network. If Railway
   asks for payment at any step: **STOP** and do not proceed without
   James's explicit approval.

### What remains unverified for Railway specifically

- That Railway builds this Dockerfile cleanly from the repo (build
  only verified locally here: tests pass, syntax compiles).
- That Railway's variable handling passes the base58 / JSON value
  through unchanged (whitespace trimming is handled by the engine, but
  a mangled paste must be caught by the pubkey-mismatch refusal, not
  by a trade).
- Live SOL swap execution, deployment-environment signer
  construction from `TRADING_PRIVATE_KEY` / `TRADING_KEYPAIR`, and the
  live slow/fast market feed — all still unverified, as in the original
  build. None of these are proven by deploying the disabled image.
