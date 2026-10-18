# Local paper trading: simulation contract v1

Paper mode is a credential-free virtual account driven by current public Reya
V2 books and marks. It helps learn commands and inspect basic accounting. It is
not historical backtesting, a replay of matching-engine behavior, or evidence
that a strategy or production account is safe.

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

## Supported behavior

| Topic | Contract |
| --- | --- |
| Instruments/order types | Perpetuals only; market-style orders are mark/slippage-bounded IOC; explicit limit IOC supported. No resting orders. This mirrors the kit's live market-as-IOC convention, not an unlimited market sweep. |
| Size/price | Base quantity strings must satisfy V2 `qtyStepSize` and `minOrderQty`, reduce-only closes included, as the live API requires; no upward rounding or position flip. IOC prices must satisfy `tickSize`; market bounds round outward to ticks. No float conversion. User values limited to 18 decimals and magnitude 1e20. |
| Depth/partial fills | Fresh REST snapshot, up to 1,000 levels per side; walk opposing depth within the bound, partially fill if needed, cancel remainder. Empty/noncrossing book yields a recorded cancellation. Never fabricate depth beyond the returned levels. |
| Fees | Explicit immutable per-ledger assumed taker fee rate in bps, charged per simulated fill on filled quantity × the observed V2 `markPrice` (not the fill price), as Reya settlement charges taker fees on the on-chain mark. Like settlement, the notional rounds away from zero and the fee rounds down, both to 6 decimals (rUSD). No inferred live tier, maker fee or rebate. Change assumptions with a separate ledger/reset. |
| Position accounting | Signed quantity and signed cost basis; reducing fills realize price PnL, flips close the old side then open the residual. Released partial basis rounds toward zero to 18 decimals; closing the final quantity releases all residual basis. Realized cash PnL rounds down to 18 decimals. |
| Cash/equity | Cash = starting virtual collateral + realized price PnL − simulated fees. Unrealized PnL = signed quantity × fresh mark − signed basis. Equity = cash + unrealized PnL. Deposits are not profit. |
| Virtual collateral | Conservative **1x gross-notional rule**, not Reya margin: opening/increasing/flipping exposure requires post-fill equity ≥ total gross marked notional. Insufficient funds reject the whole simulated command atomically. Pure reductions remain possible even with negative equity. No leverage setting or exact live risk admission. |
| Freshness | All relevant marks/oracles and book timestamps must be no older than 30 seconds and not in the future at execution. Definitions and book/marks are separate reads, not an atomic engine snapshot. |
| Durability | SQLite transaction atomically saves state and request outcome. Simultaneous writers fail with a lock error, never partially mutate. Request IDs are ledger-local; same parameters replay, changed parameters reject. 10,000 order outcomes and 10,000 snapshots maximum; explicit export/reset required at the bound. |
| History | Orders/fills and fee/PnL outcomes survive restart. `refresh` records equity observations only when invoked; no invented points between observations or before initialization. `state` does not append a point. |

## Unsupported behavior and divergence from live

- **Funding and ADL:** not accrued/applied; output fields are null, not a claim of
  zero real-world cost. Holding positions over funding intervals can materially
  overstate or understate results. Public live marks do not make this a funding simulator.
- **Fee base:** live settlement uses the mark last pushed on-chain at the settlement block, which paper
  cannot observe; paper uses the V2 summary mark read with the book. Expect a per-fill difference of
  rate × quantity × (mark drift between that read and settlement), plus up to 0.000001 rUSD per
  matched maker order because paper fills a whole price level at once.
- **Liquidation/margin:** no liquidation engine, maintenance-margin trajectory,
  insurance fund or collateral conversion; liquidation price is null. Virtual
  1x headroom is not a Reya margin balance or live leverage recommendation.
- **Order lifecycle:** no resting maker, post-only, stop loss, take profit,
  cancellation/modification of resting orders, order expiry, or live runner.
  Unsupported kinds/flags fail parsing; do not replace them with real writes.
- **Timing:** fills use one observed snapshot, with no transport latency, queue
  priority or stochastic fill model. Partial fills reflect displayed depth/price
  only. The next command may consume the same externally visible depth again.
- **Impact:** simulated orders never remove actual liquidity or move the market.
  No matching-engine in-flight exposure, bust, liquidation or market-close simulation.
- **Scope:** virtual rUSD only, no transfers/deposits after initialization,
  sub-account setup, spot, portfolio collateral or historical backtest.

The `paper` module does not import trading/account/relay clients and only issues
public GETs. Its persisted API URL is selected from supported mainnet/devnet1 V2
URLs. Live `.env`, keys, account IDs, `REYA_MODE`, and `REYA_API_URL` are ignored.
The live CLI still rejects `REYA_MODE=paper` and directs users to this command;
no flag converts a paper operation into live execution.

## Explained accounting fixture

Start with 1,000 virtual rUSD and an assumed 10 bps fee. Ask book: 1 ETH at 101,
2 ETH at 102; mark 100. Buy 2 ETH IOC with limit 101: fill 1, cancel 1, fee 0.100
(10 bps of 1 × mark 100, not of the 101 fill). Cash is 999.900, unrealized PnL −1,
equity 998.900. Repeat the request ID: unchanged. Close that 1 ETH at bid 110 with
mark 110: realized price PnL 9, closing fee 0.110, final cash 1,008.790 and no
position. No funding/ADL was simulated. Offline tests also cover flips, fractional
basis, stale data, rejected sizes, restart, failure rollback, concurrent access,
misconfigured live credentials and the mark-based fee base.

## Model scope and API dependencies

Paper keeps a local ledger and fills against the live public book. It models
depth-limited partial fills; it does not model funding, maker or TP/SL orders,
latency or market impact, and claims no fidelity beyond that.

Unsigned GETs only: `/v2/perpMarketDefinitions`, `/v2/perpMarkets/summary`,
`/v2/market/{symbol}/depth`. No new non-V2 dependency. Exact account fee tier and
full margin/ADL behavior are intentionally not inferred from missing APIs.

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
