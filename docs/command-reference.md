# Command reference

Field-level output and argument detail for `query`, `trade` and `health`.
[SKILL.md](../SKILL.md) holds the operating rules; read it first. Values below are
illustrative. Error envelopes and venue error codes are listed in
[error codes](error-codes.md).

## Agent setup (`agent.py`)

`agent init --owner 0x<wallet> [--amount N] [--account-id ID] [--wait] [--timeout S] [--interval S]` and `agent status [--wait]`.

Both return the same shape; `init` may also act (generate the agent key, record the owner and the agent account in `.env`), `status` never does. Fields: `step`, `next` (plain-language instruction for the user), `url` (deep link to open in the app; `null` until `REYA_APP_URL` is set on non-mainnet deployments), `owner`, `agent_address`, `approved`, `agent_account_id`, `agent_account_ids`, `balances` (per asset, exact strings), `from_account` (the main account for the funding link), `actions` (what this run changed: `owner_recorded`, `agent_key_generated`, `agent_account_recorded`), `signer_mode`. No field ever carries key material.

| `step` | Meaning | User action |
| -- | -- | -- |
| `needs_owner` | No `PERP_WALLET_ADDRESS_1` and no `--owner` | Re-run with `--owner` |
| `needs_app_onboarding` | The wallet has no Reya accounts | Connect the wallet in the app and accept the terms (the app creates the main account); re-run |
| `needs_approval` | Agent key exists but is not an enabled API wallet for the owner | Open `url`, compare `agent_address`, approve with one wallet signature; re-run |
| `needs_agent_account` | Approved; no recorded agent account | Open `url`, create the "Agent account" sub-account; re-run (the kit records the new one) |
| `ambiguous_new_accounts` | Several new sub-accounts appeared | Re-run with `--account-id` |
| `needs_funding` | Agent account recorded, balance zero | Open `url`, sign the transfer; `--amount` pre-fills it; re-run |
| `ready` | Approved, recorded, funded | None |

Approval status comes from the legacy `GET /api/trading/wallet/{owner}/apiWalletStatus` (addresses only), recorded as a non-V2 exception in AGENTS.md. Accounts and balances are V2 wallet reads. The kit never calls the permission contract itself and never drives the browser; `--wait` only polls.

## Read commands (`query.py`)

### Settled executions and fees

`query accounts` discovers owner accounts and their names/types without a private key or account ID. For creation/funding, use the separate `account` commands described below.

`query executions --symbol ETHRUSDPERP --order_id 123456` returns `account_id`, `executions` and `source_page`. Optional `--start_time` / `--end_time` are inclusive Unix milliseconds. The wallet endpoint returns **one capped page**, filtered locally to the configured account/exchange and optional order/symbol. `source_page` describes the original wallet page, before local filtering. It now labels `order: "newest_first"`, `newest_timestamp_ms`, `oldest_timestamp_ms` and `scope: "wallet_before_local_filters"`. Original API `startTime`/`endTime` are retained as first/last row timestamps, so `startTime > endTime` is expected (they are not the requested interval): an empty filtered result does not prove no historic fills exist. Narrow the time window when a page reaches its limit; overlapping windows need deduplication by scoped fill ID (or a nonzero sequence number) and role. A legacy sequence number of zero is not an identity: preserve observations, label uncertainty and do not sum possible overlaps. This command is not a complete-history exporter.

Each row retains `fill_id`, `sequence_number`, `order_id`, `symbol`, `role`, `qty`, `price`, `timestamp`, `type`, `fee_rusd`, `pnl_opening_fee_rusd` and `taker_rebate_credit_rusd`. Fee values are exact decimal strings from the API in rUSD, chosen for the configured account's maker/taker role. Missing maker fees stay null; negative reported fees are retained. `pnl_opening_fee_rusd` replaces the misleading `opening_fee_rusd` label without changing the API value. The API computes this PnL field on reductions/flips, not on position-extending fills; it can be null when prior state is missing. It is derived from that execution's fee and entry/execution prices, not a historical opening debit. Only `fee_rusd` is the reported fee for this fill; do not add the PnL field to it. Rebates stay separately reported, not silently subtracted. These fields are not total strategy PnL.

Create/close responses preserve `client_order_id`, `first_fill_id` and `fill_count`, and add `fees_status`, `fees_rusd` and `executions`. When filled quantity is known and nonzero, the kit defaults to one execution read with a two-second deadline. Market/limit/TP/SL creates accept `--fees_wait_s N` (integer 0–5): 1–5 polls execution reads at 0.5-second intervals within that total number of seconds; 0 keeps the single read. This never repeats the order submission and is also available inside the runner. Batch closes retain their single read per order after all close submissions. `fees_status` is `complete` only when the account/order/symbol, quantity and available fill IDs/count reconcile and all fees are known. Otherwise it is `pending`, `partial`, `unavailable`, or `not_filled`; `fees_rusd` remains null. A fee-read failure does not change the order outcome or retry the write. Poll `query executions` to reconcile later. Resting orders can fill after the create response, so later executions remain authoritative.

Order readback now uses `order_side: "buy"/"sell"` and removes the ambiguous long/short `side` alias. Open trigger orders additionally use `close_side`, `position_side` and `quantity_mode: "whole_position"`. A sell/short TP or SL protects a long; `qty: null` denotes whole-position sizing, not zero protection. Create responses use the same position/close-side explanation. `query orders` and runner `status.orders` use the same fields, including client IDs, `post_only` and `reduce_only`. Unknown flags remain null; null is not false. Consumers must migrate from `side` to `order_side`.

### Response shapes

The examples below show core fields; live responses also contain the correlation and fee fields described above.

**prices:**

```json
{
  "symbol": "BTCRUSDPERP",
  "oracle_price_usd": 77450.61,
  "updated_at": 1779270074736,
  "mark_price_usd": 77447.75
}
```

Oracle/mark prices and their timestamp may be `null` when unavailable. Do not treat missing prices as zero. The mark price is the execution reference for `order market`, `positions close_all` and `margin`; the oracle index prices collateral.

**market info:** Structural metadata an agent needs to size orders without hardcoding. Values are strings (preserve precision).

```json
{
  "symbol": "ETHRUSDPERP",
  "market_id": 1,
  "min_order_qty": "0.01",
  "qty_step_size": "0.001",
  "tick_size": "0.001",
  "max_leverage": 25,
  "initial_margin_parameter": "0.04000537463891571",
  "liquidation_margin_parameter": "0.030773365106858238",
  "oi_cap": "4500"
}
```

**market funding:** Funding rate plus current accrued funding values per side. Use this to assess the funding cost of holding a position.

```json
{
  "symbol": "ETHRUSDPERP",
  "funding_rate": "0.2916666666666667",
  "long_funding_value": "677522.0953345274",
  "short_funding_value": "24907.818391440487",
  "oi_qty": "346.69495521794544",
  "updated_at": 1779981485507
}
```

**positions:** Each position adds an estimated unrealized PnL at the current V2 mark:
`qty × (mark − avg_entry_price)`, signed by side. It is price PnL only — accrued funding
and fees are excluded — and it is an estimate, not a settlement value. If the mark cannot be
read the estimate fields are `null` and `mark_unavailable_reason` says why; the positions
themselves are still returned.

```json
[
  {
    "symbol": "ETHRUSDPERP",
    "side": "long",
    "qty": "0.046",
    "avg_entry_price": "2129.19",
    "mark_price": "2131.40",
    "mark_updated_at": 1779274209027,
    "estimated_unrealized_pnl_usd": "0.10166"
  }
]
```

**fees:** Public fee tiers and global discounts. With `PERP_WALLET_ADDRESS_1` set, `wallet`
also reports that wallet's tier and OG/affiliate/referee status (no key needed). Rates are
fractions; `*_bps` are the same rates in basis points. Use the taker rate as an input to
`paper init --fee-bps`. How discounts combine with the tier is not verified: settled
execution fees are authoritative.

```json
{
  "fee_tiers": [
    {"tier_id": 0, "tier_type": "REGULAR", "taker_fee": "0.0003", "maker_fee": "0",
     "taker_fee_bps": "3", "maker_fee_bps": "0", "volume30d": "0"}
  ],
  "discounts": {"og_discount": "...", "referee_discount": "...", "referrer_rebate": "...",
                "affiliate_referrer_rebate": "..."},
  "fee_base": "Taker fees are charged on quantity x the on-chain mark price at settlement, not on the fill price.",
  "wallet": {"address": "0x...", "fee_tier_id": 0, "taker_fee": "0.0003", "maker_fee": "0",
             "taker_fee_bps": "3", "maker_fee_bps": "0", "og_status": false,
             "affiliate_status": false, "referee_status": false, "note": "..."}
}
```

**orders:**

```json
[
  {
    "order_id": "123456789",
    "symbol": "ETHRUSDPERP",
    "order_side": "buy",
    "post_only": true,
    "reduce_only": false,
    "order_type": "LIMIT",
    "qty": "0.048",
    "limit_px": "2079.035",
    "trigger_px": null,
    "status": "OPEN",
    "created_at": 1779274209027
  }
]
```

**balances:**

```json
[
  {
    "account_id": 8009,
    "asset": "RUSD",
    "real_balance": "165.45686875879642708"
  }
]
```

**margin:** Collateral-only — unrealized PnL is NOT included, so this is not authoritative account equity or an execution admission check. Positions are valued at the mark price. Collateral uses asset-keyed oracle prices, including USDC; only RUSD is valued at one. Fails fast (`{"error": "..."}`, exit 1) if relevant asset metadata or positive mark/oracle prices are unavailable, or a position symbol is missing from market definitions.

```json
{
  "account_id": 8009,
  "collateral_value_usd": 538.41,
  "effective_collateral_usd": 501.11,
  "liquidation_margin_req_usd": 4.27,
  "margin_buffer_usd": 496.84,
  "collateral_breakdown": [
    {
      "asset": "SRUSD",
      "balance": "25.18452842667879919518",
      "price_usd": 14.808736,
      "haircut": 0.1,
      "value_usd": 372.95,
      "effective_value_usd": 335.66
    }
  ],
  "positions": [
    {
      "symbol": "ETHRUSDPERP",
      "side": "long",
      "qty": "0.046",
      "mark_price_usd": 2129.12,
      "notional_usd": 97.94,
      "liq_margin_param": 0.025,
      "liq_margin_req_usd": 2.45
    }
  ]
}
```

## Write commands (`trade.py`)

### Arguments

- `--post_only` — GTC limits only: rest as maker or cancel rather than immediately taking liquidity. Inspect status/cancellation reason.
- `--client_order_id` — Integer in `1..18446744073709551615` for market, limit, TP and SL creates. When omitted, the kit generates a random one before signing and returns it in the result, including error results, so every create can be looked up after a lost reply. Serialized by the SDK and echoed from venue responses. Retain it with the venue order ID; it does not make blind retries safe. Cancel and GTC limit modification can resolve either the venue `--order_id` or `--client_order_id` against this account's open orders.
- `--symbol X` / `--all` — Exactly one is required for `cancel_all` and `close_all`, even in previews. `--all` explicitly targets the configured account; `--symbol` can still affect another strategy on the same symbol.

- `<symbol>` — Trading symbol (e.g., `ETHRUSDPERP`, `BTCRUSDPERP`)
- `--side` — `long`, `short`, `buy`, or `sell`
- `--qty` — Quantity in base asset units (mutually exclusive with `--notional`)
- `--notional` — Order size in USD; qty is computed automatically (mutually exclusive with `--qty`)
- `--price` — Limit price in USD (for `order limit` and `order modify`). A price more than 5% through the mark (above it for a buy, below it for a sell) is refused unless `--force`; more than 50% is refused even with `--force`. See [fat-finger guards](#fat-finger-guards)
- `--slippage` — Max slippage as decimal, default `0.01` (1%). Must be in `(0, 0.5]`; values above `0.05` (5%) additionally require `--force` (for `order market` and `positions close_all`)
- `--reduce_only` — Only reduce existing position (for `order market` only)
- `--preview` — Send nothing. `order market` / `order limit`: return the computed order (qty, price bound, notional, distance from mark, account value, estimated leverage) with `would_send` and any `refused_by`. `order cancel_all` / `positions close_all`: list what would be cancelled/closed, and for `close_all` the TP/SL each close drops
- `--force` — Bypass safety guardrails: the 5% slippage cap (`order market`, `positions close_all`), the 5% limit-price band (`order limit`, `order modify`) and the TP/SL position-match and immediate-fire checks (`order tp`, `order sl`)
- `--allow_large` — Send an order above the per-order notional cap or the estimated-leverage limit (`order market`, `order limit`, `order modify`). Only after the user has confirmed that size and leverage
- `--order_id` — Order ID to cancel (for `order cancel` only)
- `--trigger_px` — Mark price at which the trigger fires, USD (for `order tp` / `order sl` only). Rounded to tick size.
- `--limit_px` — Required worst acceptable child execution price for TP/SL. It must be within the venue's permitted band around the trigger; there is no universal guaranteed-fill limit.
- `--time_in_force` — Required for TP/SL: `IOC` fills available liquidity then cancels the remainder; `GTC` may leave the remainder resting. The kit does not expose GTT expiry.

### Response shapes

The examples below show core fields; live responses also contain the correlation and fee fields described above.

**order market:**

```json
{
  "action": "order_market",
  "symbol": "ETHRUSDPERP",
  "side": "long",
  "qty": "0.046",
  "notional_usd": 97.94,
  "mark_price_usd": 2129.12,
  "limit_px_usd": "2150.41",
  "limit_vs_mark_pct": 1.0,
  "slippage": 0.01,
  "account_value_usd": 250.0,
  "current_leverage": 0.0,
  "est_leverage": 0.39,
  "max_leverage": 5.0,
  "max_order_notional_usd": 250.0,
  "max_order_notional_source": "account_value",
  "reduces_position": false,
  "order_id": "123456792",
  "status": "FILLED",
  "filled_qty": "0.046",
  "avg_fill_px": "2129.12",
  "cancel_reason": null,
  "cancel_reason_message": null
}
```

All create responses also carry `filled_qty` (API `cumQty`), `avg_fill_px`, `cancel_reason`, and `cancel_reason_message`, which can be null. A returned `order_id` is not proof of a fill: even a zero-fill IOC receives one. `order market` exits 1 with the response retained unless status is `FILLED`; partial/no-fill and `UNKNOWN` outcomes require account/order readback before another write. `order limit` and triggers accept `OPEN` or `FILLED`; cancelled/unknown outcomes exit 1.

`notional_usd` is the actual post-rounding requested order size (`qty × mark_price` for market orders, `qty × limit price` for limits), which may differ slightly from a requested `--notional`.

**order limit:**

```json
{
  "action": "order_limit",
  "symbol": "ETHRUSDPERP",
  "side": "long",
  "qty": "0.05",
  "limit_px_usd": "2000",
  "notional_usd": 100.0,
  "mark_price_usd": 2129.12,
  "limit_vs_mark_pct": -6.06,
  "account_value_usd": 250.0,
  "est_leverage": 0.43,
  "order_id": "123456790",
  "status": "OPEN"
}
```

`order limit` also returns `current_leverage`, `max_leverage`, `max_order_notional_usd`, `max_order_notional_source` and `reduces_position`, as `order market` does.

### Fat-finger guards

`order market`, `order limit` and `order modify` read the account before signing and refuse, with nothing sent, an order that fails one of these checks. The refusal exits 1 with every computed field, `refused_by` (each entry has `check`, `message` and the `override` flag, or `null` when none exists) and an `error` joining the messages. No client order ID is assigned to a refused order.

| `check` | Refused when | Override |
| -- | -- | -- |
| `limit_price_band` | A limit price is more than 5% through the mark (a buy above it, a sell below it). Passive prices are never refused. More than 50% has no override. | `--force` |
| `max_order_notional` | The order's notional is above the per-order cap: `REYA_MAX_ORDER_NOTIONAL` (USDC) if set in `.env`, else the account value. | `--allow_large` |
| `max_leverage` | Estimated leverage after a full fill is above `REYA_MAX_LEVERAGE` (default 5) and higher than before the order. | `--allow_large` |
| `account_value` | The account value is zero or negative. | none |

- **Account value** = collateral (USDC at 1, other assets at their oracle price, no haircut) + estimated unrealized PnL at mark. Funding and fees are not included.
- **Estimated leverage** = gross notional of all the account's positions at mark after a full fill ÷ account value. Resting orders are not counted. `current_leverage` is the same before the order; both are `null` when the account value is not positive.
- An order that only reduces the position (`--reduce_only`, or the opposite side for no more than the position) is never refused for size or leverage. A reduce-only market order whose account reads fail is still sent, with `risk_unavailable` explaining why the figures are missing; any other order whose reads fail is refused.
- `order modify` checks the new price against the band and only the unfilled remainder (new total qty minus filled qty) against the cap and leverage.
- Why 5%: it equals the market-order slippage cap, so a marketable limit cannot go further than `order market` can without `--force`, and the venue busts fills more than 5% from its mark on chain anyway. The mainnet TP/SL band (300 bp) applies only to trigger children.
- Inside the runner the same guards apply and the overrides are unavailable; the runner's policy is the operator's limit.
- `--preview` returns the same fields with `preview: true` and `would_send` (`false` when `refused_by` is non-empty), and exits 0 either way.

**order cancel:**

```json
{
  "action": "order_cancel",
  "order_id": "123456790",
  "status": "CANCELLED"
}
```

**order tp / order sl:** Protective orders attempt to close the **whole live position** on `PERP_ACCOUNT_ID_1` when the mark crosses `trigger_px`. `--side` names the position being protected; the order goes the other way. Open orders use `TAKE_PROFIT` / `STOP_LOSS`, carry the chosen `limit_px` / `time_in_force`, and expose `triggered` (`false` for an armed stop, `true` for a fired child, `null` when unavailable). `filled_qty` reports cumulative execution.

Firing does not guarantee a fill. An IOC child can leave exposure after a partial fill; a GTC child can rest unfilled. A created child cancels its paired protective stop even if it fills nothing, and protection is not automatically re-armed. Inspect position and order state after firing. Manually cancelling one leg leaves its sibling armed.

**Position-match guardrail:** the trigger must protect a real position — there must be an open position on the symbol for `PERP_ACCOUNT_ID_1`, and its side must match `--side`. A missing position (the trigger would protect nothing) or a side mismatch (the close direction and immediate-fire check would both be wrong) is rejected with an error unless `--force` is passed; when forced, the order is placed and the response carries a `warning` field.

**Immediate-fire guardrail:** a trigger on the wrong side of the current mark price (a TP at or below it for a long, an SL at or above it, mirrored for shorts) would fire the moment it lands and attempt to close at the selected limit. Such triggers are rejected with an error unless `--force` is passed; when forced, the order is placed and the response carries a `warning` field. To close a position intentionally, prefer `positions close_all --symbol X`.

```json
{
  "action": "order_tp",
  "symbol": "BTCRUSDPERP",
  "position_side": "long",
  "trigger_px_usd": "73273.116",
  "mark_price_usd": 71450.61,
  "limit_px_usd": "73200",
  "time_in_force": "IOC",
  "order_id": "123456791",
  "status": "OPEN"
}
```

`order_sl` returns the same shape with `"action": "order_sl"`.

**order cancel_all:** Cancels every open **perp** order on `PERP_ACCOUNT_ID_1`; spot orders are excluded. Returns each per-order outcome; individual failures don't stop the batch — they're collected under `errors`, with a top-level `error` and exit code 1.

```json
{
  "action": "order_cancel_all",
  "symbol_filter": null,
  "cancelled_count": 2,
  "cancelled": [
    {"order_id": "123456790", "symbol": "ETHRUSDPERP", "status": "CANCELLED"},
    {"order_id": "123456791",     "symbol": "BTCRUSDPERP", "status": "CANCELLED"}
  ]
}
```

With `--preview`, nothing is sent; the response lists the resting orders (including TP/SL triggers) that the real call would cancel:

```json
{
  "action": "order_cancel_all",
  "preview": true,
  "symbol_filter": null,
  "would_cancel_count": 1,
  "would_cancel": [
    {"order_id": "123456790", "symbol": "ETHRUSDPERP", "side": "long", "order_type": "LIMIT",
     "qty": "0.05", "limit_px": "2000", "trigger_px": null}
  ]
}
```

**positions close_all:** Flattens every open position on `PERP_ACCOUNT_ID_1` via reduce-only IOC orders. Sends an opposite-side IOC order per position with the configured slippage. Individual failures don't stop the batch.

Unlike the other write commands, `close_all` sends the venue's position quantity verbatim, including sub-step dust. Positions below `min_order_qty` are flagged with `below_min_order_qty: true`. The API currently rejects those closes (`PRICE_QTY_BOUNDS_ERROR`) and also rejects reduce-only orders larger than the position, so a rejected below-minimum close carries a `hint`: add at least the minimum, then close the whole position. It never automatically resubmits an ambiguous failure.

`close_orders` retains submission responses. `closed` / `closed_count` include only `FILLED` responses whose symbol is absent from the subsequent position readback (polled for up to two seconds after a filled response). `remaining_positions` lists residual exposure, or is `null` if readback failed. Remaining exposure, unconfirmed closure, or any batch error exits 1. Data can lag: query positions again before deciding on another close; never blindly retry entry orders.

```json
{
  "action": "positions_close_all",
  "symbol_filter": null,
  "slippage": 0.01,
  "remaining_positions": [],
  "close_orders": [{"symbol": "ETHRUSDPERP", "order_id": "123456793", "status": "FILLED", "filled_qty": "0.046"}],
  "closed_count": 1,
  "closed": [
    {
      "symbol": "ETHRUSDPERP",
      "position_side": "long",
      "qty": "0.046",
      "mark_price_usd": 2129.12,
      "limit_px_usd": "2107.83",
      "order_id": "123456793",
      "status": "FILLED"
    }
  ]
}
```

With `--cancel_orders`, open perp orders in the same scope (`--symbol` or `--all`) are cancelled one by one before any close is sent. Only a **confirmed** cancellation stops that order from reopening the position. The response adds `cancelled_count` and `cancelled`. A failed cancellation, or a failed open-order read, is reported under `errors` with the context `Cancel before close`, and flattening continues, so that order may still be live and can fill after the close. When `errors` is present, run `query orders` and `query positions` before treating the account as flat. If the position read fails after cancelling, nothing is closed and the result still lists the confirmed cancellations. A preview adds `would_cancel_count` and `would_cancel` instead. Spot orders are not included.

With `--preview`, nothing is sent; the response lists the close plan computed with the same slippage math the real call would use (prices are indicative — fills happen at execution-time prices):

```json
{
  "action": "positions_close_all",
  "preview": true,
  "symbol_filter": null,
  "slippage": 0.01,
  "would_close_count": 1,
  "would_close": [
    {
      "symbol": "ETHRUSDPERP",
      "position_side": "long",
      "qty": "0.046",
      "mark_price_usd": 2129.12,
      "limit_px_usd": "2107.83",
      "close_side": "sell",
      "est_notional_usd": 97.94,
      "drops_protection": [
        {"order_id": "123456794", "order_type": "STOP_LOSS", "trigger_px": "2050", "limit_px": "2030"},
        {"order_id": "123456795", "order_type": "TAKE_PROFIT", "trigger_px": "2300", "limit_px": "2280"}
      ]
    }
  ],
  "would_drop_protection_count": 2
}
```

`drops_protection` lists the account's take-profit and stop-loss on that market. They cover the whole position, so the venue cancels them once the position is flat (cancel reason `POSITION_CLOSED`); after a partial fill they stay armed for the rest. It is `null` when the open orders could not be read; the preview then reports the read failure under `errors` (context `TP/SL read`, after `Cancel before close` with `--cancel_orders`) and exits 1. With `--cancel_orders` the same orders are also in `would_cancel`, since the kit cancels them before closing. `would_drop_protection_count` totals the lists: `null` if any is unknown, `0` when nothing would close.

## Health check

```bash
python -m agent_kit.scripts.health
```

Returns:

```json
{
  "status": "ok",
  "api_url": "https://api.reya.xyz/v2",
  "sdk_version": "3.6.3.0",
  "markets_available": 5,
  "latency_ms": 209
}
```

`markets_available` is the number of perp market definitions; values shown are illustrative.

On failure, exits 1 with the standard error envelope plus diagnostic context:

```json
{
  "error": "ClientConnectorError: ...",
  "status": "error",
  "api_url": "https://api.reya.xyz/v2",
  "sdk_version": "3.6.3.0"
}
```
