# Command reference

Field-level output and argument detail for `query`, `trade` and `health`; the `strategy` commands have [their own page](hosted-workflows.md).
[SKILL.md](../SKILL.md) holds the operating rules; read it first. Values below are
illustrative. Error envelopes and venue error codes are listed in
[error codes](error-codes.md).

## Agent setup (`agent.py`)

`agent init --owner 0x<wallet> [--amount N] [--account-id ID] [--hosted-runs now|later] [--wait | --until-ready] [--timeout S] [--interval S]` and `agent status [--wait]`.

Both return the same shape; `init` may also act (generate the agent key, record the owner and the agent account in `.env`), `status` never does. Fields: `step`, `next` (plain-language instruction for the user), `url` (deep link to open in the app; `null` until `REYA_APP_URL` is set on non-mainnet deployments; once an agent account is recorded, the setup page link names it as `&account=<id>` and the page uses that account), `owner`, `agent_address`, `approved`, `agent_account_id`, `agent_account_ids`, `balances` (per asset, exact strings), `from_account` (the main account for the funding link), `actions` (what this run changed: `owner_recorded`, `agent_key_generated`, `agent_account_recorded`, `hosted_runs_now`, `hosted_runs_later`), `signer_mode`, `progress` (`{step, of}` on the setup page: 3 steps, or 4 for a run with `--hosted-runs now` while the hosted signer is still to approve), `hosted_runs` (at `ready`, below). No field ever carries key material.

Opening the link: `opened` is `true` when this run opened `url` in the user's browser (on WSL, the Windows browser). `link_already_opened: true` means an earlier run opened it, or gave it to the user, within the last 30 minutes, so it is not opened again. `browser_unavailable` says why nothing opened here: `ssh` (an SSH session; a `BROWSER` opener set by e.g. a remote editor is used instead), `no_display` (Linux without `DISPLAY` or `WAYLAND_DISPLAY`), `wsl_no_windows_browser` (WSL that cannot start Windows programs) or `no_browser` (the browser did not start); `next` then starts by asking the user to open `url` on any device where their wallet is, and the link is printed on stderr. With `--wait` or `--until-ready` and output read by a program rather than a terminal, that run returns at once with `waited: false` instead of waiting on a page nobody has seen; the next run waits. These fields stay in the result after a wait.

| `step` | Meaning | User action |
| -- | -- | -- |
| `needs_owner` | No `PERP_WALLET_ADDRESS_1` and no `--owner` | Re-run with `--owner` |
| `needs_app_onboarding` | The wallet has no Reya accounts | Connect the wallet in the app and accept the terms (the app creates the main account); re-run |
| `needs_approval` | Agent key exists but is not an enabled API wallet for the owner | Open `url`, compare `agent_address`, approve with one wallet signature; re-run |
| `needs_agent_account` | Approved; no recorded agent account | Open `url`, create the Agent account; re-run (the kit records it). From `status` with `found_account_id` and no `url`, the Agent account already exists: run `agent init` to record it |
| `ambiguous_new_accounts` | Several Agent accounts | Re-run with `--account-id` |
| `needs_funding` | Agent account recorded, balance zero | Open `url`, sign the transfer; `--amount` pre-fills it; re-run |
| `needs_hosted_runs` | Ready to trade; this run passed `--hosted-runs now` and the owner has not approved the hosted signer yet | On the same setup page (its Enable hosted runs step; a tab already open is not opened again), compare `hosted_signer` and approve with one wallet signature; relay `permission`. `--hosted-runs later` skips it |
| `ready` | Approved, recorded, funded | None |

**Enable hosted runs** is optional: setup is `ready` without it, and the kit trades either way. At `ready`, `hosted_runs.status` is `unavailable` (no strategy service for this deployment, or it could not be read within 5 seconds: nothing is offered), `enabled` (the owner approved the service's active hosted signer, `hosted_signer`) or `not_enabled`. With `not_enabled` and no answer yet, `hosted_runs.offer` carries `question`, `permission` (the approval covers every account the wallet owns; the service only trades the Agent account), `now_command` and `later_command`: ask once. `--hosted-runs later` is recorded in the state directory (`choice: "later"`) so the offer is not repeated; enabling it later (`strategy info --open`) shows `enabled` whatever the answer. `--hosted-runs now` lasts for that run only: with `--until-ready` the wait goes on through the Enable hosted runs step, and a user who chose it but never signed is offered it again on the next run. The hosted signer comes from the strategy service's `/v1/info`, read once per run.

The Agent account is the account the V2 wallet read reports with type `AGENTPERP`, whatever its name. The kit finds it by type alone: a sub-account of another type is never taken for it, even one named "Agent account" (earlier kits took that one when the wallet had no `AGENTPERP` account). `--account-id` still records any sub-account the user chooses when none is recorded yet. Once one is recorded, `init --account-id <id>` switches the kit to another of the wallet's `AGENTPERP` accounts: it rewrites `PERP_AGENT_ACCOUNT_IDS` and `PERP_ACCOUNT_ID_1` itself, keeps the agent key, and reports `previous_agent_account_id` and `actions: ["agent_account_switched", ...]`. The previous account keeps its positions, orders and any running strategy (manage them in the app). Any other account is refused and nothing changes. When the recorded account is not an `AGENTPERP` account, `init` and `status` add `agent_account_warning` and put it at the start of `next`: the account still trades, but hosted strategies refuse it, and `hosted_runs.status` is `not_agent_account`. `agent_account_fix` says what to do: `command` (`agent init --account-id <id>`, when the wallet has a single Agent account), `candidates` (a wallet may have several: ask the user which), or `url` (none: the setup page creates one; then `then: "agent status"` names the command). For margin and transfers an `AGENTPERP` account is a sub-account like any other; an account type the kit does not know still counts as owned, but the kit takes no action on it; if the recorded Agent account has such a type, `init` and `status` stop and ask for a kit update rather than record another account.

Approval status comes from the legacy `GET /api/trading/wallet/{owner}/apiWalletStatus` (addresses only), recorded as a non-V2 exception in AGENTS.md. Accounts and balances are V2 wallet reads. The kit never calls the permission contract itself and never drives the browser; `--wait` only polls.

## Read commands (`query.py`)

### Settled executions and fees

`query accounts` discovers owner accounts and their names/types without a private key or account ID. For creation/funding, use the separate `account` commands described below.

`query executions --symbol ETHRUSDPERP --order_id 123456` returns `account_id`, `executions` and `source_page`. Optional `--start_time` / `--end_time` are inclusive Unix milliseconds. The wallet endpoint returns **one capped page**, filtered locally to the configured account/exchange and optional order/symbol. `source_page` describes the original wallet page, before local filtering. It now labels `order: "newest_first"`, `newest_timestamp_ms`, `oldest_timestamp_ms` and `scope: "wallet_before_local_filters"`. Original API `startTime`/`endTime` are retained as first/last row timestamps, so `startTime > endTime` is expected (they are not the requested interval): an empty filtered result does not prove no historic fills exist. Narrow the time window when a page reaches its limit; overlapping windows need deduplication by scoped fill ID (or a nonzero sequence number) and role. A legacy sequence number of zero is not an identity: preserve observations, label uncertainty and do not sum possible overlaps. This command is not a complete-history exporter.

Each row retains `fill_id`, `sequence_number`, `order_id`, `symbol`, `role`, `qty`, `price`, `timestamp`, `type`, `fee_rusd`, `pnl_opening_fee_rusd`, `taker_rebate_credit_rusd` and `net_fee_rusd`. Fee values are exact decimal strings from the API in rUSD, chosen for the configured account's maker/taker role. Missing maker fees stay null; negative reported fees are retained. `pnl_opening_fee_rusd` replaces the misleading `opening_fee_rusd` label without changing the API value. The API computes this PnL field on reductions/flips, not on position-extending fills; it can be null when prior state is missing. It is derived from that execution's fee and entry/execution prices, not a historical opening debit. Only `fee_rusd` is the reported fee for this fill; do not add the PnL field to it. Rebates stay separately reported, not silently subtracted from `fee_rusd`: `net_fee_rusd` is their difference for a taker (the rebate comes back to the taker's account in the same settlement), the fee itself for a maker (already net), and `null` when a part is missing, for example a taker fill from before the fee breakdown. These fields are not total strategy PnL.

Create/close responses preserve `client_order_id`, `first_fill_id` and `fill_count`, and add `fees_status`, `fees_rusd` and `executions`. When filled quantity is known and nonzero, the kit defaults to one execution read with a two-second deadline. Market/limit/TP/SL creates accept `--fees_wait_s N` (integer 0–5): 1–5 polls execution reads at 0.5-second intervals within that total number of seconds; 0 keeps the single read. This never repeats the order submission and is also available inside the runner. Inside the runner the default read runs after the result is out, once the fill is indexed (up to 30 seconds): the result has `fees_status: "deferred"` and a `fees` event line follows (see [latency and warm state](account-setup-and-runner.md#latency-and-warm-state)). Batch closes retain their single read per order after all close submissions. `fees_status` is `complete` only when the account/order/symbol, quantity and available fill IDs/count reconcile and all fees are known. Otherwise it is `pending`, `partial`, `unavailable`, or `not_filled`; `fees_rusd` remains null. A fee-read failure does not change the order outcome or retry the write. Poll `query executions` to reconcile later. Resting orders can fill after the create response, so later executions remain authoritative.

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

**market info:** Structural metadata an agent needs to size orders without hardcoding. Values are strings (preserve precision). `categories` is the market's V2 categories (`CRYPTO`, `EQUITIES`, `INDICES`, `COMMODITIES`) as the API reports them, `null` when it does not; the kit never infers them. A market whose categories are reported and none is `CRYPTO` follows an underlying with trading hours: see `market_hours_warning` below.

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
  "oi_cap": "4500",
  "categories": ["CRYPTO"]
}
```

**market funding:** Funding rate plus current accrued funding values per side. Use this to assess the funding cost of holding a position. `funding_rate` is exact, in % per hour; `funding_text` is the one to quote, rounded as the Reya app shows funding (four decimals, a minus sign only) with its annualised rate.

```json
{
  "symbol": "ETHRUSDPERP",
  "funding_rate": "0.2916666666666667",
  "funding_text": "0.2917%/h (2555.00% a year)",
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
themselves are still returned. Each position also carries `liquidation_price_usd`,
`distance_to_liquidation_pct` and the account's `account_margin_usage` ([margin model](margin.md));
they are `null` with `margin_unavailable_reason` when the margin read fails.

<a id="market-hours-warning"></a>**`market_hours_warning`** (on `positions` and `margin` positions, and on `order market` / `order limit` previews and results; absent otherwise) marks a market whose underlying has trading hours: Reya trades it around the clock on its mark while the underlying is closed, the mark can pause and then jump at the reopen, and a stop-loss fires on the mark, so it is no guard through that gap. A reduce-only order carries none. See [margin: markets whose underlying closes](margin.md#markets-whose-underlying-closes).

```json
[
  {
    "symbol": "ETHRUSDPERP",
    "side": "long",
    "qty": "0.046",
    "avg_entry_price": "2129.19",
    "mark_price": "2131.40",
    "mark_updated_at": 1779274209027,
    "estimated_unrealized_pnl_usd": "0.10166",
    "liquidation_price_usd": "1702.31",
    "distance_to_liquidation_pct": 20.13,
    "account_margin_usage": 0.113
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

**margin:** Equity, margin requirements, margin usage and liquidation prices as the matching engine computes them, rebuilt from V2 data; [docs/margin.md](margin.md) has the model, every field and what the figures leave out. Equity is collateral after haircuts as Core counts it (every collateral at price × (1 − haircut), withdrawal-only included; a debt at price / (1 − haircut)) plus unrealized PnL at mark. V2's real balance already includes funding, so `unsettled_funding_estimate_usd` is 0. The pre-trade `engine_check` follows the matching engine, which gives no credit to some collaterals. `margin_usage` is liquidation margin / equity: 1 is the liquidation line. `margin_buffer_usd` is equity − liquidation margin (before 0.8.0 it left out PnL). Fails fast (`{"error": "..."}`, exit 1) if relevant asset metadata or positive mark/oracle prices are unavailable, or a position symbol is missing from market definitions.

```json
{
  "account_id": 8009,
  "equity_usd": 503.27,
  "real_balance_usd": 501.11,
  "unrealized_pnl_usd": 2.16,
  "unsettled_funding_estimate_usd": 0.0,
  "liquidation_margin_req_usd": 2.45,
  "initial_margin_req_usd": 3.185,
  "margin_usage": 0.0049,
  "margin_usage_pct": 0.49,
  "liquidatable": false,
  "margin_buffer_usd": 500.82,
  "initial_margin_buffer_usd": 500.08,
  "health_usd": 500.82,
  "im_multiplier": 1.3,
  "settlement_reserve_rate": 0.01,
  "collateral_value_usd": 538.41,
  "effective_collateral_usd": 501.11,
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
      "avg_entry_price": "2082.18",
      "unrealized_pnl_usd": 2.159,
      "unsettled_funding_estimate_usd": 0.0,
      "liq_margin_param": 0.025,
      "liq_margin_req_usd": 2.45,
      "liquidation_price_usd": null,
      "distance_to_liquidation_pct": null
    }
  ],
  "assumptions": ["..."]
}
```

## Write commands (`trade.py`)

### Arguments

- `--post_only` — GTC limits only: rest as maker or cancel rather than immediately taking liquidity. Inspect status/cancellation reason.
- `--client_order_id` — Integer in `1..18446744073709551615` for market, limit, TP and SL creates. When omitted, the kit generates a random one before signing and returns it in the result, including error results, so every create can be looked up after a lost reply. Serialized by the SDK and echoed from venue responses. Retain it with the venue order ID; it does not make blind retries safe. Cancel and GTC limit modification can resolve either the venue `--order_id` or `--client_order_id` against this account's open orders.
- `--symbol X` / `--all` — Exactly one is required for `cancel_all` and `close_all`, even in previews. `--all` explicitly targets the configured account; `--symbol` can still affect another strategy on the same symbol.

- `<symbol>` — Trading symbol (e.g., `ETHRUSDPERP`, `BTCRUSDPERP`)
- `--side` — `long`, `short`, `buy`, or `sell`
- `--qty` — Quantity in base asset units. Exactly one of `--qty`, `--notional`, `--max_size` or `--usage` (for `order market` and `order limit`)
- `--notional` — Order size in USD; qty is computed automatically
- `--max_size` — Size to the largest qty the matching engine would admit at the order's limit price, leaving `REYA_MARGIN_HEADROOM` (default 0.2% of the position notional) spare. It stays within `REYA_MAX_ORDER_NOTIONAL` and `REYA_MAX_LEVERAGE` unless `--allow_large`. Only for an order that opens or adds to a position. See [sizing](margin.md#sizing---max_size-and---usage)
- `--usage U` — Size so margin usage (liquidation margin / equity) is at most `U` (0 < U < 1) after a full fill at the limit price; never above `--max_size`
- `--skip_margin_check` — Send an order the kit predicts the engine would reject for margin (`order market`, `order limit`, `order modify`). Only when the kit is stricter than the engine and the user confirms; unavailable in the runner
- `--price` — Limit price in USD (for `order limit` and `order modify`). A price more than 5% through the mark (above it for a buy, below it for a sell) is refused unless `--force`; more than 50% is refused even with `--force`. See [fat-finger guards](#fat-finger-guards)
- `--slippage` — Max slippage as decimal, default `0.01` (1%). Must be in `(0, 0.5]`; values above `0.05` (5%) additionally require `--force` (for `order market` and `positions close_all`)
- `--reduce_only` — Only reduce existing position (for `order market` only; not with `--sl`/`--tp`)
- `--sl` / `--tp` — Arm a stop-loss / take-profit once the `order market` entry fills: a percent of the entry's average fill price (`0.35%`, below 50%) or a price (`5790`). Either can be used alone. See [entry protection](#entry-protection). `order limit` refuses them
- `--trigger_slippage` — Child limit distance from each `--sl`/`--tp` trigger, as a decimal (default `0.01` = 1%, at most `0.03`, the venue's 300 bps TP/SL band: see [TP/SL band](#tp-sl-band)). Wide enough that a fired child fills: a fired child cancels its sibling even when it fills nothing
- `--preview` — Send nothing. `order market` / `order limit`: return the computed order (qty, price bound, notional, distance from mark, account value, estimated leverage, and the `margin` block: the engine's verdict, margin usage and liquidation price after the fill, `max_qty`) with `would_send` and any `refused_by`. `order market` also reads the order book once and returns its [`book` block](#order-book-context), with a `warning` when the book holds less than the order inside its limit. An `order market` entry without `--sl`/`--tp` also returns [`protection`](#entries-without-a-stop): whether a stop-loss on Reya covers the position. `order cancel_all` / `positions close_all`: list what would be cancelled/closed, and for `close_all` the TP/SL each close drops and each close's [`book` block](#order-book-context) with its `warning`
- `--force` — Bypass safety guardrails: the 5% slippage cap (`order market`, `positions close_all`), the 5% limit-price band (`order limit`, `order modify`) and the TP/SL position-match, immediate-fire and [band](#tp-sl-band) checks (`order tp`, `order sl`). It never relaxes the `--sl`/`--tp` checks on `order market`
- `--allow_large` — Send an order above the per-order notional cap or the estimated-leverage limit (`order market`, `order limit`, `order modify`). Only after the user has confirmed that size and leverage
- `--order_id` — Order ID to cancel (for `order cancel` only)
- `--trigger_px` — Mark price at which the trigger fires, USD (for `order tp` / `order sl` only). Rounded to tick size.
- `--limit_px` — Required worst acceptable child execution price for TP/SL. It must be within the venue's [3% band](#tp-sl-band) around the trigger, or the kit refuses it before sending; there is no universal guaranteed-fill limit.
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
  "margin": {
    "engine_check": "ok",
    "engine_error_code": null,
    "engine_message": null,
    "shortfall_usd": null,
    "equity_before_usd": 250.0,
    "equity_after_usd": 248.98,
    "margin_usage_before": 0.0,
    "margin_usage_after": 0.0099,
    "liquidation_margin_after_usd": 2.448,
    "initial_margin_after_usd": 3.183,
    "settlement_reserve_usd": 0.989,
    "health_after_usd": 246.57,
    "reserve_headroom_usd": 245.58,
    "liquidation_price_after_usd": null,
    "distance_to_liquidation_after_pct": null,
    "after_fill_basis": "full fill at the limit price: the worst case for an order that opens or adds to a position. ...",
    "max_qty": "2.493",
    "headroom_rate": 0.002,
    "taker_fee_rate_assumed": 0.0004
  },
  "order_id": "123456792",
  "status": "FILLED",
  "filled_qty": "0.046",
  "avg_fill_px": "2129.12",
  "cancel_reason": null,
  "cancel_reason_message": null
}
```

All create responses also carry `filled_qty` (API `cumQty`), `avg_fill_px`, `cancel_reason`, and `cancel_reason_message`, which can be null. A returned `order_id` is not proof of a fill: even a zero-fill IOC receives one. `order market` exits 1 with the response retained unless status is `FILLED`; partial/no-fill and `UNKNOWN` outcomes require account/order readback before another write. `order limit` and triggers accept `OPEN` or `FILLED`; cancelled/unknown outcomes exit 1.

<a id="order-book-context"></a>**Order-book context (`book`).** An `order market` that comes back `CANCELLED` with part or all of its quantity unfilled (cancel reason `NO_LIQUIDITY`, nothing at the limit, or `IOC_REMAINDER`, the rest after a partial fill) reads `GET /v2/market/{symbol}/depth` once, right after the send, and adds:

```json
"book": {
  "updated_at_ms": 1792600000123,
  "age_ms": 412,
  "best_bid": {"px": "2128.1", "qty": "0.4"},
  "best_ask": {"px": "2152.3", "qty": "0.02"},
  "limit_px": "2150.41",
  "touch_past_limit_pct": 0.09,
  "qty_within_limit": "0",
  "qty_within_limit_complete": true
}
```

- `updated_at_ms` is the venue's timestamp on that book, and `age_ms` its age when read (by the server's clock when the response's `Date` header allows; a `clock_warning` is added when this computer's clock is clearly off).
- `touch_past_limit_pct`: how far the best opposite price (the best ask for a buy, the best bid for a sell) is past the order's limit, in per cent of the limit. Positive means nothing could fill; `null` means that side of the book is empty.
- `qty_within_limit`: the visible quantity on the opposite side at or inside the limit. `qty_within_limit_complete: false` means all 1,000 levels served were inside the limit, so more may sit beyond them.
- The book is read after the order, so it no longer holds what the order took.
- A failed or slow read (2 s) gives `"book": {"unavailable": "<reason>"}` and never changes the order's own fields. `data market book --symbol X` reads the book again.
- The `error` then says what filled, that the book is as of `updated_at_ms`, and not to resend at the same or a nearby price until the book's timestamp has moved on, nor to widen the slippage without the user's yes. Other cancel reasons (self-trade prevention, risk) keep the generic error.
- A fully filled order reads no book. Inside the runner the read is one extra REST call, made only after such a fill shortfall. The read starts with the send's reply, so arming `--sl`/`--tp` never waits for it.

`positions close_all` does the same for each close: a close entry that comes back `CANCELLED` with quantity unfilled carries `book` (one read per such close, started as its reply arrives, so the next close is not held back) and the same `error` advice.

With `--preview`, `order market` reads the book once and returns the same block, without the read-after-send caveats; `positions close_all --preview` does so for each planned close. When the book holds less than the order inside its limit, `warning` says the order would likely fill only part, or nothing on a one-sided book or one whose touch is past the limit.

`notional_usd` is the actual post-rounding requested order size (`qty × mark_price` for market orders, `qty × limit price` for limits), which may differ slightly from a requested `--notional`.

With `--notional`, the preview and the result also carry a `sizing` block that says how far: the quantity floors onto `qty_step_size`, so `$30` of SOL at a mark of 108.15 sends 0.27 SOL.

```json
"sizing": {
  "sized_by": "notional",
  "requested_notional_usd": 30.0,
  "effective_notional_usd": 29.2,
  "shortfall_usd": 0.8,
  "shortfall_pct": 2.67,
  "priced_at": "mark",
  "price_usd": "108.15",
  "qty_step": "0.01",
  "next_step_up": {"qty": "0.28", "notional_usd": 30.28},
  "message": "$30.00 at the mark 108.15 rounds down to 0.27 SOL (steps of 0.01) ≈ $29.20, 2.67% under. The next step up is 0.28 SOL ≈ $30.28."
}
```

- `effective_notional_usd` always equals `notional_usd`. `priced_at` is `mark` for `order market` and `limit` for `order limit`.
- `next_step_up` and `message` appear only when rounding took something off. Place the step up with `--qty` (`--qty 0.28`), so the size is exact whatever the mark does.
- `sizing` is absent with `--qty`. With `--max_size` or `--usage` it is the [margin sizing](margin.md#sizing---max_size-and---usage) block instead.
- Below the market minimum a `--notional` order sends nothing, and the error gives the minimum in dollars and the least `--notional` that reaches it: `$20.00 at the mark 2345 is 0.008 ETH, below minimum order qty 0.01 ETH (≈ $23.45 at this price). Use --notional 23.45 or more, or --qty 0.01.` A reduce-only order stays exempt from the minimum.

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

**order modify** returns `qty` (the new total), `limit_px_usd`, `exec_qty` and the create-response fields above, with the figures `order limit` computes: `mark_price_usd`, `limit_vs_mark_pct`, `account_value_usd`, `current_leverage`, `est_leverage`, `max_leverage`, `max_order_notional_usd`, `max_order_notional_source`, `reduces_position` and the `margin` block. The leverage and margin figures count only the unfilled remainder (new total qty minus filled qty). Before 0.9.1 a successful modify returned no `margin` block.

### Fat-finger guards

`order market`, `order limit` and `order modify` read the account before signing and refuse, with nothing sent, an order that fails one of these checks. The refusal exits 1 with every computed field, `refused_by` (each entry has `check`, `message` and the `override` flag, or `null` when none exists) and an `error` joining the messages. No client order ID is assigned to a refused order.

| `check` | Refused when | Override |
| -- | -- | -- |
| `limit_price_band` | A limit price is more than 5% through the mark (a buy above it, a sell below it). Passive prices are never refused. More than 50% has no override. | `--force` |
| `max_order_notional` | The order's notional is above the per-order cap: `REYA_MAX_ORDER_NOTIONAL` (USDC) if set in `.env`, else the account value. | `--allow_large` |
| `max_leverage` | Estimated leverage after a full fill is above `REYA_MAX_LEVERAGE` (default 5) and higher than before the order. | `--allow_large` |
| `account_value` | The account value is zero or negative. | none |
| `engine_margin` | The kit predicts the matching engine would reject the order for margin (liquidation threshold, initial margin or settlement reserve) if it filled in full at its limit price. The message is the engine's own, with the largest size that fits. | `--skip_margin_check` |

- **Account value** = equity as Core computes it: collateral after haircuts (withdrawal-only included; debts at price / (1 − haircut)) + unrealized PnL at mark; V2's real balance already includes funding. Fees are not included. Before 0.8.0 it used collateral without haircuts and no funding; before 0.12.27 it gave withdrawal-only collateral no credit and subtracted paid funding a second time.
- **Estimated leverage** = gross notional of all the account's positions at mark after a full fill ÷ account value. Resting orders are not counted. `current_leverage` is the same before the order; both are `null` when the account value is not positive.
- An order that only reduces the position (`--reduce_only`, or the opposite side for no more than the position) is never refused for size or leverage. A `--reduce_only` order is never refused for margin either; an opposite-side order without the flag is an ordinary order to the engine, so it gets the margin check and a `reduce_only_hint`. A reduce-only market order whose account reads fail is still sent, with `risk_unavailable` explaining why the figures are missing; any other order whose reads fail is refused.
- `order modify` checks the new price against the band and only the unfilled remainder (new total qty minus filled qty) against the cap and leverage.
- Why 5%: it equals the market-order slippage cap, so a marketable limit cannot go further than `order market` can without `--force`, and the venue busts fills more than 5% from its mark on chain anyway. The mainnet TP/SL band (300 bp) applies only to trigger children.
- Inside the runner the same guards apply and the overrides are unavailable; the runner's policy is the operator's limit.
- `--preview` returns the same fields with `preview: true` and `would_send` (`false` when `refused_by` is non-empty), and exits 0 either way.

### Entry protection

`order market <symbol> --side S (--qty N | --notional N) [--sl L] [--tp L]` opens or adds to a position and arms its stop-loss and take-profit in one command. Position reads trail an order's fill by about a second, so protection waits for evidence rather than time:

1. **Before sending**, the kit reads the position and open orders and adds these refusals to `refused_by` (none has an override), alongside the [fat-finger guards](#fat-finger-guards). Inside the runner, with verified WebSocket wallet state, both come from that state instead: the position with the runner's own acknowledged fills, and open orders with the triggers it has acknowledged but the feed has not shown yet ([latency and warm state](account-setup-and-runner.md#latency-and-warm-state)).

   | `check` | Refused when |
   | -- | -- |
   | `protection_exists` | A trigger of the requested kind is already armed on the market. It closes the whole position, this entry included, and the venue allows one stop-loss and one take-profit per position. |
   | `protection_opposite_position` | The open position is on the other side, so the entry would reduce or flip it. |
   | `protection_fires_at_once` | A level is already through the mark (a long's stop-loss at or above it, a take-profit at or below it; mirrored for a short). |

   `--preview` adds `protection_plan`: per leg `level`, `est_trigger_px_usd`, `est_limit_px_usd`, `est_trigger_vs_mark_pct`, and signed `est_pnl_at_trigger_usd` / `est_pnl_at_limit_usd` for this entry's quantity before fees, estimated from the mark. A take-profit whose child limit is past the estimated entry (the mark, blended with any position it adds to) also carries `warning`: see [take-profit below the entry](#take-profit-below-the-entry).
2. **The entry** is sent once, exactly as without the flags.
3. **After the fill**, percent levels are measured from `avg_fill_px` (`reference`). The kit reads positions every 0.5 s until the position shows the entry's side with a newer `lastTradeSequenceNumber` than before the entry. That number is the sequence of the last execution the position includes, the same counter as an execution's `sequenceNumber`. The wait ends at that evidence. Its ceiling is the deployment's indexing ceiling, the same bound for which the runner counts its own unshown fills ([latency and warm state](account-setup-and-runner.md#latency-and-warm-state)), set by the configured API URL: **15 s on mainnet** (`https://api.reya.xyz/v2`, at most 31 reads) and **30 s anywhere else**, such as devnet1 or Cronos (at most 61 reads). `REYA_MODE=live` requires the mainnet URL and `REYA_MODE=devnet` refuses it, so the mode always agrees. Inside the runner, when its WebSocket wallet state was verified just before the send and still is, it checks the feed's position at each update instead, within the same ceiling, and the sequence must also be newer than the feed's position before the send. If the feed is or becomes unavailable, REST reads take over for the rest of the ceiling, never a new one.

   Why these ceilings: on mainnet from 13 to 19 October 2026 a fill reached the feed a p99 of about 1.5 s after its block and at worst 4.15 s, never over 5 s; with an estimated 0.5–3 s from acknowledgement to block, that is about 7 s at worst, half the ceiling, and REST trails the feed by under a second. On devnet1 on 19 October 2026 fills reached the feed 4.6–15.4 s late for half an hour, and bursts of other accounts' fills have backed its indexer up to about 10 s. A fill that is still not visible at the ceiling is either far behind or busted (a busted fill never appears), and nothing is placed either way.
4. Then it checks each level against a fresh mark and places the **stop-loss first, then the take-profit**: IOC children `--trigger_slippage` beyond the trigger, each with its own client order ID. It reads open orders back (up to 5 reads within 2 s), then the fee read runs. Inside the runner, with verified WebSocket wallet state, it checks the order feed at each update instead, within the same 2 s, and counts a trigger only once the feed shows it open and not fired, never because the venue acknowledged it. If the feed is or becomes unavailable, REST reads take over for the rest of the 2 s.

It never resends the entry, never retries a trigger and never closes the position. Inside the runner the command may take 15 s plus the ceiling, two trigger writes of up to 5 s each and the 2 s read-back, instead of 15 s: 42 s on mainnet, 57 s elsewhere. `protection` reports `position_wait_s` (how long it waited for the position, in seconds) and `position_wait_limit_s` (the ceiling that applied).

`protection` in the result:

| `state` | Meaning | Exit |
| -- | -- | -- |
| `protected` | Everything requested is armed. `verified` says whether open-order reads (inside the runner, the order feed) showed it. | 0, or 1 for an incomplete IOC fill |
| `partial` | The stop-loss is armed; the take-profit was not placed. | 1 |
| `unprotected` | The entry filled, and the requested stop-loss is not armed (or nothing requested was armed). This includes a position not visible within the ceiling (15 s on mainnet, 30 s elsewhere): then nothing is placed. | 1 |
| `not_placed` | The entry filled nothing; nothing was placed. | 1 (IOC not filled) |
| `unknown` | The entry's outcome is unknown; nothing was placed. | 1 |
| `pending` | The command stopped while placing protection (for example a runner timeout). | 1 |
| `not_requested` | The entry was sent without `--sl`/`--tp`; see [entries without a stop](#entries-without-a-stop). | As the entry |

On every state but `protected`, `not_placed` and `not_requested`, `error` starts with `protection.message`, which names what is armed and what is missing, gives the exact command to arm it, and says never to repeat the entry. A partial fill is protected as filled: the triggers close the whole position.

```json
{
  "action": "order_market",
  "symbol": "ETHRUSDPERP",
  "side": "long",
  "qty": "0.05",
  "order_id": "123456792",
  "status": "FILLED",
  "filled_qty": "0.05",
  "avg_fill_px": "2000",
  "protection": {
    "state": "protected",
    "message": "Armed: stop-loss at 1993 (limit 1973.07), take-profit at 2010 (limit 1989.9). They close the whole long position (0.05 ETHRUSDPERP) when they fire.",
    "reference": "avg_fill_px",
    "reference_px_usd": "2000",
    "position_wait_s": 0.84,
    "position_wait_limit_s": 15.0,
    "position": {"side": "long", "qty": "0.05", "last_trade_sequence_number": 152954},
    "stop_loss": {
      "level": "0.35%",
      "trigger_px_usd": "1993",
      "limit_px_usd": "1973.07",
      "time_in_force": "IOC",
      "order_id": "123456793",
      "client_order_id": "2131494745023730871",
      "status": "OPEN",
      "cancel_reason": null,
      "armed": true,
      "visible": true,
      "command": "order sl ETHRUSDPERP --side long --trigger_px 1993 --limit_px 1973.07 --time_in_force IOC"
    },
    "take_profit": {"level": "0.5%", "trigger_px_usd": "2010", "limit_px_usd": "1989.9", "armed": true, "visible": true, "...": "..."},
    "verified": true
  }
}
```

A take-profit whose child limit is past the position's average entry carries `warning`; it is still placed. A leg that was not placed has `armed: false` and an `error` (with the venue's `error_code` when it rejected it; a lost reply also names the leg's `client_order_id` to look for in `query orders`). A leg not requested is `null`. A mark that cannot be read leaves the levels unchecked and adds `warning`.

A resting limit entry cannot carry protection: it can fill later or in parts after the command exits. Place the limit, then arm `order sl` / `order tp` once `query positions` shows the fill. Entry orders that carry their own TP/SL, armed by the matching engine on fill, would remove the wait altogether; the API does not offer that today.

#### Entries without a stop

An `order market` that opens or adds to a position without `--sl`/`--tp` reports `protection` with `state: "not_requested"`, in the `--preview` plan and in the result. A close (`--reduce_only`, or an opposite order no larger than the position) reports none.

| Field | Meaning |
| -- | -- |
| `stop_loss`, `take_profit` | `null`: none was requested with this order. |
| `stop_loss_armed`, `take_profit_armed` | `true` when one is already armed on the market for the position's side (it closes the whole position, this order included, as after slice 1 of a sliced entry); `false` when none is; `null` when the open orders could not be read. |
| `message` | What protects the position, in words to pass on: when `stop_loss_armed` is `false`, that the position has no stop-loss on Reya. |

It never changes the exit code or the order. The open-order read follows the send (inside the runner it uses the warm order feed), so it never delays the order; a failed read gives `null`, never an error.

```json
"protection": {
  "state": "not_requested",
  "stop_loss": null,
  "take_profit": null,
  "stop_loss_armed": false,
  "take_profit_armed": false,
  "message": "This long position has no stop-loss on Reya: none was requested with this order (--sl) and none is armed on ETHRUSDPERP."
}
```

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

**Position-match guardrail:** the trigger must protect a real position — there must be an open position on the symbol for `PERP_ACCOUNT_ID_1`, and its side must match `--side`. Position reads trail fills by about a second, and on devnet at times by 15 s or more, so the kit reads until the position shows that side (a flip shows the old side first), within the deployment's indexing ceiling: 15 s on mainnet, 30 s elsewhere, the same ceiling as after an [entry with protection](#entry-protection). It reads every 0.5 s, so at most 31 reads on mainnet and 61 elsewhere. Inside the runner, with verified WebSocket wallet state, it checks the feed's position at each update instead, within the same ceiling, and reads REST when the feed is unavailable; if the feed drops during the wait, REST gets only the rest of the ceiling. A missing position (the trigger would protect nothing) or a side mismatch (the close direction and immediate-fire check would both be wrong) is rejected with an error unless `--force` is passed; when forced, the order is placed and the response carries a `warning` field. Either refusal comes only after the full ceiling: a fill the venue has not indexed yet reads exactly like no position, and refusing it early would leave the position without its stop. Inside the runner `order tp` / `order sl` may take 15 s plus the ceiling and one trigger write of up to 5 s, instead of 15 s: 35 s on mainnet, 50 s elsewhere.

<a id="tp-sl-band"></a>**TP/SL band guardrail:** the matching engine admits a TP/SL only when `|limit_px - trigger_px| <= trigger_px x band`, compared in E9 integer prices with the bound rounded down. The band is the engine's collar less its safety margin, 500 - 200 = 300 bps on every market on mainnet and devnet1, from the engine's configuration (verified 21 Oct 2026). After rounding both prices to the tick, `order tp` / `order sl` refuse a limit outside the band before reading the position or signing anything, so nothing is sent. The refusal is in `refused_by` (check `trigger_band`, override `--force`) with `band_bps`, `distance_bps`, `admissible_limit_px` (`min`, `max`, on the tick) and `closest_limit_px`. `--force` sends the limit anyway and adds a `warning`: it exists only in case the venue widens its band before the kit's constant changes, and it is unavailable inside the runner like every override. `order market --sl/--tp` legs are never refused: `--trigger_slippage` is capped at the same band.

When the venue still answers `TRIGGER_LIMIT_OUTSIDE_BAND_ERROR`, the result keeps `error_code`, the venue's `message` and `client_order_id`, adds `trigger_band` with `reason` and the band figures, and `error` says what to do:

- `no_band`: the venue's message says the market has no band, so it accepts no TP/SL and a position there cannot carry a stop. Do not open a protected entry on it; close the position to exit. After `order market --sl/--tp` the protection message says so instead of offering the arm command.
- `outside_band`: the limit is outside the venue's band. If the kit's own guard admitted it, the venue's band is now narrower than 300 bps: move the limit closer.
- `unknown`: the message names neither cause (the API's fallback text). `error` gives the admissible range; a limit inside it that is also rejected means the market accepts no TP/SL.

**Immediate-fire guardrail:** a trigger on the wrong side of the current mark price (a TP at or below it for a long, an SL at or above it, mirrored for shorts) would fire the moment it lands and attempt to close at the selected limit. Such triggers are rejected with an error unless `--force` is passed; when forced, the order is placed and the response carries a `warning` field. To close a position intentionally, prefer `positions close_all --symbol X`.

<a id="take-profit-below-the-entry"></a>**Take-profit below the entry (a warning, not a refusal):** a trigger fires on the mark, but its child fills against the order book at any price up to `limit_px`. On a thin book the best bid can sit well below the mark, so a long's take-profit whose limit is below the entry (or a short's above it) can close the position at a loss. In a devnet rehearsal a long's take-profit triggered at 2356.567 and filled at 2353.681, below the 2358.069 entry. `order tp` then adds `warning` with the distance and the fix (a `--limit_px` at or beyond the entry, which may then not fill), and `order tp` / `order sl` report the position's `entry_px_usd`. With `order market --tp` the same check applies to each leg: the child limit sits `--trigger_slippage` past the trigger, so a take-profit closer to the entry than that is always below it.

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

It reads positions, then open orders, before sending anything, and never cancels a take-profit or stop-loss before a close: they stay armed while the closes are sent, so a close that fills only part leaves the rest protected, and the venue removes them once the position is flat (cancel reason `POSITION_CLOSED`).

Unlike the other write commands, `close_all` sends the venue's position quantity verbatim. A position below `min_order_qty` closes like any other, because reduce-only orders are exempt from the minimum. A position that is not a multiple of the market's step (ADL rescaling can leave one) is flagged `off_step_qty: true`: the API rejects off-step quantities on every order (`PRICE_QTY_BOUNDS_ERROR`), so a rejected close carries a `hint` with the reduce-only `order market` that closes all but the part below one step. It never automatically resubmits an ambiguous failure.

A fill the venue has not indexed reads as no position. So when a market in scope has a take-profit or stop-loss but positions show none there (or, inside the runner, its verified feed shows a position REST does not), the command reads again, for up to the indexing ceiling (15 s on mainnet, 30 s on devnet), and closes the position if it shows; inside the runner it also waits, within the same bound, for the feed to show the session's own fills. If none shows, nothing is closed or cancelled on that market and `errors` gets an entry with the context `Position not shown`, its `symbol`, and an `error` that names both causes (an unindexed fill, or TP/SL left over from a closed position).

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

With `--cancel_orders`, the other open perp orders in the same scope (`--symbol` or `--all`) are cancelled one by one before any close is sent; take-profits and stop-losses are not. Only a **confirmed** cancellation stops that order from reopening the position. Positions are then read again, so a fill that raced a cancel is closed too. The response adds `cancelled_count` and `cancelled`. A failed cancellation, or a failed open-order read, is reported under `errors` with the context `Cancel before close`, and flattening continues, so that order may still be live and can fill after the close. When `errors` is present, run `query orders` and `query positions` before treating the account as flat. If the position read after the cancels fails, nothing is closed, the TP/SL are kept (`kept_protection`), and the result still lists the confirmed cancellations; if the first position read fails, nothing is sent at all.

Once the read-back shows a market flat, `--cancel_orders` reads the open orders once more and cancels any take-profit or stop-loss still listed there. Every TP/SL in scope is then reported once: `cancelled_protection` (cancelled by the kit, with each cancellation's status), `retired_protection` (order IDs the venue had already removed) or `kept_protection` (on a market that is not flat, still protecting what is there). A failure in this step is reported under `errors` with the context `TP/SL cleanup`.

A preview adds `would_cancel_count` and `would_cancel` (the orders other than TP/SL) and `would_keep_protection` (the TP/SL left armed while closing; `null` when the open orders could not be read). Spot orders are not included.

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

`drops_protection` lists the account's take-profit and stop-loss on that market. They cover the whole position, so the venue cancels them once the position is flat (cancel reason `POSITION_CLOSED`); after a partial fill they stay armed for the rest. It is `null` when the open orders could not be read; the preview then reports the read failure under `errors` (context `TP/SL read`, after `Cancel before close` with `--cancel_orders`) and exits 1. `would_drop_protection_count` totals the lists: `null` if any is unknown, `0` when nothing would close.

A preview reads positions once. When a market in scope has a take-profit or stop-loss but no position shows there, it adds `positions_not_shown` (each `symbol` with its `protection`) and a `warning`, and `would_drop_protection_count` is `null`: the real call waits for that position and closes it if it shows, which also ends its TP/SL.

## Hosted strategies (`strategy.py`)

`strategy info | draft | validate | preview | start | status | stop`: a thin client of Reya's hosted strategy service. `validate` and `preview` ask the service (the only evaluator); `start` and `stop` are EIP-712 requests signed with the agent key and refused for any account other than the recorded Agent account. Options, every output field, the named skip, stale, stop and completion reasons, and lost-answer handling are in [hosted strategies](hosted-workflows.md).

## Latency probe (`latency.py`)

```bash
python -m agent_kit.scripts.latency [--symbol ETH] [--samples 5]
```

Times, as medians in milliseconds, one market-data request (`round_trip_ms`) and the same passive limit order through the runner with every input read over REST (`rest_path`) and with warm WebSocket state (`warm_path`). Each path lists the HTTP `requests` the order made before it would be sent and the `outcome` (`would_send`, or the guard that refused it). Reads go to the live API for `PERP_ACCOUNT_ID_1`. Order entry is a local stub, orders are signed with a throwaway key, and writer state goes to a temporary directory, so nothing reaches the venue and a running runner is not disturbed. A real order adds one order round trip to each path.

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
