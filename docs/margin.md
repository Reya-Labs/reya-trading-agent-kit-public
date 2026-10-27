# Margin, liquidation and sizing by risk

The matching engine decides whether an order is admitted, and the chain (Core) liquidates an account below its liquidation margin. The kit rebuilds both from public V2 data so that an assistant can see, before it trades, how much room the account has, where each position liquidates and how large an order can be. The admission logic is a port of the engine's risk engine (source revision `4d8ed012`) and is tested with vectors from the engine's own tests. Equity, margin usage and liquidation follow Core's definitions, and their vectors are derived from Core (`agent_kit/tests/test_margin.py`).

These are kit estimates. The engine's verdict is final, and some of its inputs are not published on V2 (see [What the kit cannot see](#what-the-kit-cannot-see)).

## How the engine admits an order

All amounts are rUSD (shown as USDC in the app). Cash is rounded down and requirements are rounded up, to rUSD's 6 decimals.

| Figure | Formula |
| -- | -- |
| Real balance (RB) | Σ collateral × price × (1 − haircut) (a debt × price / (1 − haircut)), plus realized PnL, funding and ADL cashflows. rUSD counts at 1. For equity and liquidation (Core), every collateral counts, withdrawal-only included. For the pre-trade check only, the engine gives no credit to USDC, deUSD, sdeUSD, the LM receipts (rSelini, rAmber, rHedge, rKeyrock, rFlow), REYA and sREYA. |
| Equity, margin balance (MB) | RB + Σ position × (mark − average entry) |
| Liquidation margin (LM) | Σ s × \|position\| × mark, where s is the market's `liquidation_margin_parameter` |
| Initial margin (IM) | IM multiplier × LM. The multiplier is 1.3 on every mainnet market (`initial_margin_parameter` = 1.3 × s). |
| Settlement reserve | 1% × Σ \|qty\| × price over the order (at its limit price) and the account's matched-but-unsettled fills. It is charged on each order, not on the position. |

The engine projects the order as fully filled at its limit price:

- **MB_floor** = MB + min(0, qty × (mark − limit)), less one native unit (0.000001) per position and per fill for Core's rounding. A market buy with 1% slippage is charged that full 1% up front.
- **RB_floor** = the same, plus the loss at mark on any part of the position the order closes.
- **Health h** = min(MB_floor − LM, RB_floor).

The verdict, first failure wins:

1. **Before the order:** h ≥ 0, or *"This account is X rUSD below its liquidation safety threshold. Add collateral before placing another order."* (`ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR`). This applies to every order, closes included.
2. **Before and after:** MB_floor ≥ IM, or *"Insufficient margin: the account is X rUSD below the initial margin requirement for this order…"*.
3. **After:** h ≥ reserve, or *"Insufficient margin: this order would leave the account X rUSD below its settlement reserve…"*.

Steps 2 and 3 both return `ACCOUNT_BELOW_INITIAL_MARGIN_ERROR`; the message tells them apart.

- An order with the reduce-only flag, and every TP/SL, owes step 1 only. An opposing order *without* the flag is ordinary, owes all three steps, even if it only reduces.
- A resting limit order is checked at placement only if it crosses the book. Otherwise each fill is checked at its own price (steps 2 and 3); a fill that fails cancels the order.
- The chain liquidates when MB < LM.

### Positions in different markets add up

Long and short net only within one market: a market's requirement is on the account's net position there. Across markets the requirements are summed (the Σ over markets above), so a position in one market never lowers another market's requirement. A short in one market held against a long in another on the same account is extra exposure for margin: it adds its own liquidation margin, even when the two are correlated and the pair swings less in P&L. The preview's `margin` block shows the effect before the order (`margin_usage_after`, `liquidation_price_after_usd`); compare them with `query margin` now.

## What this means on mainnet

IM is only 1.3 × LM, so on the 25x and 40x markets the 1% reserve, not initial margin, caps a single order. Largest single order from a flat account, as a multiple of equity:

| Market | Published `max_leverage` | Limit at the mark | Market order, default 1% slippage |
| -- | -- | -- | -- |
| BTC | 40 | 34.2x | 25.4x |
| SPY, ETH (25x markets) | 25 | 24.5x | 19.7x |
| 15x markets | 15 | 15.0x (IM binds) | 13.0x |

- Never size an order as equity × `max_leverage`. V2 publishes `max_leverage = round(1 / IM)`, which can be above the real limit: SPY shows 25 while 1 / IM is 24.997.
- The reserve is per order, so a position can approach the IM limit over two orders. Tighter slippage on a market order allows a larger size.

## Markets whose underlying closes

Equity, index and commodity perps (`categories` other than `CRYPTO` in `query market info`) follow an underlying market with trading hours. Reya trades them around the clock on their mark while the underlying is closed, at weekends, on holidays and, for equities and equity indices, overnight. The mark can stay still and then move in one step when the underlying reopens, and their books are thinner than the large crypto markets. A stop-loss fires on the mark, so it closes at the first mark after the gap, possibly well past its trigger, or not at all if the book cannot fill its limit: it is no guard through a gap, and a liquidation can follow the same step. Scheduled high-impact releases (inflation figures, jobs reports, rate decisions) can move any market in one step the same way. The kit marks these markets with `market_hours_warning` on orders and positions; it has no release calendar.

## What the kit computes from V2

| Input | Source | Treatment |
| -- | -- | -- |
| Collateral | `accountBalances` (net deposits + settled PnL, funding and ADL included), `assetDefinitions`, `assetOraclePrices` | As Core: a holding at price × (1 − haircut), a debt at price / (1 − haircut), whatever the asset's V2 `status` (`WITHDRAWAL_ONLY` is a deposit policy). The pre-trade check withholds the engine's zero-credit list (above), which is the engine's own list (its collateral table, `collateral_sources.rs`), not V2's status: the engine credits USDe and sUSDe. |
| Positions | `positions` (ADL-adjusted qty, average entry) and `perpMarkets/summary` marks | Exact formula. |
| Funding | Already in the rUSD real balance | Not added again (below). |
| LM rate, IM multiplier | `perpMarketDefinitions` | s as published; multiplier = IM / LM rate. |
| Reserve rate | Not on V2 | Pinned at 1%, the engine's V1 policy constant. |

**Funding.** V2's rUSD `realBalance` is net deposits plus each position's settled PnL, which includes funding and ADL cashflows up to the latest market trackers (the API computes it as Core's `updatePerpPosition` does). Core's real balance and the engine's include the same cashflows, so the kit adds nothing: equity is the real balance plus PnL at mark. Before 0.12.27 the kit also subtracted −(market funding value − average entry funding value) × position, counting paid funding twice (deposits 10,000, a long of 1 BTC at 100,000 that paid 50: real balance 9,950, Core's equity 9,950, the kit's 9,900). Funding accrued since the API last read the trackers is not visible; it is seconds of funding.

## What the kit cannot see

- **Matched fills that have not settled yet.** These last seconds, up to the engine's 60-second indexing grace. The engine adds their notional to the reserve, their worst case to the exposure, and their adverse price to equity. The kit does not see them. The gap is zero once the account has had no fill for about a minute; wait for the previous fill to show in `query positions` before sizing the next order.
- **Mark moves** between the kit's read and the engine's check.
- **ADL base multipliers.** These are not on V2; the kit assumes none, which holds unless the market has had an ADL. ADL cashflows already settled are in the real balance.
- **Collateral price skew.** The engine values collateral from its live price feed, the kit from V2's oracle prices.

With no unsettled fills, the only gap is price movement in that window. A kit refusal is therefore reliable. When the kit says an order fits, it can be wrong only if a fill is in flight or the mark moved more than the headroom; the engine then rejects the order, and nothing fills.

## Commands

### `query margin`

| Field | Meaning |
| -- | -- |
| `equity_usd` | Margin balance as Core counts it: collateral after haircuts + unrealized PnL at mark |
| `real_balance_usd`, `unrealized_pnl_usd`, `unsettled_funding_estimate_usd` | Its parts. The funding estimate is 0: the real balance already includes funding. |
| `liquidation_margin_req_usd` | LM; the chain liquidates when equity falls below it |
| `initial_margin_req_usd` | IM; new exposure needs equity at or above it |
| `margin_usage`, `margin_usage_pct` | LM / equity; 1 (100%) is liquidation. `null` when equity is not positive. |
| `liquidatable` | equity < LM |
| `margin_buffer_usd` | equity − LM (before 0.8.0 this was collateral − LM, without PnL) |
| `initial_margin_buffer_usd` | equity − IM |
| `health_usd` | The engine's h with no order, with the engine's collateral credit: below 0, every order is rejected, closes included |
| `im_multiplier`, `settlement_reserve_rate` | The parameters used |
| `positions[]` | Adds `avg_entry_price`, `unrealized_pnl_usd`, `unsettled_funding_estimate_usd`, `liquidation_price_usd`, `distance_to_liquidation_pct` |
| `collateral_breakdown[]` | Each collateral's `value_usd` and `effective_value_usd` (as Core counts it); `engine_credit_usd` (0) and a `credit_note` for one the engine's pre-trade check does not credit |
| `assumptions` | What the figures leave out |

**Liquidation price** is cross margin: the price in that market at which equity equals the liquidation margin, with every other mark, collateral price and funding unchanged. It is rounded onto the tick toward the mark. It is `null` for a long that no positive price liquidates. `distance_to_liquidation_pct` is how far the mark must move against the position, rounded down; it is negative when the account is already past the line.

### `query positions`

Each position adds `liquidation_price_usd`, `distance_to_liquidation_pct` and `account_margin_usage`. If the margin read fails, positions are still returned, with these fields `null` and `margin_unavailable_reason` set.

### `--preview` and live orders: the `margin` block

`order market`, `order limit` and `order modify` return a `margin` block computed for a full fill at the order's limit price (the slippage bound for market orders):

- `engine_check`: `ok`; `tight` (admitted, but with less spare than the headroom); or the engine's reason, one of `below_liquidation_threshold`, `below_initial_margin` or `below_settlement_reserve`. The reason comes with `engine_error_code`, `engine_message` (the engine's own text) and `shortfall_usd`.
- **Before and after the fill:** `equity_before_usd` and `equity_after_usd` (after the fill at the limit price, less the estimated taker fee); `margin_usage_before` and `margin_usage_after`.
- **The engine's projection:** `liquidation_margin_after_usd`, `initial_margin_after_usd`, `settlement_reserve_usd`, `health_after_usd`, `reserve_headroom_usd` (h − reserve).
- **Liquidation after the fill:** `liquidation_price_after_usd` and `distance_to_liquidation_after_pct` for this market.
- `after_fill_basis`: the basis of every `*_after` figure, a full fill at the limit price. For an order that opens or adds to a position this is the worst case: a fill at a better price or a partial fill leaves margin usage lower and the liquidation price further from the mark. A market order's limit is its slippage bound, so the gap is largest there. On devnet, a BTC market order sized with `--max_size` at the default 1% slippage previewed `margin_usage_after` 0.61 and left 0.50 after the fill. Report these figures as "up to" (margin usage up to 61%), and read `query margin` after the fill for the actual ones. This does not hold for an order that reduces a position: a partial close leaves more of the position open, and can leave more usage than the preview.
- `max_qty`: the largest size the engine would admit at this price, less the headroom. Only for an order that opens or adds to a position.
- **Assumptions used:** `headroom_rate`, and `taker_fee_rate_assumed` (the highest tier rate; `null` if fee tiers could not be read).
- **Notes, when they apply:**
  - `engine_checks_at` (limit orders): when the engine checks it.
  - `note`: a reduce-only order that step 1 would reject is still sent.
  - `reduce_only_hint`: an order that only reduces the position but lacks `--reduce_only`.

A predicted reject of an ordinary order is refused before anything is sent, with `refused_by` check `engine_margin` and the engine's message. The verdict uses the engine's collateral credit; the `*_before`/`*_after` equity, usage and liquidation figures use Core's. `--skip_margin_check` sends it anyway; use it only when the kit is stricter than the engine (for example, a collateral the engine credits that this kit version does not) and the user confirms. It is unavailable inside the runner. Reduce-only orders, `close_all` and TP/SL are never refused for margin. If a sent order fails (the venue rejects it, or the reply is lost), the error result keeps the computed figures and the `margin` block next to the venue's `error`, `error_code` and `message`, so the kit's `engine_check` and `shortfall_usd` can be read against the engine's reply. Exit codes and `client_order_id` are unchanged.

### Sizing: `--max_size` and `--usage`

`order market` and `order limit` accept one of `--qty`, `--notional`, `--max_size` or `--usage U`:

- `--max_size`: the largest size the engine would admit at the order's limit price, leaving the headroom spare.
- `--usage U` (0 < U < 1): the largest size that leaves margin usage at or below U after a full fill at the limit price, including the estimated fee. Never more than `--max_size`.

Both stay within any `REYA_MAX_LEVERAGE` and `REYA_MAX_ORDER_NOTIONAL` the user set in `.env`, unless `--allow_large` is given. The kit sets neither by default, so they size up to what the engine admits. The result carries `sizing`, with `sized_by`, `target_usage`, `size_limited_by` (`engine_margin`, `usage`, `max_order_notional` or `max_leverage`) and `qty_by_limit`. Both options size an order that opens or adds to a position, so they refuse `--reduce_only` and the side that reduces.

`REYA_MARGIN_HEADROOM` (`.env`, default `0.002`) is the spare amount sizing leaves, as a fraction of the market's position notional after the fill. It covers the mark moving between the kit's read and the engine's check. `0` sizes to the engine's exact limit.

## Paper trading

Paper admits orders with this model, applied to its virtual rUSD ledger: the same three checks, messages and error codes, the `margin` block on every order result, and `--max_size` / `--usage` sizing (default headroom; `REYA_MARGIN_HEADROOM` is ignored). `paper state` and `paper refresh` add a `margin` block (`equity_usd`, `liquidation_margin_req_usd`, `initial_margin_req_usd`, `margin_usage`, `liquidatable`, `margin_buffer_usd`, `health_usd` and the rest) and each position's `liquidation_price` and `distance_to_liquidation_pct`. Paper's cash does not include funding, so paper adds it exactly from V2's funding values, and it liquidates the whole virtual account when equity falls below the liquidation margin. [paper-trading.md](paper-trading.md#how-faithful-it-is) lists where it can differ from live.
