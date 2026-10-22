# Error codes and retry safety

This page maps every error a kit write or read can return to its meaning, likely cause, next step and, for writes, whether resubmitting is safe. It covers the Reya V2 REST API codes in the pinned SDK enum (`RequestErrorCode`, SDK 3.6.3), plus the HTTP-level failures the kit can hit before or after the API.

## Read this first

- **The kit never resubmits a write automatically.** The SDK's HTTP retry applies only to non-POST methods, and every order-entry call is a POST. A retry is always a new command, signed with a fresh nonce.
- **Every venue verdict is HTTP 400.** The status code alone does not tell you whether the operation was applied. Branch on `error_code`.
- **Neither an order ID nor a missing record proves the outcome.** A zero-fill IOC still gets an order ID. The engine keeps no record of terminal orders, and fills can show up after the response. Absence is not proof that nothing happened.
- **When the outcome is unknown, reconcile before any new write.** Use `query orders`, `query executions --order_id ID`, `query positions` and `data reconcile-orders`. Search for the `client_order_id` the kit printed; every create carries one. Leave the attempt unresolved until the evidence settles it.

## How the kit reports an error

Every failure prints JSON with `error` and exits 1. For SDK HTTP failures, the kit also adds `error_code`, `message` and `http_status` when it can:

- **Venue codes:** taken from the JSON body's `error` field when it is an upper-case code.
- **Regional block:** a 403 carrying the "Not available in your region" page becomes `error_code: "REGION_UNAVAILABLE"`.
- **Edge rate limit:** any HTTP 429 becomes `error_code: "EDGE_RATE_LIMITED"`.
- **Any other body:** becomes `error_code: "API_ERROR"`.
- **Timeouts and connection errors:** print only `error`, as `"<ExceptionType>: ..."`, with no `error_code`.
- **Retry hint:** a `retryAfterMs` in the body is returned as `retry_after_ms`.
- **Creates:** every create result, including an error, carries the `client_order_id` the kit sent.
- **Inside the runner:** a single write rejected with `RATE_LIMITED_ERROR`, `SERVICE_UNAVAILABLE_ERROR`, `POST_ONLY_WOULD_CROSS_ERROR`, `ORDER_NOT_FOUND_ERROR` (cancel or modify), `MODIFY_QTY_BELOW_FILLED_ERROR` (modify), `REDUCE_ONLY_CONDITION_NOT_MET_ERROR` (`order market --reduce_only`) or `EDGE_RATE_LIMITED` adds `session_continues: true`, and the session keeps running. So does a market, limit or modify acknowledged as `CANCELLED`. Every other write error still stops the session for reconciliation. A failed cancel-all-after renewal with `SERVICE_UNAVAILABLE_ERROR`, `RATE_LIMITED_ERROR`, `ORDER_OUTCOME_UNKNOWN_ERROR`, a 5xx, a timeout or a transport error is retried while it can still be confirmed before the countdown's deadline; `EDGE_RATE_LIMITED` or any other renewal error stops the session at once. See [the runner guide](account-setup-and-runner.md#coordination-cancellation-and-recovery).

## Retry column

| Value | Meaning |
|---|---|
| **Rejected** | Rejected before acceptance. Source shows that either the API refused before handing the request to the matching engine, or the engine refused it before any state change. Fix the input, then resubmitting is safe. |
| **Unknown** | Unknown outcome. Never resubmit; reconcile first. |
| **N/A** | Read-only. Nothing to retry safely or unsafely. |

## Validation

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `INPUT_VALIDATION_ERROR` (specific `message`) | Malformed or inconsistent request. Examples: bad `orderType`/`timeInForce`, `qty` on a TP/SL, missing `qty` on a limit order, `triggerPx` rules, more than 9 decimals, a reduce-only/post-only combination the engine refuses, malformed JSON. | Fix the field named in `message`. | **Rejected** |
| `INPUT_VALIDATION_ERROR` with `message` exactly `Input validation failed` | The controller catch-all for any unexpected exception in create/modify/cancel/cancel-all-after. It can fire **after** the engine accepted the order: response-building assertions run on the engine's success reply. | Reconcile. | **Unknown** |
| `PRICE_QTY_BOUNDS_ERROR` | Possible causes: price above the maximum or off tick; qty above the maximum, below `min_order_qty` or off step. Seen live on devnet1 (11 Oct): `Order quantity 0.001 is below minimum order base 0.01`, with no order ID and no position change. | Check `query market info --symbol X`. Meet `min_order_qty`/step/tick. For dust below the minimum, follow the `close_all` hint. | **Rejected** |
| `NUMERIC_OVERFLOW_ERROR` | An ID, nonce, deadline, expiry or `triggerPx` exceeds uint64. This is usually a units bug (ms vs s, E18 vs decimal). | Fix the value. | **Rejected** |
| `ORDER_DEADLINE_PASSED_ERROR` | The signature `deadline` is already past: host clock skew, or a long gap between signing and sending. | Check the host clock (NTP), then resubmit. | **Rejected** |
| `ORDER_DEADLINE_TOO_HIGH_ERROR` | The `deadline` of a non-trigger IOC is more than 600 s ahead. Usually the host clock is fast. | Fix the clock, then resubmit. | **Rejected** |
| `ORDER_EXPIRES_TOO_SOON_ERROR` | `expiresAfter` falls inside the engine's settlement window, or has already lapsed on the engine clock. | Use a later `expiresAfter`. | **Rejected** |
| `TRIGGER_IOC_MUST_NOT_EXPIRE_ERROR` | An IOC TP/SL carries `expiresAfter`. | Drop the expiry, or use GTT. | **Rejected** |
| `TRIGGER_LIMIT_OUTSIDE_BAND_ERROR` | One code, two causes, told apart by the venue's `message`. "…outside the market's fill-collar band…": the TP/SL `limitPx` is further from `triggerPx` than the band allows, which is 3% (300 bps) of the trigger on every market (the engine's configuration, verified 21 Oct 2026). "…band is unconfigured for this market…": the market has no band, so it accepts no TP/SL at all. The kit refuses an `order sl` / `order tp` limit outside 3% before sending (`refused_by` check `trigger_band`; `--force` sends it, outside the runner only), and its own `--sl`/`--tp` legs always fit. When the venue still answers this code, the result adds `trigger_band` with `reason` (`outside_band`, `no_band`, or `unknown` when the message names neither) and plain wording in `error`. | `outside_band`: use a `--limit_px` within 3% of the trigger (`trigger_band.admissible_limit_px`); if the kit's own guard passed it, the venue's band has narrowed, so move it closer. `no_band`: do not place stops or open a protected entry on that market; to exit, close the position. | **Rejected** |
| `TRIGGER_ALREADY_EXISTS_ERROR` | This position already has an armed trigger of this type. Each position allows one SL and one TP. | `query orders`, then modify or cancel the existing trigger. | **Rejected** |
| `POST_ONLY_WOULD_CROSS_ERROR` | A post-only order would take liquidity. The engine marks it Rejected with order ID 0 and no trades; the nonce is consumed. | Reprice away from the touch. | **Rejected** |
| `DUPLICATE_CLIENT_ORDER_ID_ERROR` | A live order or armed trigger already uses this `clientOrderId` for this signer. It is often your own earlier attempt. | `query orders` for that client ID. Do not change the ID to force a second order. | **Rejected** (this request; the other order is live) |
| `ORDER_NOT_FOUND_ERROR` | The cancel/modify target is not live: it filled, was cancelled, expired or never existed. The engine keeps no record that tells these apart. | Do not assume it was cancelled. Check `query orders` and `query executions --order_id ID`. | **Rejected** (the request had no effect) |
| `MODIFY_QTY_BELOW_FILLED_ERROR` | The new qty is below what has already filled. The order is unchanged. | Read fills; restate with qty ≥ filled. | **Rejected** |
| `EMPTY_MODIFY_ERROR` | The modify changes no field. The order is unchanged. | None. | **Rejected** |

## Risk / margin

All of these are refused at admission. No order is created, and a refused modify leaves the resting order untouched.

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `ACCOUNT_BELOW_LIQUIDATION_MARGIN_ERROR` | The account is already below its liquidation threshold (`health_usd` < 0 in `query margin`); the message gives the shortfall. The engine rejects every order then, reduce-only closes included. | Add collateral. Check `query margin` and `query positions`. | **Rejected** |
| `ACCOUNT_BELOW_INITIAL_MARGIN_ERROR` | Two causes, told apart by the message. *"…below the initial margin requirement…"*: the account is below initial margin before the order, or a full fill at the limit price would leave it below. *"…below its settlement reserve…"*: health after the fill is below 1% of the order's notional at its limit price (plus unsettled fills). On the 25x and 40x markets the reserve binds first, so an order sized at `max_leverage` is rejected. A strict reduce-only order or TP/SL never gets this; an opposite-side order without `--reduce_only` can. | The kit's `--preview` predicts both (`margin.engine_check`); size with `--max_size` or `--usage`, tighten `--slippage` (a market order is charged its full slippage up front), add collateral, or close with `--reduce_only`. See [margin.md](margin.md). | **Rejected** |
| `REDUCE_ONLY_CONDITION_NOT_MET_ERROR` | Reduce-only would not strictly reduce, for one of three reasons: no position; wrong side or would flip; or qty above the worst-case reducible amount (settled position minus in-flight reductions). An opening fill counts only once settled. Seen live on devnet1 (11 Oct): `reduce-only order exceeds the worst-case reducible quantity`, with no order ID. | `query positions`. Wait for the opening fill to settle and size at or below the settled position. Do not blind-retry. | **Rejected** |
| `OPEN_INTEREST_BUDGET_ERROR` | The market's budget for matched-but-unsettled OI is exhausted. It usually clears within seconds. | Wait briefly, then resubmit or reduce size. | **Rejected** |
| `OPEN_INTEREST_CAP_ERROR` | The market's OI cap is reached. The current engine has no emitter for this code. | Reduce size or wait for OI to fall. | **Rejected** |
| `INSUFFICIENT_BALANCE_ERROR` | Spot only: the balance cannot fund the order. | Reduce size or fund the account. | **Rejected** |

## Auth / signature / nonce

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `UNAUTHORIZED_SIGNATURE_ERROR` | Either the signature does not recover to `signerWallet`, or the signer has no trade permission on the account. | Check the key ↔ wallet ↔ account pairing and the deployment. Do not retry unchanged. | **Rejected** |
| `UNAUTHORIZED_ACCOUNT_ERROR` | The account may not act here. Causes: the order belongs to another account, the account lacks access to a private market, or a spot/perp account mismatch. | Check `PERP_ACCOUNT_ID_1` and the order's owner. | **Rejected** |
| `INVALID_NONCE_ERROR` | The nonce is not above the signer's high-water mark, or is missing. Usual causes: another writer is using the same signer, or nonce state was reset. It is not a receipt for any earlier attempt. | Stop competing writers. Never reset the kit's nonce or journal files. If this followed an uncertain write, reconcile first. | **Rejected** (this request only) |
| `NOT_WHITELISTED_ERROR` | The account owner is not whitelisted for order entry. Ejected wallets get the same answer. | Do not retry; contact Reya. | **Rejected** |
| `ACCOUNT_SUSPENDED_ERROR` | The venue's kill switch has suspended the account. | Do not retry; contact Reya. | **Rejected** |

## Rate limit

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `RATE_LIMITED_ERROR` (HTTP 400) | The account's order-rate bucket is empty. The kit returns the body's `retryAfterMs` as `retry_after_ms`. | Pause writes, back off for at least a few seconds, then resubmit. | **Rejected** |
| `OPEN_ORDER_COUNT_EXCEEDED_ERROR`, `OPEN_ORDER_NOTIONAL_EXCEEDED_ERROR` | The account's cap on resting-order count or notional is reached. No retry hint. | Cancel resting orders first. | **Rejected** |
| `CAPACITY_LIMITED_ERROR` | The venue is shedding creates and modifies under load. Cancels are unaffected. | Back off, then resubmit. | **Rejected** |
| HTTP 429 (kit: `EDGE_RATE_LIMITED`) | Mainnet edge per-IP ban, from either of two rules. Over 2,000/min on paths starting `/v2/createOrder`, `/v2/modifyOrder`, `/v2/cancelOrder` or `/v2/cancelAll` (this includes `cancelAllAfter`). Over 1,500/min on any other REST path. For 60 s the IP gets 429 on every request in that rule's scope, with no `Retry-After` header. The API itself never sends 429. | Stop requests from this IP for at least 60 s. During the ban, REST cancels and the runner's cancel-all-after refresh are also blocked. | **Rejected** for writes (blocked at the edge); **N/A** for reads |

## Availability

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `SERVICE_UNAVAILABLE_ERROR` | An internal dependency failed before dispatch: no matching-engine connection, the signature verifier, or the permission/owner lookup. It also covers requests never written to the engine socket (queue full, or the connection dropped before the write). | Back off, then resubmit (new nonce). | **Rejected** |
| `SERVICE_DISABLED_ERROR` | Order entry is not open on this market: it is not trading, is wound down or is unknown to the engine. Alternatively, spot entry is disabled. Cancels still work. | Do not retry until the market trades again. | **Rejected** |
| `TRADING_HALTED_ERROR` | The engine is shutting down or trading is halted. | Wait, check status, resubmit later. | **Rejected** |
| `CROSSING_ORDERS_TEMPORARILY_UNAVAILABLE_ERROR` | Crossing orders are refused because the risk engine is not ready, its DB timed out, or settlement is paused. Resting orders, modifies and cancels still work. | Wait briefly, then resubmit the same order (new nonce). | **Rejected** |
| HTTP 403, "Not available in your region" HTML (kit: `REGION_UNAVAILABLE`) | Regional block on the API and WebSocket hosts; the request never reached the API. | Stop. This is a compliance block: do not route around it with a VPN or proxy, and tell the operator. | **Rejected** (nothing sent) |

## Unknown

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `ORDER_OUTCOME_UNKNOWN_ERROR` | Either the request reached the engine socket but no answer came back (connection closed or timed out), or the send failed in a way the API cannot classify. It may have applied. | Reconcile. Never resubmit with a fresh nonce while it is unresolved. | **Unknown** |
| `CREATE_ORDER_OTHER_ERROR`, `CANCEL_ORDER_OTHER_ERROR`, `MODIFY_ORDER_OTHER_ERROR`, `CANCEL_ALL_AFTER_OTHER_ERROR` | The engine returned a code with no public mapping, such as an internal fault or a version mismatch. `MODIFY_ORDER_OTHER_ERROR` also covers modifying a non-LIMIT order. | Reconcile. Report to Reya with the time and `message`. | **Unknown** |
| `INPUT_VALIDATION_ERROR` with `Input validation failed` | See [Validation](#validation). | Reconcile. | **Unknown** |
| HTTP 5xx: 500 `INTERNAL_SERVER_ERROR`, or 502/503/504/52x from the proxy or CDN | Server or proxy failure, possibly after the API forwarded the request. | Reconcile. | **Unknown** |
| Timeout or connection error (no `error_code`; `error` starts `TimeoutError:`, `ClientConnectorError:`, ...) | The reply was lost or never arrived. Only a failure to connect proves nothing was sent, and the kit does not distinguish that case. | Reconcile. | **Unknown** |
| `API_ERROR` with any other status, SDK `UNKNOWN`, or any code not on this page | Unrecognized body, or a code newer than the pinned SDK. | Reconcile. | **Unknown** |

## Read-only

| Code | Meaning / likely cause | Next step | Retry |
|---|---|---|---|
| `SYMBOL_NOT_FOUND_ERROR` | Unknown or unavailable symbol on a market-data read. | Check the symbol with `query prices`. | **N/A** |
| `NO_ACCOUNTS_FOUND_ERROR` | The wallet has no accounts on this deployment. | Check `PERP_WALLET_ADDRESS_1` and the selected deployment. | **N/A** |

## Not errors

Some outcomes return HTTP 200 and are not errors: no liquidity, an IOC remainder, and self-trade prevention. They arrive as `status: CANCELLED` with `cancel_reason`. Kit exit codes and readback rules for these are in SKILL.md.

## Sources

Classified from source on 12 October 2026: the deployed API, the matching engine and Reya's edge configuration, plus the pinned SDK 3.6.3 enum. Codes newer than the pinned SDK fall under **Unknown**. Re-check this page when the SDK pin or the API revision changes.
