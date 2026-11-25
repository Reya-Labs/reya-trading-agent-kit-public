# V2 data, accounting and recovery contract

## Endpoint inventory and exception decision

All new data/stream commands use only V2. The pinned SDK `v3.6.3.0` uses a `/v2`
base for reference, market, wallet and order-entry calls. `_env.py` rejects other
base paths, credentials in URLs, query strings and fragments. No fallback route,
RPC provider or transaction broadcaster is introduced.

| Capability | Exact REST method/path (relative to host) | WebSocket channel / treatment |
| --- | --- | --- |
| Market discovery | GET `/v2/perpMarketDefinitions` | REST metadata; validated full symbols or search |
| Summaries, prices, funding | GET `/v2/perpMarkets/summary`, `/v2/perpMarket/{symbol}/summary` | `/v2/perpMarket/{symbol}/summary` |
| Books | GET `/v2/market/{symbol}/depth?limit=1..1000` | `/v2/market/{symbol}/depth`, top 100 per side |
| Candles | GET `/v2/candleHistory/{symbol}/{resolution}?endTime=...` | No candle subscription added; one bounded page, up to 200 |
| Recent market trades | GET `/v2/market/{symbol}/perpExecutions` | Same channel |
| Busts | GET `/v2/market/{symbol}/executionBusts`, `/v2/wallet/{address}/executionBusts` | Same channels |
| Accounts, positions, balances | GET `/v2/wallet/{address}/accounts`, `/positions`, `/accountBalances` | Corresponding `/v2/wallet/{address}/...` channels |
| Account valuation | GET `/v2/wallet/{address}/accountSummaries`, `/positionRisk` (`?accountId=N`) | Channels exist; not used (the kit has no live view of these figures) |
| Orders | GET `/v2/wallet/{address}/openOrders`, `/orderHistory` | `/v2/wallet/{address}/orderChanges` |
| Settled executions | GET `/v2/wallet/{address}/perpExecutions` | Same channel |
| Transfer ledger | GET `/v2/wallet/{address}/transfers` with opaque cursor | Same channel; two account-side entries can describe one transfer |
| Collateral/reference | GET `/v2/assetDefinitions`, `/assetOraclePrices` | REST used by the pre-trade check and sizing (`trade.py`), not by account figures |
| Existing SDK write path | POST `/v2/createOrder`, `/modifyOrder`, `/cancelOrder`, `/cancelAll`, `/cancelAllAfter` | Existing trading/runner only; not called by data/stream |
| SDK startup/reference | GET `/v2/spotMarketDefinitions` alongside perp definitions | Existing SDK startup; no spot product added |

**Documented exception (12 October 2026):** the account adapter retains
POST `/api/transaction-gelato/executeReya` for unsigned account creation and
owner-signed transfers, plus GET `/api/owner/{owner}/metadata` for the Core
signature nonce used by transfer preview/signing. The published mainnet V2
OpenAPI checked on 10 October 2026 has neither an account-operation POST nor a
Core nonce replacement. These are existing isolated routes in `_account_relay.py`;
this PR neither expands them nor exercises them live. Trading order nonces and
Core transfer signature nonces are different domains and cannot be substituted.

Scope is exactly these two routes, for account creation and owner-signed transfers;
nothing else may be added under it. Retirement: migrate each operation when its
dedicated endpoint ships (the API team will roll them out one at a time), preserve
journal identity, and never retry an ambiguous old-route write on a new route. An endpoint migration
alone does not supply request lookup or server-side idempotency.

## CLI and output

```bash
python -m agent_kit.scripts.data market search --symbol ETH
python -m agent_kit.scripts.data market summary --symbol ETHRUSDPERP
python -m agent_kit.scripts.data market book --symbol ETHRUSDPERP --limit 50
python -m agent_kit.scripts.data market candles --symbol ETHRUSDPERP --resolution 5m
python -m agent_kit.scripts.data market trades --symbol ETHRUSDPERP --max-pages 3
python -m agent_kit.scripts.data market trades --symbol ETHRUSDPERP --type LIQUIDATION
python -m agent_kit.scripts.data accounts
python -m agent_kit.scripts.data state
python -m agent_kit.scripts.data history transfers --max-pages 10
python -m agent_kit.scripts.data reconcile-orders --max-pages 10
python -m agent_kit.scripts.stream market --feed depth --symbol ETHRUSDPERP --seconds 60
python -m agent_kit.scripts.stream wallet --feed transfers --seconds 60
```

`data` prints one JSON object. Decimal wire values remain strings; identifiers
are exact integers or strings, never coerced through float. `stream` prints
NDJSON observations for one channel, at most one hour per invocation. Scope
includes deployment, channel and wallet; run a new observation to switch scope.
It never loads private keys or submits actions. Wallet history intentionally
covers the whole wallet and labels that scope; account projection/reconciliation
requires an owned selected account.

The existing `query` commands remain available for compatibility. Their older
`prices`/`margin` display fields use numeric approximations; do not use those
rounded display values as exact account risk. Prefer `data` for lossless reads.

## Snapshots, freshness and history

A feed begins `recovering`, seeds from the subscription snapshot and merges
updates according to its published contract. For orders, the snapshot sequence
separates current open orders from later event rows. For immutable history feeds,
sequence IDs deduplicate snapshot, REST backfill and buffered live data. A
bounded reader queue continues collecting while REST pages load. Overflow,
malformed frames, conflicting records or unorderable depth diffs invalidate the
observation and cause bounded backoff/resubscription. No events are silently
dropped. Reconnect discards current state and requires a fresh snapshot; only
`orderChanges` carries its terminal orders and gap-free sequence across the
resubscription, so its REST backfill can restore fills made while disconnected.
The reconnect budget (`reconnects`, default 3) counts consecutive failed attempts
in one outage and resets once the feed serves again; the observation's duration
still bounds the total.

Depth uses **absolute quantity** updates, not deltas; zero removes a price from
the bounded view. A removed price can still exist deeper in the full book.
There is no public depth sequence cursor, so an out-of-order diff is not guessed
into place. Positions merge by account/exchange/symbol, zero removes a position;
balance updates merge by account/asset. Account-binding removal events remove the
old-owner entry. Wallet/environment changes never reuse another scope's cache.

`ready` means this observation has a snapshot and accepted updates, not matching
engine admission readiness. `data_age_seconds` is time since channel data was
observed; ping/pong or an open socket does not reset it. Quiet channels can become
`stale` after the chosen threshold even if transport remains connected. `incomplete`
coverage takes precedence over `stale` in `state`; the boolean `stale` field reports
quiet data independently. An `orderChanges` view holds open orders from its first
snapshot plus later changes. On resubscription it keeps terminal orders it already
observed and recovers terminal orders recorded in REST order history after its last
observed sequence, so a fill made while disconnected is not dropped. A fresh observer
excludes orders finished before it started (`order_scope`); use `data history` or
`reconcile-orders` for outcomes. A book's
`updatedAt` is a state-change time, not proof of transport liveness. No new write
path consumes these caches; `safe_for_order_admission` is always false.

REST transfers use the returned cursor unchanged. Other execution/order-history
reads use inclusive timestamp boundaries and sequence deduplication. Execution
timestamps are the block's time in whole seconds (milliseconds ending in 000), so
fills from one block, a liquidation cascade included, share one: rows with the same
timestamp are not the same event, and the kit orders them and deduplicates nonzero
`sequenceNumber`s, never by time (legacy rows with sequence zero are all kept; see
[history and performance](history-and-performance.md)). `market trades --type T` sends `type` (documented
values ORDER_MATCH, LIQUIDATION, ADL, MARKET_CLOSE) and also keeps only rows of that
type itself; `coverage.type_filter.removed_locally` counts rows from a server that
ignored the parameter. Paging still follows the pages as served. If 100 rows
share a boundary, advancing by a millisecond could lose rows: the kit instead
reports `timestamp_boundary_saturated` and `incomplete`. Page budgets remain
visible. Exhausting available history yields `bounded`, **never complete audit
coverage**: retention varies, order history is limited for high-volume wallets,
and absence does not prove nonexecution. The stream is an observation tool, not
a durable full-ledger recorder. Its 10,000-row capacity and 128-frame buffer (256 KiB per WebSocket frame)
fail visibly rather than silently pruning reconciliation evidence.

## Accounting contract

The inspected public balance projection already incorporates rUSD funding/ADL
settlement adjustments. The public position `qty` is already ADL-adjusted in the
source projection. Consume those values once. Raw funding/ADL/base-multiplier
tracker internals are not a supported public client dependency.

Reference scenario: stored base 10 with stored multiplier 1 and current multiplier
0.8 produces public qty 8. Raw cash 100, funding -5 and ADL cash +10 produce
realBalance 105. At entry 100 / mark 102, that adjusted position's marked PnL is
16, not 20: equity is 105 + 16 once, with no other funding/ADL term.

`data state` returns the account's balance and position records beside the API's
own valuation of them (`accountSummaries`, `positionRisk`, read for that account);
the kit computes no figure itself.
`effective_collateral_usd`, `estimated_marked_pnl_usd`, `margin_balance_usd` and
`estimated_equity_usd` are the summary's `collateralValue`, `unrealizedPnl`,
`marginBalance` and `equity`, exact strings; `account_summary` is the whole row.
`estimated_equity_usd` is equity as the Reya app shows it, before collateral
haircuts (before 0.18.0 it was the margin balance); margin is measured against
`margin_balance_usd`. Each position adds its `position_risk`
row (mark, notional, PnL, `fundingSinceChange`, liquidation margin and price) and,
for compatibility, `estimated_marked_pnl_usd`, `mark_price` and `prices_updated_at`
from it. The valuation is shown only when it is of exactly the positions recorded
(market, side, size and `lastTradeSequenceNumber`): when a fill lands between the
reads, the whole account's valuation is left out, since liquidation prices are cross
margin. Balances carry no per-asset value; the API values the total only. When the
API cannot value the account (HTTP 503 for a missing or stale price, or no valuation
for the account) or the valuation is of another fill, the records are still
returned, the figures are `null` and `valuation_unavailable_reason` says why;
missing values never become zero. It still reports `incomplete` for risk admission:
reads are not atomic, and the settlement reserve and in-flight fills are not
included. It is not a buying-power API. Where the API does not send `equity` yet
(it is new in its version 3.7.7), `estimated_equity_usd` is `null`, never the
margin balance, and the rest is shown; until then `position_risk` rows carry the
API's earlier `fundingPnl` (funding since entry) in place of `fundingSinceChange`.

`data reconcile-orders` joins account + symbol + order ID. Orders with the same
ID in different markets do not merge. Execution and bust sequence namespaces
remain separate. Partial fills are compared with observed settled and busted
quantities; the remainder is `settling_or_missing_history`, not a failed order.
ADL/market-close executions without an order ID are not attributed to an order.
The absence of a historical event cannot clear an existing runner uncertainty
flag; this read tool does not modify journals or approve retries.

## Transfer outcome recovery

The public contract explicitly makes sequence numbers opaque. Account recovery
no longer infers parity/adjacency. For an identified transaction it requires
exact owned source/destination, RUSD amount, opposite directions and a unique
debit/credit pair after bounded cursor traversal. Matching amount/time or a
balance change cannot identify a request. A second matching pair, incomplete
page budget, missing side or conflicting evidence leaves the journal unresolved.
All existing locking, intent persistence before submission, saved hash readback
and no-replay rules remain in force.

The transfer ledger is **net-deposit history**. It excludes realized PnL, funding
and ADL cashflows; `netDepositsAfter` is not `realBalance`. Two entries for an
internal same-wallet transfer net to zero wallet flow, not two user transfers.

## Evidence and remaining gaps

On 10 October 2026, unauthenticated read-only checks against mainnet and devnet1
returned market definitions, summaries, depth, 5m candles and market trades.
Candle reads require `endTime` (the CLI supplies current milliseconds when
omitted). Both mainnet and devnet1 depth streaming returned a snapshot and live
diffs. No account credentials, funded writes or operator canary
were used. Source/spec availability is not proof every wallet feed is deployed
or semantically current for every funded account.

| Gap / reproducer | Impact | Follow-up |
| --- | --- | --- |
| Lose relay reply before a transaction hash is saved | Exact transfer ledger cannot identify one of multiple equal intents | API owner to confirm durable request identity/status/dedup support |
| Creation reply lost, then concurrent accounts appear | Wallet set difference does not identify this creation | Supported creation-by-request/transaction lookup required |
| 100 history events at one millisecond or retention truncation | Cannot prove gap-free replay with time pagination | API-owner cursor/retention decision; client reports incomplete |
| Funding/ADL or fills change between REST responses | Independent exact account admission state unavailable | API-owner versioned/atomic derived state contract, not internal service or RPC fallback |
| Cronos/custom WebSocket URL not verified | No automatic mapping or host guessing | API owner to document supported mapping; REST remains independently available |
| Paper demonstration | First-use acceptance remains partial | Walkthrough evidence |
| Real client and funded-account walkthrough | Offline/public-data checks do not establish this | Operator acceptance |

Primary public contracts: [OpenAPI](https://api.reya.xyz/v2/openapi-spec.yaml),
[AsyncAPI](https://api.reya.xyz/v2/asyncapi-spec.yaml). The kit's earlier account
projection, before it read the API's valuation, was also checked against the Reya
app's own order-history reconciliation, balance and position views, and the
matching engine's risk calculation.

## Legacy execution identity and summary updates

Per-market summary snapshots and updates carry one object; array-shaped wallet
feeds remain strict arrays. Reconnection replaces the bounded observation window
with a fresh snapshot before applying updates.

The V2 service can expose older executions with `sequenceNumber: 0`. Zero is
missing identity, not a shared event ID. History and streams retain each raw
observation, including identical rows, and flag incomplete identity/coverage.
The current window's `unidentified_observations` count is not a unique-trade
count. Performance sums are unavailable when selected execution identity is
uncertain. Order reconciliation similarly returns null settled/unresolved
quantities and the raw unidentified observations for affected orders. Real
nonzero-ID conflicts continue to fail. Stable legacy API event identifiers
remain an additive integration dependency; amount/time/row hashes are not
substitutes for a venue identity.
