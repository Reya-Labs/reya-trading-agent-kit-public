# Local paper trading: simulation contract v2

Paper mode is a credential-free virtual account driven by current public Reya
V2 books and marks. Orders are admitted by the matching engine's margin rules
(the kit's port in [margin.md](margin.md)), so a leveraged order can be rehearsed
before it is sent for real: the same command shows whether the engine would take
it, the margin usage and liquidation price after the fill, and what a stop-loss
does. Paper accrues funding, can liquidate the virtual account, and fires TP/SL
on the mark. It is not historical backtesting, a replay of matching-engine
behavior, or evidence that a strategy or production account is safe.

## First paper session

After a private installation, run these with the kit's `.venv/bin/python` (or
`poetry run python`). No `.env`, wallet, key or deposited funds is required.

```bash
python -m agent_kit.scripts.paper init --collateral 10000 --fee-bps 3
python -m agent_kit.scripts.paper order market eth --side long --qty 0.01 --client_order_id 1
python -m agent_kit.scripts.paper state
python -m agent_kit.scripts.paper refresh
python -m agent_kit.scripts.paper history
python -m agent_kit.scripts.paper history --snapshots
```

`3` bps is an **assumption chosen by you**. `query fees` lists the live tier
rates (and, with an owner configured, that wallet's tier) to base it on; the kit
never copies a live tier into a ledger. Inspect current `data market search --symbol ETH` minimum/step before
selecting quantity; the example can fail when market constraints change. Paper
supports mainnet public data by default and devnet1 with an explicit `init
--api-url https://api-devnet.reya-cronos.network/v2`.

`paper order market <symbol>` takes the same shape as the live
`trade order market`, so a tested command can move between them by swapping the
script name. `--client_order_id` (or `--request-id`) is required in paper: it is
the replay key. The flag form `paper order --request-id ID --symbol ETHRUSDPERP
--side buy [--kind ioc --price P]` still works; `--kind ioc` is how to send an
immediate-or-cancel at a limit price. `paper order limit` is refused because paper
cannot rest a GTC order. Symbols accept bare tickers (`eth`).

Repeat the exact command: the saved result returns with `replayed: true`,
without another fill or even a market read. Inspect `filled_qty`, `cancelled_qty`,
fees and balances rather than assuming a requested size executed. Close only
observed size using a new ID and `--side short --reduce_only --qty <size>`.

Restarts retain the ledger. `--directory /absolute/private/path` (before the
subcommand) selects another isolated ledger. Export `history --limit 10000` and
`history --snapshots --limit 10000` before resetting. Reset is explicit:

```bash
python -m agent_kit.scripts.paper reset --confirm-ledger-id <current-ledger-id> --collateral 10000 --fee-bps 3
```

Reset starts a new ledger and replaces old history/request IDs. Never reset as a
retry mechanism. Directory defaults to `~/.local/state/reya-agent-kit/paper`.

## Rehearsing a leveraged order

Reya has no leverage setting: leverage is the order's size against the account's
cross-margin equity. Rehearse the live command itself, sized the same way:

```bash
python -m agent_kit.scripts.paper order market eth --side long --usage 0.5 --sl 2% --tp 3% --client_order_id 2
python -m agent_kit.scripts.paper state
```

- `--qty`, `--max_size` or `--usage U` (one of them). `--max_size` and `--usage`
  size exactly as live does ([margin.md](margin.md#sizing---max_size-and---usage)),
  with the default 0.2% headroom. Paper has no `REYA_MAX_ORDER_NOTIONAL` or
  `REYA_MAX_LEVERAGE` guards, so `sizing.qty_by_limit` lists only `engine_margin`
  and `usage`. `--notional` is not accepted.
- The result's `margin` block is the live preview's: `engine_check`, usage and
  liquidation price after a full fill at the limit price (the worst case),
  `settlement_reserve_usd`, `max_qty`. `account_after` is the ledger after the
  simulated fill at the same marks: `equity`, the `margin` figures and the
  position with its `liquidation_price`.
- An order the engine would reject is refused with its message, `error_code`
  (`ACCOUNT_BELOW_INITIAL_MARGIN_ERROR` or `ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR`),
  `refused_by: [{"check": "engine_margin"}]`, `margin.shortfall_usd` and
  `margin.max_qty`. Nothing is simulated and the request ID stays unused.
- `--sl`/`--tp` are armed from the average fill price, as live
  (`protection.state`: `protected`, `partial`, `unprotected` or `not_placed`).

## Supported behavior

| Topic | Contract |
| --- | --- |
| Instruments/order types | Perpetuals only; market-style orders are mark/slippage-bounded IOC; explicit limit IOC supported. No resting orders except TP/SL. This mirrors the kit's live market-as-IOC convention, not an unlimited market sweep. |
| Size/price | Base quantity strings must satisfy V2 `qtyStepSize` and `minOrderQty`, reduce-only closes included, as the live API requires; no upward rounding or position flip. IOC prices must satisfy `tickSize`; market bounds round outward to ticks. No float conversion. User values limited to 18 decimals and magnitude 1e20. |
| Depth/partial fills | Fresh REST snapshot, up to 1,000 levels per side; walk opposing depth within the bound, partially fill if needed, cancel remainder. Empty/noncrossing book yields a recorded cancellation. Never fabricate depth beyond the returned levels. |
| Fees | Explicit immutable per-ledger assumed taker fee rate in bps, charged per simulated fill on filled quantity × the observed V2 `markPrice` (not the fill price), as Reya settlement charges taker fees on the on-chain mark. Like settlement, the notional rounds away from zero and the fee rounds down, both to 6 decimals (rUSD). No inferred live tier, maker fee or rebate. Change assumptions with a separate ledger/reset. |
| Position accounting | Signed quantity and signed cost basis; reducing fills realize price PnL, flips close the old side then open the residual. Released partial basis rounds toward zero to 18 decimals; closing the final quantity releases all residual basis. Realized cash PnL rounds down to 18 decimals. |
| Margin admission | The engine's three checks on the order projected as fully filled at its limit price: health ≥ 0 before; equity covers initial margin before and after; health after covers the 1% settlement reserve on the order's notional. Real balance = cash (virtual rUSD, no haircut, floored to 6 decimals) + unsettled funding; equity adds unrealized PnL at the V2 mark. Liquidation-margin rates and the IM multiplier come from `perpMarketDefinitions`. A reduce-only order owes the first check only; an opposing order without `--reduce_only` owes all three. Same messages and error codes as live. |
| Funding | V2 publishes a cumulative funding value per unit of base for each market and side (`longFundingValue`, `shortFundingValue`). Each position remembers the value at its last fill. Funding since then, −(value now − value then) × signed quantity, is unsettled; it counts in equity and margin, credit or debit, and is settled into cash at the next fill on that market, as Core does. Rounded down to 6 decimals. `funding` is the settled total, `unsettled_funding` the rest. |
| Liquidation | Checked at every command that reads marks (`state`, `refresh`, `order`, `order sl|tp`), after TP/SL. Equity below the liquidation margin liquidates the whole account: every position closes at the observed marks (price PnL and funding settled), the remaining balance goes to the pool (`penalty_to_pool`), and cash is set to 0. An insolvent account also ends at 0; the deficit is reported as `uncovered_loss` (live, the insurance fund covers it). TP/SL are dropped. History gets a `liquidation` event with marks, equity, liquidation margin, usage and `last_safe_observation_ms`. An `order` that finds the account liquidatable returns the liquidation with `status: not_simulated`, fills nothing, leaves its request ID unused, and exits 1. |
| TP/SL | `order market --sl/--tp` (percent of the average fill price, below 50%, or a price; `--trigger_slippage`, default 1%, at most 3%) and `order sl|tp <symbol> --side --trigger_px --limit_px --time_in_force IOC [--force]`, parsed and rounded as live. Whole position, one of each per market, only on a matching position, refused when one is armed (cancel first) or when it would fire at once (`order sl|tp --force` arms it anyway). `order cancel_all --symbol S | --all` removes them. At each observation, before the liquidation check, a trigger fires when the mark reaches it (a long's stop-loss at mark ≤ trigger, take-profit at mark ≥ trigger; mirrored for a short); stop-loss first. The child is an IOC for the whole position against the current book at the stored limit, with normal fees and funding settlement. A fired trigger is used up even if its child fills partly or not at all (`remaining_position`). A flat or flipped position drops its triggers (`dropped_triggers`). History gets a `stop_loss` or `take_profit` event. |
| Freshness and clock | Book and mark timestamps must be at most 30 seconds old and at most 5 seconds ahead of this computer's clock, the runner's band. Every command that reads V2 reports `clock`: this computer's clock minus Reya's, from the response's HTTP `Date` header (`offset_ms`, `uncertainty_ms`, about ±0.5 s). From 2 s it adds `clock_warning`. Data outside the band refuses the command with the skew and the fix (on WSL, `sudo hwclock -s` or `wsl --shutdown`); old data is blamed on the clock only when the server's clock says it is fresh. The ledger is unchanged. There is no flag to widen the band. Definitions and book/marks are separate reads, not an atomic engine snapshot. |
| Durability | SQLite transaction atomically saves state and request outcome. Simultaneous writers fail with a lock error, never partially mutate. Request IDs are ledger-local; same parameters replay, changed parameters reject. 10,000 history entries and 10,000 snapshots maximum; explicit export/reset required at the bound. |
| History | Order outcomes, TP/SL fills and liquidations (`event`: `stop_loss`, `take_profit`, `liquidation`) survive restart. `refresh` records equity observations only when invoked; no invented points between observations or before initialization. `state` does not append a point, but it is an observation: it can fire TP/SL or liquidate, and saves the ledger. |
| Cash identity | cash = initial collateral + realized price PnL − fees + settled funding − liquidation penalties + uncovered losses. Equity = cash + unrealized PnL + unsettled funding. Deposits are not profit. |

## How faithful it is

| | |
| --- | --- |
| Matches live | The admission formula, its messages and error codes; the settlement reserve; the IM multiplier; the liquidation price; the liquidation line (equity below the liquidation margin); funding from the values V2 publishes; TP/SL trigger and child-limit rounding |
| Paper is more lenient | Paper settles instantly, so it has no matched-but-unsettled fills: live, an order seconds after a fill also reserves for that fill and can be rejected where paper admits it. Virtual rUSD has no haircut. Liquidation and TP/SL are checked only when a command runs, so a dip that recovers between commands is missed. |
| Paper is harsher | Liquidation always takes everything, at the 100% line. Live usually starts with partial liquidations (some exposure at a discount, plus fees) and pushes the whole position and margin balance to the pool only at a 125% margin ratio, or when partial liquidation fails. |
| Can go either way | The mark moving between paper's read and the engine's check (as with the live preview); fills from one book snapshot; fees on the V2 mark; funding only as fresh as the published value; a TP/SL child filled against the book at the next command rather than when the mark first crossed |

When paper first sees the mark past both a stop-loss and the liquidation line, the
stop-loss fires first only if it sits above the position's liquidation price (below
it, for a short): on the way there the mark reached the stop first. A stop beyond
the liquidation price loses to the liquidation. For one position this is exact;
with several, the liquidation price assumes the other marks unchanged. A fired
child is not re-checked for margin: live, it fired when the mark crossed the
trigger.

## Unsupported behavior

- **ADL, insurance fund, partial liquidation and liquidation fees:** not modeled.
  Their parameters are not on V2. `adl` stays null.
- **Collateral:** virtual rUSD only. No deposits or transfers after
  initialization, no other collateral, haircuts or auto-exchange. A cross-margin
  account whose real balance is negative while equity is above the liquidation
  margin (after closing a loser with a winner open) is refused every order, closes
  included, as the engine does; live, adding collateral fixes it, in paper only a
  reset or a mark move does.
- **Order lifecycle:** no resting maker, post-only, GTC TP/SL child, modification,
  order expiry, or live runner. Unsupported kinds/flags fail parsing; do not
  replace them with real writes.
- **Timing:** fills use one observed snapshot, with no transport latency, queue
  priority or stochastic fill model. Partial fills reflect displayed depth/price
  only. The next command may consume the same externally visible depth again.
  TP/SL and liquidation happen only when a command runs.
- **Impact:** simulated orders never remove actual liquidity or move the market.
  No bust or market-close simulation.
- **Scope:** sub-account setup, spot, portfolio collateral or historical backtest.

The `paper` module does not import trading/account/relay clients and only issues
public GETs. Its persisted API URL is selected from supported mainnet/devnet1 V2
URLs. Live `.env`, keys, account IDs, `REYA_MODE`, `REYA_API_URL` and
`REYA_MARGIN_HEADROOM` are ignored. The live CLI still rejects `REYA_MODE=paper`
and directs users to this command; no flag converts a paper operation into live
execution.

## Ledgers from earlier versions

A ledger made before 0.10.0 (schema 1) continues in place on its first use: same
cash, positions, history and request IDs (a recorded request still replays). It is
then admitted by the engine rules instead of the old 1x rule, can be liquidated,
and its open positions accrue funding from that first observation
(`funding_since_ms`; nothing before it is charged). `upgraded_at_ms` records when.
Kits before 0.10.0 refuse the upgraded ledger.

## Explained accounting fixture

Start with 1,000 virtual rUSD and an assumed 10 bps fee. Ask book: 1 ETH at 101,
2 ETH at 102; mark 100. Buy 2 ETH IOC with limit 101: fill 1, cancel 1, fee 0.100
(10 bps of 1 × mark 100, not of the 101 fill). Cash is 999.900, unrealized PnL −1,
equity 998.900. Repeat the request ID: unchanged. Close that 1 ETH at bid 110 with
mark 110: realized price PnL 9, closing fee 0.110, final cash 1,008.790 and no
position.

## Explained leverage fixture

The offline tests use mainnet SPY's parameters: liquidation margin 3.077% of
notional, initial margin 1.3 times that (a 25x market). Start with 1,000 virtual
rUSD, no fee, mark 100, asks at 100:

- **Admission.** An IOC long at 100 for 245 is admitted (24.5x). 247 is refused:
  it passes initial margin but leaves health below the 1% settlement reserve. 250
  fails initial margin. These are the engine's own vectors (`test_margin.py`).
- **A 10x long.** Buy 100 at 100: margin usage 0.3078 (liquidation margin 307.73
  on equity 1,000), liquidation price 92.86, the same figures `query margin` gives
  for a live account with 1,000 USDC long 100 at 100.
- **Liquidation.** At mark 92.9 equity is 290 against a liquidation margin of
  285.88: safe. At mark 92.8 equity is 280 against 285.58: the account is
  liquidated. The position closes at 92.8 (realized −720), the remaining 280 goes
  to the pool, and cash is 0.
- **A stop-loss.** Long 10 at 100 with `--sl 2% --tp 3%`: the stop-loss triggers at
  98 with child limit 97.02, the take-profit at 103 with child limit 101.97. When a
  command sees mark 97.5, the stop fires and sells 10 at the 97.6 bid (realized
  −24); the take-profit is dropped with the position. Against bids at 96, below
  the child's limit, nothing fills, the stop is used up and the position stays
  open with its take-profit.
- **Funding.** Long 10 opened when the long funding value was 5; at 5.5 the
  position owes 5 (−0.5 × 10), equity is 995, and the next fill on the market
  settles the 5 into cash. A short receives it.

## Model scope and API dependencies

Paper keeps a local ledger and fills against the live public book. It models
depth-limited partial fills, engine margin admission, funding, whole-account
liquidation and TP/SL at observations; it does not model ADL, partial
liquidation, maker orders, latency or market impact, and claims no fidelity beyond
that.

Unsigned GETs only: `/v2/perpMarketDefinitions`, `/v2/perpMarkets/summary`,
`/v2/market/{symbol}/depth`. No new non-V2 dependency. Exact account fee tier and
full ADL behavior are intentionally not inferred from missing APIs.

## Subminimum residual cleanup

An IOC requesting 0.001 BTC can fill only 0.0001 BTC, leaving a position below
the market minimum. Paper refuses to close it with a reduce-only order because
the live API does: on devnet1 on 11 October 2026 a reduce-only 0.001 ETH close was
rejected with `PRICE_QTY_BOUNDS_ERROR` ("below minimum order base"), and a
reduce-only 0.01 ETH close of that 0.001 position with
`REDUCE_ONLY_CONDITION_NOT_MET_ERROR`. The API checks the minimum on every order,
so the matching engine's reduce-only
exemption is not reachable through it.

To exit, add at least the minimum with a non-reduce-only order, then close the
whole position with one reduce-only order, or leave the residual. Never round a
requested quantity up or reset the ledger to pretend it was closed. Prefer entry
sizes the visible depth can fill completely.
