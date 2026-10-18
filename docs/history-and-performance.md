# History and performance: observed evidence, not inferred returns

`history` combines V2 order history, settled perpetual executions, execution
busts and collateral transfers for an explicit owner/account and time interval.
No private key or trading permission is needed. Data is filtered after wallet
pagination; coverage always describes the wallet page walk before local filters.

```bash
python -m agent_kit.scripts.history --wallet <owner-address> --account-id <id> --start-time <unix-ms> --end-time <unix-ms>
python -m agent_kit.scripts.history --wallet <owner-address> --whole-wallet --start-time <unix-ms> --max-pages 20
python -m agent_kit.scripts.history --wallet <owner-address> --account-id <id> --start-time <unix-ms> --symbol ETHRUSDPERP --include-current-state
```

Intervals are inclusive Unix milliseconds; omitted end freezes the interval at
invocation time. Order events use `lastUpdateAt`, not original creation time.
Current ownership is verified before selecting account scope. An unavailable
feed is labelled per feed, while independent supported feeds can still explain
activity. Never interpret a missing feed as zero activity.

## Output/accounting contract

- `order_records` preserves server updates, including symbol/order identity.
  `execution_records` emits account/role-specific settled evidence with stable
  exchange+symbol+fill-or-sequence+role identity when available. Nonzero-ID overlaps
  are deduplicated; conflicts fail. Legacy executions with sequence zero have no
  reliable event identity: every observation is retained (including equal rows),
  `identity` is null, and coverage is incomplete. No unique ID is invented.
  The SDK execution query also preserves legacy exchange-zero/sequence-zero
  rows with `exchange_scope_verified: false`, never as verified fill/fee evidence.
  `side` is the reported account's direction; `taker_side` preserves the raw API side.
- `execution_metrics_rusd` reports source-provided fee, opening fee, rebate,
  realized PnL, price-variation PnL and funding PnL **separately**. Optional legacy
  fields stay null. `observed_sum` includes only known values; record counts and
  `unavailable_or_partial` expose omissions. Every total is bounded to observed
  records, never a verified full-period return. If selected execution observations
  have unknown identity, sums are null with `unavailable_identity_uncertain`;
  amounts cannot safely be added across possible page/stream overlap.
- Settled fills and execution busts have separate sequence namespaces. A bust is
  rejected settlement evidence, not a negative settled fill. It is displayed
  independently and is not subtracted from realized PnL.
- `transfer_records` preserves source entries; `transfers_by_asset_and_type`
  groups signed amounts without converting non-rUSD assets at today's prices.
  `cashflow_totals_state` applies to both transfer totals: `incomplete` or
  `unavailable` means an empty or small total is not zero, and a symbol filter
  gives `partial_symbol_filter`.
- Exhausting a feed is not completeness. When in-scope fills reference orders that
  order history did not return, `orderHistory` becomes `incomplete` with
  `orders_missing_for_fills`. When a fee-paying taker fill is older than the
  account's earliest served transfer (fee transfers post at the fill's timestamp),
  `transfers` becomes `incomplete` with `fills_without_fee_transfers`. Both were
  observed on mainnet, where the API no longer serves older order and transfer rows.
  Fees in transfers are another view of the ledger, not extra charges to add to
  execution fees/PnL. Unknown/OTHER, pool and liquidation flows retain their labels.
- External deposits/withdrawals and identified transfers crossing the selected
  account set are cashflows, not profit. Missing counterparty identities are not
  guessed. Transfer IDs are opaque: no parity or adjacency is decoded. Same-wallet
  observations are grouped by transaction hash, reciprocal distinct accounts,
  asset and exact nonzero opposite amount. Exactly two reciprocal observations
  are labelled `balanced_observations`, with `observed_net_wallet_cashflow` zero;
  `leg_identity_verified` remains false and `net_wallet_cashflow` stays null.
  A capped page could contain opposite sides of different repeated transfers.
  Repeated same-amount observations in one transaction remain
  `incomplete_or_ambiguous`, with every record retained. An API leg ID and complete
  transaction coverage are needed to prove individual transfer pairing.
- ADL executions are exposed separately. They cannot establish the full effect of
  unsettled ADL/funding trackers. No adjustment is added to `realBalance` again.
- `--include-current-state` uses the existing public projection: separate REST
  observations, estimated marked PnL/effective collateral, explicitly not the
  exact matching-engine admission view. It is observed **now**, never at a past
  period boundary; no historical return is calculated from it.

## Coverage and unavailable metrics

Each feed carries requested interval, page count, cursor/time boundary and reason
for stopping. Transfers use the published cursor; older time-paginated histories
preserve inclusive timestamp boundaries and flag saturation rather than skip ties.
A page budget or failed feed is incomplete. Exhausting available history does not
prove retention is complete, even when all returned pages were successfully read.

The envelope supplies API deployment, wallet and observation timestamp. There is
no verified atomic watermark across feeds. Concurrent updates can produce a
partial view; re-reading is safe and does not create an accumulating duplicate ledger.
Symbol filtering omits transfers without a symbol, so its cashflow result is not
an account-wide total. Previously owned/transferred accounts are not discovered
by current-ownership checks and require explicit historical investigation.

`net_trading_return`, `equity_time_series` and `unrealized_pnl_time_series` are null.
No supported server history series is integrated; no synthetic pre-installation
history, linear interpolation, annualized return or deposit-adjusted profit is
fabricated. Paper `refresh` snapshots are separately labelled **local simulation**
and only exist after recording; they are not real account performance.

## Tester examples

1. Fund an account by 100 and withdraw 5 (using existing operator evidence).
   Observed external cashflow is +95, **not profit**.
2. Transfer 10 twice from owned account A to owned B. Account A observes −20
   cashflow and B +20. Separate transaction hashes can produce two balanced
   observation groups, without proving individual leg identity. Repeated equal
   transfers in one transaction stay ambiguous. Missing sides never imply failure.
3. A settled execution reports price PnL 25, funding −3, fee 2 and reported
   realized PnL 22: the API's realized PnL is price variation plus funding and
   excludes the fee. Preserve those fields separately; do not add all four or add
   a matching transfer fee again. If the next legacy record lacks funding/PnL,
   suppress sums if its event identity is unknown; otherwise mark missing components partial.
4. Compare `data state` with `history --include-current-state` using fresh reads.
   Neither should add funding/ADL trackers on top of public derived balances.
   Differences during concurrent activity need timestamps/source evidence, not
   an assertion that one historical sum proves current equity.

## Supported reads and open gaps

`GET /v2/wallet/{address}/{accounts,orderHistory,perpExecutions,executionBusts,transfers}`;
optional current projection adds `accountBalances`, `positions`, market summaries,
asset definitions and asset oracle prices. All are supported V2 route shapes from
the pinned SDK/off-chain contracts; environment deployment/retention can differ.
No new legacy route, RPC fallback or backend backfill service is introduced.

Remaining gaps: verified full-period retention; interval-boundary equity and
unrealized PnL series; exact pending ADL/funding attribution; engine admission
watermark; complete history for prior account ownership. These are explicit API
integration gaps, not permission to estimate missing metrics as zero.
