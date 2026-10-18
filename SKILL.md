---
name: reya-trading-agent-kit
description: >-
  Query Reya V2 market data, books, candles, trades, funding and WebSocket feeds,
  inspect exact account state and bounded order/transfer history, diagnose setup,
  and trade perpetual futures with live execution: market and limit orders,
  take-profit and stop-loss triggers, cancel, cancel_all, and close_all, plus
  account reads (positions, open orders, balances, equity, margin usage and
  liquidation prices), and sizing orders by margin risk. Use when the user mentions Reya, Reya Network, or RUSD perps, or
  wants to check positions, margin, or funding, or to place, cancel, or close
  perpetual futures orders on Reya.
license: MIT
allowed-tools:
  - Bash
compatibility: >-
  Requires Python 3.12+ and an assistant that can run shell commands (Claude
  Code or Codex). One-time setup: run `./install.sh --release=latest
  --owner=0x<wallet>` inside the skill directory (no package manager needed —
  creates `.venv`, pulls the pinned reya-python-sdk from GitHub and starts the
  guided agent setup), or `poetry install` with Poetry 2.x. Needs network
  egress to github.com and the selected Reya API (mainnet, devnet1, or Cronos).
---

# Reya Agent Kit

Trade perpetual futures on Reya DEX — a high-performance decentralized exchange.

Scripts live in `agent_kit/scripts/` inside this skill's directory. Read commands use `query.py`, write commands use `trade.py`. Both print structured JSON to stdout and exit `0` on success. Any failure — including invalid or missing arguments — prints JSON with an `error` field and exits `1`. Partial execution and batch failures retain their result details alongside the error. The scripts never print usage text or tracebacks and never use exit code `2`; the only non-JSON output is `--help`.

Commands below are shown as `python -m agent_kit.scripts.<script> ...`. Run them from any directory as:

```bash
<skill-dir>/.venv/bin/python -m agent_kit.scripts.<script> ...
```

where `<skill-dir>` is the directory containing this SKILL.md. The venv interpreter path selects this skill's own environment regardless of your current working directory — no Poetry process in the loop. (`poetry -C <skill-dir> run python -m ...` also works if the kit was installed with Poetry.)

> **Live execution and separate local paper simulation.** Use `paper` for virtual funds; it cannot submit live writes. Read [paper limits](docs/paper-trading.md) before describing results. No historical backtest or production-acceptance claim.

## Install

The repository is private during the pilot. See [installation, update and uninstall](docs/private-first-use.md).
Use `doctor` to report the resolved environment and skill-link status without
opening secrets. Set `REYA_MODE=read-only`, `devnet` or `live` explicitly for a
pilot. Use the separate `paper` command for simulation; `REYA_MODE=paper` keeps live commands blocked. Never imply a sub-account
restricts a wallet-wide credential.


Clone with your GitHub access and run the installer with `--release=latest` — it switches to the newest published release, creates `.venv`, installs the pinned dependencies (including [`reya-python-sdk`](https://github.com/Reya-Labs/reya-python-sdk) as a pinned git dependency, so every `from sdk...` import resolves against it), symlinks the kit into `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (other agentskills.io runtimes) and/or `${CODEX_HOME:-~/.codex}/skills/` (Codex), and runs a health check. The [private first-use guide](docs/private-first-use.md) is authoritative for install, clone troubleshooting, update and uninstall:

```bash
gh repo clone Reya-Labs/reya-trading-agent-kit
cd reya-trading-agent-kit
./install.sh --release=latest --owner=0x<wallet>   # --owner continues into agent init; --agent=… / --no-link choose skill links
```

If the user asks you to set the kit up from scratch, follow "For AI assistants" in the [README](README.md#for-ai-assistants-setting-this-kit-up-for-a-user). To update an installed kit, run `./install.sh --release=latest` in the kit directory after the user agrees.

Prefer Poetry? `poetry install` works too (Poetry 2.x; the venv lands at `.venv` either way) — then link manually with `make skill-link`.

Verify connectivity any time:

```bash
~/.claude/skills/reya-trading-agent-kit/.venv/bin/python -m agent_kit.scripts.health
```

## V2 data and streams

Use the [complete data/accounting contract](docs/v2-data-and-reconciliation.md)
for precision, coverage, failure and API limitations. All new reads and feeds
are V2. The account relay and Core-nonce routes are the only non-V2 calls, kept
under the exception agreed on 12 October 2026 for account setup only; add no others.

```bash
python -m agent_kit.scripts.doctor
python -m agent_kit.scripts.data market search --symbol ETH
python -m agent_kit.scripts.data market book --symbol ETHRUSDPERP --limit 50
python -m agent_kit.scripts.data market candles --symbol ETHRUSDPERP --resolution 5m
python -m agent_kit.scripts.data market trades --symbol ETHRUSDPERP --max-pages 3
python -m agent_kit.scripts.data market summary --symbol ETHRUSDPERP
python -m agent_kit.scripts.data accounts
python -m agent_kit.scripts.data state
python -m agent_kit.scripts.data history transfers --max-pages 10
python -m agent_kit.scripts.data reconcile-orders --max-pages 10
python -m agent_kit.scripts.stream market --feed depth --symbol ETHRUSDPERP --seconds 30
python -m agent_kit.scripts.stream wallet --feed transfers --seconds 30
```

`data history` also supports orderHistory, perpExecutions and executionBusts.
Market streams support depth, summary, perpExecutions and executionBusts. Wallet
streams support accounts, positions, accountBalances, orderChanges,
perpExecutions, executionBusts and transfers. `--max-pages` is 1..100 (default 10);
`--start-time`/`--end-time` use milliseconds. Candle resolutions are listed by
`--help`; absent end-time uses current time. History results are wallet-scoped.

`stream` emits NDJSON with ready/stale/recovering/incomplete state. It is bounded
and read-only; it never persists a full audit ledger or enables writes from a
cached view. A connected socket does not establish fresh data. Do not add funding
or ADL again to public `realBalance`, or rescale public `qty` a second time.
Missing history and unresolved outcomes never authorise resubmission.

## Credentials

Public reads (`health`, `prices`, `market info`, `market funding`) work without credentials. Account reads require an owner address and account ID (`accounts` only needs the owner). Writes sign with the **agent key**: the kit generates it, the owner approves its address once in the Reya app, and it can only place and cancel orders — never withdraw, transfer or change permissions. **Never ask the user for a private key.** Mainnet is the default and needs no `.env` before setup: `agent init` writes `.env` at the skill root (owner, agent key, agent account). Copy `.env.example` to `.env` only to switch deployment or mode (devnet1 block is commented there). Loading is anchored to the kit's own location, not the current working directory, and real environment variables take precedence.

```bash
# Written by `agent init` on mainnet; nothing to prepare by hand:
# PERP_WALLET_ADDRESS_1, PERP_AGENT_PRIVATE_KEY, PERP_AGENT_ADDRESS, PERP_AGENT_ACCOUNT_IDS, PERP_ACCOUNT_ID_1
# Devnet1 instead: CHAIN_ID=89346162, REYA_API_URL=https://api-devnet.reya-cronos.network/v2, matching gateway, REYA_DEX_ID=1, REYA_MODE=devnet
```

Set up with `python -m agent_kit.scripts.agent init --owner 0x<wallet>` (see the agent setup section). The agent key trades only accounts recorded in `PERP_AGENT_ACCOUNT_IDS`; other accounts are refused by the kit because the on-chain permission is wallet-wide.

Own-key mode: a user who deliberately wants the kit to hold a wallet key (for example a fresh wallet created only for it) sets `REYA_SIGNER=own-key` and exactly one of `PERP_PRIVATE_KEY_FILE` (absolute path to a regular, non-symlink file owned by the current user, mode `0600`/`0400`, containing only the hexadecimal key) or `PERP_PRIVATE_KEY_1`. Those variables without `REYA_SIGNER=own-key` are an error. Do not suggest own-key mode during onboarding; use it only when the user asks for it. Unsigned account reads do not open key files or validate signing secrets.

Devnet1 and Cronos share a chain ID but have different API URLs and signing gateways. All commands use the same environment resolver; known deployment mismatches are rejected. Non-mainnet account commands require `REYA_DEX_ID` explicitly. See `.env.example` for each deployment. With no environment, public reads default to mainnet.

For mainnet, use `.env.mainnet.example` and follow [the production guide](docs/mainnet.md). Account reads do not construct a signer. Reads and writes check that the configured account belongs to the owner on the selected deployment; a mismatch is an error, not an empty account. Mainnet public-read and offline signing coverage does not replace operator-run live acceptance.

Never commit private keys to any repository.

`doctor` warns (without reading the value) when `.env` holds a key and other users can read the file; fix it with `chmod 600 .env`.

**Treat `.env` and private-key files as write-only.** Never read, `cat`, `echo`, source, or otherwise print `.env` or the value of `PERP_AGENT_PRIVATE_KEY` or `PERP_PRIVATE_KEY_1` — not into command output, logs, or conversation. If credentials are missing or wrong, the scripts say so in their JSON error; relay that message and tell the user what to do (usually: re-run `agent init`). Do not inspect the file to "check" it.

## Agent setup (`agent.py`)

```bash
python -m agent_kit.scripts.agent init --owner 0x<wallet> [--amount N] [--account-id ID] [--wait | --until-ready]
python -m agent_kit.scripts.agent status [--wait]
```

`init` is idempotent and state-driven: it returns `step` plus `next` (what the user must do) and a `url` to open in the Reya app. For the live steps `url` is one setup page that covers approval, the Agent account and funding; `init --until-ready --timeout 100` opens it and waits through all three, returning at `ready` (or a decision). While it returns `timed_out: true`, tell the user their `progress` (step N of 3) and run it again. `--wait` returns at the next step change instead. Steps: `needs_owner` → `needs_app_onboarding` (wallet has no Reya accounts: connect in the app and accept the terms; the kit never creates a wallet's first account) → `needs_approval` (agent key generated into `.env`; the user approves `agent_address` on the setup page with one signature; relay the `permission` text; the link carries the owner, and the page asks the user to switch first if another wallet is connected) → `needs_agent_account` (one click in the app creates the "Agent account" sub-account; the kit records it) → `needs_funding` (one signature transfers USDC from the main account; the page suggests an amount, and `--amount` pre-fills it) → `ready`. The sub-account named exactly "Agent account" is always the agent's: if the wallet has one (from this setup, an earlier one or a reinstall), `init` uses it instead of asking for another, and `next` names it; tell the user which account it will trade. If several have that name, `init` returns `ambiguous_new_accounts`: ask the user which one and pass `--account-id`; the kit only trades the account recorded, so the others are left alone. Other names are never picked by name. `status` reports the same without changing anything, so it never records the Agent account: wait with `init --until-ready`, not `status --wait`. If the user meant a different wallet from the one given to `--owner`, remove the `PERP_WALLET_ADDRESS_1` line from `.env` and run `init --owner <that wallet>`; the agent key is kept and is approved from the new wallet. Show the user the `url` and the address to compare; never paste or print key material, and never try to drive the user's browser or wallet yourself.

**Say USDC, not rUSD, to the user.** The Reya app shows the rUSD collateral as USDC everywhere; they are the same balance. The kit's JSON keeps `rusd` in field names (`min_order_rusd`, `fee_rusd`, balances), but say USDC when you talk about amounts, and never suggest buying, bridging or converting to rUSD as a separate step.

A fresh setup's result carries `practice_first` and opens nothing yet: ask its question once, and on "practise" run its `practice_command` instead of continuing; on "real trading" run `init --until-ready`, which opens the setup page. The page opens once across runs; `link_already_opened: true` means it is already in a browser tab, so point the user there or show `url`. If the setup page does not load, give the user `step_url`, the app's regular page for the current step. At `needs_funding`, relay `funding_guidance` (smallest order on the guidance market and a suggested amount); the user enters the amount on the page. At `ready` the result carries `try_asking`: offer those as first things to try. Both `init` and `status` also return `kit` (`installed`, `latest_release`, `update_available`); when an update is available, tell the user and offer to run `./install.sh --release=latest` in the kit directory. Never update without asking.

If a write later fails with `UNAUTHORIZED_SIGNATURE_ERROR`, the approval was probably revoked: run `agent status` for the re-approve link.

**If you are not able to place trades for the user** (as of October 2026 the Claude desktop app declines to, even when asked), say so the first time trading comes up, before preparing anything: never tell the user you can trade and then refuse at the last step. Offer the two working paths: run the same request in Claude Code in a terminal (`claude`) or Codex, or prepare each order for the user to run themselves (below).

**User-run orders.** When you cannot place trades but the user wants to trade from this session, you prepare and the user runs. Run every read and every `--preview` yourself; only the write itself goes to the user. For each write:

1. **Say what it does in plain words first:** market, side, size, order type, limit price or price bound, notional in USDC, estimated leverage and distance from mark (from your `--preview`), and the account it trades.
2. **Show exactly one command** in its own code block tagged `bash`, so apps that offer a Run button show one. Use the absolute venv path (`<skill-dir>/.venv/bin/python -m agent_kit.scripts.trade ...`), no `cd`, `&&`, pipes or shell variables, and always pass an explicit `--client_order_id` on creates so the order can be found in `query orders` or order history if the output is lost.
3. **Ask the user to press Run** or paste the command into their terminal, then wait. Never put two writes in one block or one message. An entry with its stop-loss and take-profit is one command (`order market ... --sl ... --tp ...`); for any other multi-step plan, show the next block only after the previous result is confirmed.
4. **Verify with read commands** afterwards (`query orders`, `query positions`, `query executions --order_id ID`) and report the outcome, fill price and fee. If you can read the command's output, use it, and still confirm with a read.
5. **Never show the same write again** while its outcome is unknown; read account state first. A rerun can place a second order.

This is not a way around your rules: the user decides and runs every write. `cancel_all` and `close_all` still need their `--preview` and the user's explicit confirmation first.

---

## Symbols, errors and output

Perp symbols are `TICKER` + `RUSDPERP` (`ETHRUSDPERP`, `BTCRUSDPERP`). Every `--symbol` and
symbol argument also accepts the bare ticker in any case (`eth`, `BTC`), which the kit
expands before validation; `data market search --symbol` stays a literal substring search.
Unknown symbols return `{"error": "Unknown symbol ..."}` (exit 1), never an empty list.
Use `query prices` (no `--symbol`) to list markets.

SDK HTTP failures keep the `error` field and add `error_code`, `message` and `http_status`
when available; headers, exception dumps and arbitrary body fields are omitted, and
unrecognised bodies use `API_ERROR`. A per-IP edge ban (HTTP 429) is `EDGE_RATE_LIMITED`:
pause every request for at least 60 seconds. A venue `retryAfterMs` hint is returned as
`retry_after_ms`. See [error codes](docs/error-codes.md) for what each code means and
whether a retry is safe. An error code never authorises retrying an
ambiguous write: read account state first.

`REGION_UNAVAILABLE` (HTTP 403) means Reya's API is not served in the user's region and
nothing reached the venue. Tell the user plainly; do not suggest VPNs, proxies or other
ways around it.

Field-by-field response shapes and full argument notes are in the
[command reference](docs/command-reference.md).

---

## Read Commands (`query.py`)

| Command                          | Purpose                                                                  | Auth |
| -------------------------------- | ------------------------------------------------------------------------ | ---- |
| `accounts` | Discover accounts belonging to the owner (no account ID or private key needed) | Owner address |
| `fees` | Fee tiers and discounts; with an owner set, that wallet's tier | No (owner optional) |
| `executions [--symbol X] [--order_id ID] [--start_time MS] [--end_time MS]` | One bounded page of settled account fills and fees | Owner/account |
| `prices [--symbol X]`            | Get oracle and mark prices for all or one market                         | No   |
| `market info [--symbol X]`       | Structural params (steps, ticks, max leverage, margin params, OI cap)    | No   |
| `market funding [--symbol X]`    | Funding rate, open interest, long/short funding values per market        | No   |
| `positions [--symbol X]`         | Open positions with estimated unrealized PnL, liquidation price and distance | Yes  |
| `orders [--symbol X]`            | Get open orders                                                          | Yes  |
| `balances`                       | Get balances for the configured perp account                             | Yes  |
| `margin`                         | Equity incl. unrealized PnL, liquidation and initial margin, margin usage, liquidation prices | Yes  |

All authenticated commands scope to `PERP_ACCOUNT_ID_1`. Other accounts on the same wallet are not included.

```bash
python -m agent_kit.scripts.query prices --symbol btc
python -m agent_kit.scripts.query market info --symbol ETHRUSDPERP
python -m agent_kit.scripts.query market funding --symbol eth
python -m agent_kit.scripts.query accounts      # owner only; discover account IDs
python -m agent_kit.scripts.query fees          # tier and taker/maker bps
python -m agent_kit.scripts.query positions
python -m agent_kit.scripts.query orders --symbol eth
python -m agent_kit.scripts.query executions --symbol eth --order_id 123456789
python -m agent_kit.scripts.query balances
python -m agent_kit.scripts.query margin
```

Rules that matter when reading results:

- Missing oracle/mark prices are `null`, never zero.
- `positions` adds `mark_price`, `mark_updated_at` and `estimated_unrealized_pnl_usd`
  (`qty × (mark − entry)`, signed by side). It excludes accrued funding and fees and is
  not a settlement value. If marks cannot be read, those fields are `null` and
  `mark_unavailable_reason` explains why.
- `margin` mirrors the matching engine's model ([docs/margin.md](docs/margin.md)): `equity_usd`
  is collateral after haircuts plus unrealized PnL at mark plus unsettled funding (a debit-only
  estimate). `margin_usage` = liquidation margin / equity; 1 is liquidation. `margin_buffer_usd`
  is equity − liquidation margin. Each position has `liquidation_price_usd` (cross margin,
  every other price unchanged) and `distance_to_liquidation_pct`; `positions` carries them too.
  For any monitoring loop or trigger, use equity from `margin`, never balances alone: balances
  do not move with price.
- `executions` returns one capped wallet page filtered locally; an empty result does not
  prove no fills exist. Only `fee_rusd` is the fill's fee; `pnl_opening_fee_rusd` is a PnL
  field, not an extra fee. Taker fees are charged on quantity × the on-chain mark at
  settlement, not the fill price.
- Orders use `order_side: "buy"/"sell"`. Trigger orders add `close_side`, `position_side`
  and `quantity_mode: "whole_position"`; `qty: null` there means whole-position sizing.
  Unknown flags are `null`, and null is not false.

---

## Write Commands (`trade.py`)

**All write commands require credentials.** Quantities and prices are rounded to the market's `qty_step_size` and `tick_size` with exact decimal arithmetic. Price and size arguments must be positive finite decimals. Fields that echo the submitted payload (`qty`, `limit_px_usd`, `trigger_px_usd`) are strings containing exactly what was sent.

| Command                                                                                   | Key arguments                                    |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `order market <symbol> --side S (--qty N \| --notional N \| --max_size \| --usage U) [--slippage N] [--sl L] [--tp L] [--trigger_slippage N] [--reduce_only] [--preview] [--force] [--allow_large] [--skip_margin_check]` | Market order (IOC with slippage); `--sl`/`--tp` arm a stop-loss and take-profit once it fills |
| `order limit <symbol> --side S --price N (--qty N \| --notional N \| --max_size \| --usage U) [--post_only] [--preview] [--force] [--allow_large] [--skip_margin_check]` | GTC limit order |
| `order modify (--order_id X \| --client_order_id X) [--symbol X] --price N --qty N [--force] [--allow_large] [--skip_margin_check]` | Modify a GTC limit (qty = intended total) |
| `order cancel (--order_id X \| --client_order_id X) [--symbol X]`                          | Cancel an open order                             |
| `order cancel_all (--symbol X \| --all) [--preview]`                                       | Cancel every open perp order in scope            |
| `order tp <symbol> --side S --trigger_px N --limit_px N --time_in_force IOC/GTC [--force]` | Take-profit trigger (closes whole position)      |
| `order sl <symbol> --side S --trigger_px N --limit_px N --time_in_force IOC/GTC [--force]` | Stop-loss trigger (closes whole position)        |
| `positions close_all (--symbol X \| --all) [--cancel_orders] [--slippage N] [--preview] [--force]` | Flatten positions (reduce-only IOC); optionally cancel orders first |

```bash
python -m agent_kit.scripts.trade order market eth --side long --qty 0.05 --preview   # sends nothing
python -m agent_kit.scripts.trade order market eth --side long --qty 0.05
python -m agent_kit.scripts.trade order market spy --side long --qty 1.55 --sl 0.35% --tp 0.5% --preview
python -m agent_kit.scripts.trade order market spy --side long --qty 1.55 --sl 0.35% --tp 0.5%
python -m agent_kit.scripts.trade order market ETHRUSDPERP --side short --notional 50 --slippage 0.02
python -m agent_kit.scripts.trade order limit btc --side short --price 80000 --qty 0.001 --post_only
python -m agent_kit.scripts.trade order cancel --order_id 123456789 --symbol eth
python -m agent_kit.scripts.trade order tp eth --side long --trigger_px 2300 --limit_px 2295 --time_in_force IOC
python -m agent_kit.scripts.trade order sl eth --side long --trigger_px 2000 --limit_px 1995 --time_in_force IOC

# Risk primitives: ALWAYS preview first
python -m agent_kit.scripts.trade order cancel_all --all --preview
python -m agent_kit.scripts.trade positions close_all --all --cancel_orders --preview
python -m agent_kit.scripts.trade positions close_all --all --cancel_orders
```

- `--side` is `long`, `short`, `buy` or `sell`. `--slippage` is a decimal fraction in
  `(0, 0.5]` (default `0.01` = 1%); above `0.05` needs `--force`.
- **Fat-finger guards** run before anything is signed, on `order market`, `order limit` and
  `order modify`. A limit price more than 5% through the mark (a buy above it, a sell below
  it) needs `--force`. An order above the per-order cap (`REYA_MAX_ORDER_NOTIONAL` in `.env`,
  default: the account value) or that takes estimated leverage above `REYA_MAX_LEVERAGE`
  (default 5) needs `--allow_large`. A refusal exits 1 with `refused_by` (each entry names its
  `override`, or `null` when there is none) and the computed figures; nothing was sent. An
  order that only reduces a position is never refused for size. **Never add `--force` or
  `--allow_large` on your own:** show the user the figures and the limit, get an explicit yes
  for that order, then re-run with the flag. Details in the
  [command reference](docs/command-reference.md#fat-finger-guards).
- `--preview` on `order market` / `order limit` returns `qty`, `notional_usd`, `limit_px_usd`,
  `limit_vs_mark_pct`, `account_value_usd`, `current_leverage`, `est_leverage`, `would_send`
  and `refused_by`, and sends nothing. Its `margin` block has the engine's verdict for a full
  fill at the limit price (`engine_check`: `ok`, `tight`, or the engine's reason with
  `engine_message`), `margin_usage_after`, `liquidation_price_after_usd`,
  `distance_to_liquidation_after_pct` and `max_qty`, the largest size that fits.
- **Margin check:** the kit refuses (`refused_by` check `engine_margin`) an order the matching
  engine would reject for margin, with the engine's own message. Reduce-only orders, TP/SL and
  `close_all` are never refused for margin. An opposite-side order without `--reduce_only` is
  an ordinary order to the engine; the preview's `reduce_only_hint` says so. Never add
  `--skip_margin_check` on your own; it is for when the kit is stricter than the engine and
  the user agrees.
- **Size by risk, never from `max_leverage`.** `max_leverage` is rounded and ignores the 1%
  settlement reserve, which on the 25x and 40x markets binds first; a market order is also
  charged its full slippage up front. For "as large as possible" use `--max_size`; for a risk
  level ("half way to liquidation") use `--usage 0.5`. Both stay within the kit's guards
  unless `--allow_large`, and only size an order that opens or adds to a position.
- Every create carries a client order ID. Pass `--client_order_id N` (1..2⁶⁴−1) or let the
  kit generate one; it is returned in the result, including error results. Keep it with
  the venue `order_id`. It does not make blind retries safe.
- Order and client IDs are market-scoped. Supply `--symbol` for cancel/modify; a bare ID
  works only if it resolves uniquely, and ambiguous matches fail before signing.
- `order market` exits 1 (response retained) unless status is `FILLED`, and with
  `--sl`/`--tp` also unless `protection.state` is `protected`. A returned `order_id` is
  not proof of a fill; partial, zero-fill and `UNKNOWN` outcomes need readback before
  another write. Read `protection` even when the command exits 1. `order limit` and
  triggers accept `OPEN` or `FILLED`.
- Create and close responses add `fees_status`, `fees_rusd` and `executions` from one
  bounded execution read (`--fees_wait_s 1..5` polls longer). A fee-read failure never
  changes the order outcome or retries the write.
- TP/SL close the **whole live position** when the mark crosses `trigger_px`; `--side`
  names the position being protected. Firing does not guarantee a fill, and a fired child
  cancels its sibling. Position reads trail fills by about a second, so `order tp` /
  `order sl` read the position for up to 4 seconds before deciding. A position still
  missing or on the other side after that, or a trigger that would fire immediately, is
  rejected unless `--force` (the response then carries `warning`). Never use `--force` to
  get past a lagging position: read `query positions` again.
- **Entries with protection: use `order market --sl L --tp L`.** Do not place the entry
  and then `order sl` / `order tp` separately. `L` is a percent of the entry's average
  fill price (`0.35%`) or a price (`5790`); either flag can be used alone. After the fill,
  the command waits up to 5 seconds until position reads include it, arms the stop-loss,
  then the take-profit (IOC children `--trigger_slippage` beyond each trigger, default
  `0.01`, at most `0.03`), reads both back, and returns `protection`:
  - `state`: `protected` (everything requested is armed); `partial` (stop-loss armed,
    take-profit not); `unprotected` (the entry filled and the requested stop-loss is not
    armed, or nothing requested was armed); `not_placed` (the entry filled nothing);
    `unknown` (the entry's outcome is unknown; nothing was placed); `pending` (the command
    stopped while placing them).
  - `stop_loss` / `take_profit`: `trigger_px_usd`, `limit_px_usd`, `order_id`,
    `client_order_id`, `armed`, `visible` (seen in the read-back), `error`, and `command`
    (the exact `order sl`/`order tp` to arm it by hand); `null` when not requested.
  - `message` says what is armed or what is missing, in words you can pass on.
  - Partial fills are protected as filled (the triggers close the whole position). It
    never resends the entry or retries a trigger, and never closes the position itself.
  - Refused before sending (in `refused_by`): a stop-loss or take-profit of the same kind
    already armed on that market (it already covers the whole position), an open position
    on the other side (this would reduce or flip it), or a level already through the mark.
    Not with `--reduce_only`, and not on `order limit`: place the limit, then arm
    protection once `query positions` shows the fill. Details in the
    [command reference](docs/command-reference.md#entry-protection).
- **State the protection in every reply after a protected entry:** the stop-loss and
  take-profit prices from `protection`, or that one was not requested; if `verified` is
  false, add that open-order reads do not show them yet and check `query orders`. If
  `state` is `unprotected`, `unknown` or `pending`, start the reply with "Your position is
  UNPROTECTED" (for `unknown` and `pending`: "may be UNPROTECTED") and the reason from
  `message`. Then read `query positions` and `query orders --symbol X`; if the position
  is there without a stop-loss, run the stop-loss `command` from `protection` once (or
  `order sl` at the agreed price; it waits for the position again). If that fails too,
  tell the user at once and ask whether to close the position
  (`positions close_all --symbol X`, preview first). Never repeat the entry. On
  `partial`, say the take-profit is missing and offer its `command`.
- `cancel_all` covers perp orders only. Individual failures are collected under `errors`
  with a top-level `error` and exit 1.
- `close_all` sends the venue's position quantity verbatim. `--cancel_orders` first tries to
  cancel each open perp order in the same scope. Only confirmed cancellations stop an order
  reopening exposure: a failed cancel is reported under `errors` (context
  `Cancel before close`) while flattening continues, so after any such error run
  `query orders` and `query positions` before calling the account flat. Positions below `min_order_qty` are flagged
  `below_min_order_qty: true`; the API rejects those closes, and the `hint` explains the
  only exit (add at least the minimum, then close the whole position). `closed` counts
  only fills confirmed by a position readback; any `remaining_positions`, unconfirmed
  closure or batch error exits 1. Read positions again before another close.

### Before every live order

Run each live `order market` / `order limit` with `--preview` first and tell the user in
one line: side and size, **notional in USDC**, **estimated leverage** after the fill
(`est_leverage`), and **distance from the mark** (`limit_vs_mark_pct`; for a market order it is
the worst price accepted). For an order that opens or adds to a position, add **margin usage
after the fill** and the **liquidation price with its distance** (`margin.margin_usage_after`,
`margin.liquidation_price_after_usd`, `margin.distance_to_liquidation_after_pct`). With
`--sl`/`--tp`, add the stop-loss and take-profit trigger prices from `protection_plan` and the
estimated result at the stop: `est_pnl_at_trigger_usd`, and `est_pnl_at_limit_usd` at the child
limit, the worst price the fired child accepts (signed, before fees). Then:

- **Ask for an explicit yes and wait** when any of these holds: it is the first live order in
  this conversation; the notional is more than 25% of `account_value_usd`; `est_leverage` is
  above 2; `margin.margin_usage_after` is above 0.5 or `margin.engine_check` is `tight`; or
  `refused_by` is not empty (name the limit and the flag that would override it). If the
  refusal is `engine_margin`, offer `margin.max_qty` or `--usage` instead.
- Size the next order only after the previous fill shows in `query positions`: the kit cannot
  see fills the engine has matched but not yet settled.
  An instruction such as "buy 0.5 ETH" is not a yes to figures the user has not seen.
- Otherwise, when the user asked for exactly this order, place it and report the same figures
  with the result.
- Preview again if anything changes (size, price, or a market move before the yes).

### Sliced entry with a bracket

The venue has no TWAP order. To build a position in slices with a stop-loss and a
take-profit, run the slices yourself in this session. Do not use the runner for this.

1. **Agree the whole plan once:** market, side, total notional in USDC, number of slices and
   interval, per-slice slippage (keep it tight, for example `0.002`–`0.005`), and the
   stop-loss and take-profit trigger and limit prices. Preview the first slice and the
   total; the 25% and leverage rules above apply to the total.
2. **Slice 1 carries the protection:** `order market <symbol> --side S --notional <total ÷ slices>
   --slippage 0.003 --sl <stop> --tp <target>`, with the agreed stop and target as prices
   (percent levels would follow this slice's fill, not the whole plan's average).
3. **Check `protection.state` is `protected`.** The stop-loss and take-profit are venue-side,
   close the whole position when they fire, and so cover every later slice with no change.
   The venue allows one of each per market, so later slices carry no `--sl`/`--tp` (the kit
   refuses a duplicate before sending). If slice 1 reports anything else, follow the rule
   above for an unprotected entry and send no further slice until the stop is armed.
4. **Before each later slice,** read `query positions --symbol X` and `query orders --symbol X`.
   **Stop slicing** if the position is gone or smaller than what has filled, or if the SL or
   TP is no longer open (a trigger fired or was cancelled). Never add to a position whose
   stop has fired, and never re-arm protection without asking.
5. **Report any shortfall; never chase.** A slice that fills partly or not at all exits 1 with
   `filled_qty`: read the account, note the shortfall, and continue with the next slice at
   the planned size and slippage. Do not enlarge later slices, widen slippage or add slices
   to make up for it. At the end report filled versus planned, the average price and the
   armed SL/TP, and ask before topping up.
6. If the session stops part-way, the SL and TP stay armed at the venue and no further
   slice is sent. Tell the user what filled.

---

## Health Check

`python -m agent_kit.scripts.health` returns `status`, `api_url`, `sdk_version`,
`markets_available` and `latency_ms`; on failure it exits 1 with the error envelope plus
`api_url` and `sdk_version`.

---

## Safety Notes

- **Live orders on Reya are irreversible.** Review order parameters carefully.
- **`cancel_all` and `close_all` (with or without `--cancel_orders`) are high-impact writes requiring `--symbol` or explicit `--all`.** Always run them with `--preview` first, show the user the `would_cancel` / `would_close` plan, including any `drops_protection` (the TP/SL that stop protecting once the position is flat), and get explicit confirmation in plain language before the real call. Never infer intent from vague phrases like "clean up", "reset", or "start fresh".
- **One writer at a time per shared owner/signer nonce context.** The base SDK nonce lock is process-local. Updated kit commands add owner/signer leases and durable nonces shared by local kit checkouts. Other bots/hosts/state directories do not participate and can still race with `INVALID_NONCE`. Stop competing writers or integrate them with a coordinated writer service. A separate subaccount alone does not establish nonce isolation. See [the operator guide](docs/mainnet.md#account-isolation-and-concurrent-writers).
- TP/SL triggers must match an open position (existence and side, read for up to 4 seconds) and would-fire-immediately prices are rejected — both unless `--force` is passed. `order market --sl/--tp` arms them only once position reads include its fill, and says UNPROTECTED when it could not.
- Slippage is capped at 50% outright, and values above 5% require `--force`. Limit prices more than 5% through the mark require `--force` (more than 50% is refused).
- Orders above the per-order cap (default: the account value) or above the leverage limit (default 5x) require `--allow_large`, which you may add only after the user's explicit yes to that order.
- Market orders are IOC limits at `mark × (1 ± slippage)` — the fill price may differ from the mark price.
- Quantities are automatically rounded down to the market's step size — except `close_all`, which closes the venue's reported position quantity verbatim.
- Never commit private keys to any repository.
- Never read or print `.env` or private key material — see the Credentials section.

- `REGION_UNAVAILABLE` means the API is geoblocked for the user; never suggest circumventing it.

---

## Account setup and runner commands

The agent kit must use supported Reya APIs for account setup and trading. Do not introduce direct RPC dependencies, transaction broadcasting or user-managed gas requirements. Missing SDK support is an API integration gap, not justification for an RPC fallback.

See [the complete operator guide](docs/account-setup-and-runner.md) for parameters, API gaps, state recovery and the live-validation checklist. Account setup currently uses the allowlisted `executeReya` relay; migrate its isolated adapter to dedicated operation endpoints after deployment is verified. Never retry a failed write through another route.

- `python -m agent_kit.scripts.account create` previews a new margin account. `--execute --request-id ID` relays unsigned `createAccount`: no owner key or signature is needed. Confirmation requires a relay-returned account ID and matching owned margin-account read; the first spot account is not returned as the margin account.
- `python -m agent_kit.scripts.account transfer --from-account A --to-account B --amount N` previews an exact six-decimal rUSD transfer between owned margin accounts. `--execute --request-id ID` signs the Core authorization using `REYA_ACCOUNT_OWNER_KEY_FILE`, the owner nonce API and a 60-second deadline, then relays `transferFromMAToMA`. Reya's executor submits the transaction and pays gas. No bridge, deposit, allowance or permission changes are implied.
- `python -m agent_kit.scripts.account status [--request-id ID]` reads the saved relay outcome and supported account/transfer APIs. Transfer confirmation requires matching transaction hash, both accounts, asset and exact debit/credit amounts. If a transfer response was lost, an operator can supply an independently recovered `--tx-hash HASH`; it is saved only if it verifies this request's exact transfer, and a hash already recorded for another request is refused. Without a hash, status confirms only from a unique matching observation after the request (`verification: "unique_observation_after_request"`; see the operator guide for its limitations). A creation hash alone is insufficient with current APIs.
- Reuse the same request ID for the same intent. A completed ID returns its saved outcome; an uncertain ID is never submitted again, and blocks a new ID. Do not delete journals or change IDs to bypass uncertainty. The relay lacks a request-status/idempotency lookup, and wallet differences or missing history do not prove an outcome. Keep unresolved requests blocked and escalate the API gap.
- `python -m agent_kit.scripts.runner --policy PATH` previews account state/policy without loading a key. Add `--execute --ack-account-wide-cancel` only for a deliberately dedicated, flat, empty account.

Without `--request-id`, account status also lists all locally unresolved records in `pending_requests` (including API deployment and transaction reference); those entries are journal summaries, not fresh reconciliation of every operation. Use each exact request ID for targeted reconciliation.

A live runner receives JSON lines of `{"id":"unique-id","command":["order","limit",...]}`. Wait for its ready event. It reuses the trade interface, serializes writes, enforces symbol/slippage/submission budgets, and refreshes a venue 30-second account-wide cancellation timer every five seconds. `status` and `stop` are special one-element command arrays. Status includes full order details as `orders`, while retaining `open_order_ids`. Idle timeout measures only waiting for the next input, excluding command execution. If the monotonic heartbeat deadline is missed, the runner halts, cancels and requires reconciliation; it never silently rearms after a stall. TP/SL inside the runner retry missing position reads for up to three seconds (four-second total read deadline), then refuse to submit if the owned position with the expected side is still unavailable. They never infer protection from the entry acknowledgement or repeat the entry. Stdin must be a terminal or pipe; use `cat commands.jsonl | python -m agent_kit.scripts.runner ...` for a command file, not direct file or `/dev/null` redirection. The kit's fat-finger guards apply inside the runner too. Do not bypass an error with `--force` or `--allow_large` (both unavailable), recreate an uncertain order, or reset nonce/journal files. Operators must reconcile unfinished sessions before acknowledging the exact prior session ID.

**Neither the runner's exit nor the venue timer closes positions, and neither removes TP/SL.** When the venue's countdown fires (process killed, laptop asleep), it cancels every open order except TP/SL. On every exit the runner handles itself (stop, EOF, idle or command limit, heartbeat failure, a caught error) it cancels its working orders: with no position and no TP/SL, one account-wide cancel; otherwise each non-protective order individually, because an account-wide cancel would also clear the position's TP/SL. The `stopped` event reports `cleanup` and `protection`: each remaining position with the stop-loss and take-profit still armed for it, or `null`. Tell the user about any position left open, and say plainly when it has no stop. Only a clean, flat exit disarms the countdown. Limits govern submissions, not losses or mark-to-market exposure. Account setup and runner live paths require their own operator rehearsal; earlier canaries do not establish their acceptance.

## How to Handle User Requests

1. Parse the user's intent: which symbol, direction, and size.
2. Use `query positions` to check existing exposure before trading.
3. Use `query prices --symbol X` (and `query market info` for minimums and steps) before sizing. If the user asks for less than the market minimum (for example $20 of ETH when the minimum is 0.01 ETH), say so with the minimum in USDC before preparing anything, and offer the minimum instead.
4. Preview with `trade order market` / `trade order limit` `--preview`, state the notional, leverage and distance from mark, confirm as in [Before every live order](#before-every-live-order), then execute.
5. Use `query orders` to verify open orders and `query executions` for fills and fees.
6. Use `trade order cancel` to cancel orders when needed.
7. Before `order cancel_all` or `positions close_all`, run the same command with `--preview`, show the user what would be affected, and proceed only on explicit confirmation.
8. Parse the JSON response and present results clearly. If the user is unsure whether they want real funds, use `paper`.

## Local paper trading (virtual funds only)

`python -m agent_kit.scripts.paper` is always local simulation, regardless of
live environment settings. It does not use wallet/key configuration. All commands
emit JSON. See the [simulation contract and demo](docs/paper-trading.md).

- Global `--directory /absolute/private/path` selects the ledger directory.
- `init --collateral 10000 --fee-bps 3 [--api-url <supported-v2-url>]`: create a virtual account with an explicit assumed taker fee (`query fees` shows the live tier rates).
- `order market <symbol> --side long|short|buy|sell --qty <base-qty> [--slippage 0.01] [--reduce_only] --client_order_id <id>`: the same shape as the live market order, so `trade` can be swapped for `paper`. `--request-id` is accepted instead of `--client_order_id`; one of them is required. Walks live snapshot depth and cancels the unfilled remainder.
- `order --symbol <symbol> --kind ioc --price <limit> ...`: immediate-or-cancel at a limit. `order limit` (resting GTC) is refused.
- `state`: value positions at fresh marks without recording a time series point.
- `refresh`: record a local simulated equity observation.
- `history [--offset 0] [--limit 100] [--snapshots]`: inspect local fills or recorded observations.
- `reset --confirm-ledger-id <current-id> --collateral 10000 --fee-bps 3`: replace this virtual ledger after exporting history.

Retry an uncertain paper command using exactly the same request ID and parameters.
A persisted outcome is replayed without new fills. Do not automatically reset
state on errors. Resting, post-only and protective orders are unsupported; never
substitute a live command. Funding, ADL and liquidations are not simulated.
Paper and the live API both refuse a reduce-only close below the market minimum;
see [paper cleanup](docs/paper-trading.md#subminimum-residual-cleanup).

## Account-period history and performance

`python -m agent_kit.scripts.history --wallet <public-owner> --account-id <id>
--start-time <unix-ms> [--end-time <unix-ms>] [--symbol ETHRUSDPERP]
[--max-pages 10] [--include-current-state]` reads supported V2 history. Use
`--whole-wallet` instead of `--account-id` for explicit wallet scope. No key is needed.
See [metric meanings, missing data and examples](docs/history-and-performance.md).

Report per-feed coverage alongside every metric. `observed_sum` is not a verified
period total. Do not add execution PnL to transfer fees, treat deposits as profit,
or substitute zero for missing metrics. Current account state is observed now,
not a historical interval endpoint. Server equity series are unavailable; paper
snapshots are local simulated observations only.

History `side` is account-relative, including maker inversion; `taker_side` is
the raw venue direction. Zero-sequence legacy executions are retained as
unidentified observations, with unverified sums suppressed. Runner shutdown must
positively reconcile acknowledged fills/busts before flat reads clear its restart
gate; see the [runner settlement contract](docs/account-setup-and-runner.md#settlement-evidence-and-remaining-api-limits).
