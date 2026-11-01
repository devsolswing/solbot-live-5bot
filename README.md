# SolBot LIVE 5-bot engine — DISABLED build

Build for James's ordered live 5-bot trading on Solana mainnet, in
**disabled mode only**. See RULES.md for the single rulebook.

## Files

- `engine.py` — core logic: sizing, pipeline, sell rules, halt, live gate,
  key-shape validation (never logs key material). No network, no signing.
- `dry_run.py` — read-only public checks: SOL price, CAD/USD rate, and a
  Jupiter swap quote. No signing, no sending, no funding.
- `tests/test_engine.py` — unit tests for every rule in RULES.md.
- `RULES.md` — the rulebook. It wins over older copies on any conflict.

## Run the tests

```sh
cd ~/workspace/solbot-live-5bot
python3 -m unittest discover -s tests -v
```

## Dry run (read-only)

```sh
python3 dry_run.py
```

This only reads public data. If a public endpoint is unreachable from
this sandbox, the script reports that plainly instead of inventing data.

## How live would be enabled (NOT done by this build)

All of these, in order, are required before any real trade:

1. **Funding confirmed** — $85 CAD worth of USDC on Solana, plus a little
   SOL for fees, actually in wallet
   `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`
   (mainnet balance at build time: 0.00158242 SOL, bankroll not funded).
2. **Key set by James himself** — exactly one of the following, set in
   the deployment environment only (Railway Variables, see below).
   Never in chat, code, repo, or files. Never both at once.
   - `TRADING_PRIVATE_KEY` — base58 string, exactly as Phantom exports
     it (recommended: no conversion step on James's side).
   - `TRADING_KEYPAIR` — JSON array of 64 integers (original form).
   The engine derives the key's public key and refuses to sign unless
   it matches wallet `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`.
3. **Final explicit go-ahead from James** after funding is confirmed.
4. **Code gate** — `LIVE_ENABLED=1` in the deployment environment, plus
   a live-deployment smoke test of a real Jupiter swap execution (which
   this sandbox build explicitly does NOT verify).

Until all four are done, `LIVE_ENABLED` defaults to off and the engine
refuses to trade. Do not start a persistent loop from this build.

## Railway deployment (prepared, NOT deployed by this build)

This folder is deployment-ready but **nothing has been pushed, and no
Railway project or service has been created by this build**. James
himself performs every step below; the assistant does not set any
variable and does not ask James for a key.

### Files

- `Dockerfile` — Python 3.12 slim, stdlib only (no dependencies to
  install). Build-time: runs the full unit test suite; the image fails
  to build if any test fails. Runtime: `LIVE_ENABLED=0` is set in the
  image, no `TRADING_*` variable is set in the image, and the container
  runs a disabled-mode guard (live gate off, no signer) and exits
  cleanly. **This image does NOT start a trading loop** — no loop
  exists in this build — and it sends no transaction. Deploying it now
  only hosts the disabled code.
- `.dockerignore` — excludes git metadata, caches, `.env` files, any
  keypair/secret files, and state files, so none of those can be baked
  into the image by accident.

### Steps James takes (in order)

1. **Push this folder to a GitHub repo** (the assistant has NOT pushed
   it). Railway deploys from a repo, so the folder must be in one first.
2. **Log in at railway.app**, create a **New Project → Deploy from
   GitHub repo**, and select that repo.
3. **Service settings:** set the service's **Root Directory** to the
   folder containing this `Dockerfile` (e.g. `solbot-live-5bot` if that
   is the folder name in the repo), and confirm the builder is
   **Dockerfile**. If the folder is at the repo root, no Root Directory
   change is needed.
4. **Variables (Railway → service → Variables), in this order:**
   a. Set **exactly one** key variable, pasted by James himself from
      his own Phantom export / keypair — never in chat, never in code,
      never in the repo:
      - `TRADING_PRIVATE_KEY` = the base58 Phantom export string
        (recommended), **OR**
      - `TRADING_KEYPAIR` = the JSON array of 64 integers.
      Never set both: the engine refuses to run if both are present.
   b. Leave **`LIVE_ENABLED` at `0` / unset** for now. It must stay off
      until funding is confirmed AND James gives a final go-ahead.
5. **Deploy and check the logs:** the service should report the
   disabled-mode guard (live off, no signer) and send nothing. If the
   logs show anything else, stop and investigate before proceeding.
6. **Only after** (i) the $85 CAD USDC bankroll + SOL for fees is
   confirmed landed in wallet
   `9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ`, (ii) the key
   variable above is set, and (iii) James gives a final explicit
   go-ahead: set `LIVE_ENABLED=1` in Railway Variables and run a live
   smoke test in Railway (a real Jupiter swap execution, which this
   sandbox build explicitly does NOT verify) before the first live
   trade. If Railway asks for payment at any step: **STOP** and do not
   proceed without James's explicit approval.

### What remains unverified for Railway specifically

- That Railway builds this Dockerfile cleanly from the repo (build
  only verified locally here: tests pass, syntax compiles).
- That Railway's variable handling passes the base58 / JSON value
  through unchanged (whitespace trimming is handled by the engine, but
  a mangled paste must be caught by the pubkey-mismatch refusal, not
  by a trade).
- Live Jupiter swap execution, deployment-environment signer
  construction from `TRADING_PRIVATE_KEY` / `TRADING_KEYPAIR`, and the
  live slow/fast market feed — all still unverified, as in the original
  build. None of these are proven by deploying the disabled image.
