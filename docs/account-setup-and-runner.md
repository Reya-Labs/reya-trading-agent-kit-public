# Account setup and a coordinated trading session

The agent kit must use supported Reya APIs for account setup and trading. Do not introduce direct RPC dependencies, transaction broadcasting or user-managed gas requirements. Missing SDK support is an API integration gap, not justification for an RPC fallback.

Account setup uses the allowlisted `executeReya` API relay. The runner accepts trading commands from an agent and manages their execution; it does not choose a strategy. Both flows preview by default. Choose the account, amounts and trading policy before enabling execution. Mainnet uses real funds.

For private installation and explicit environment selection, follow
[first use](private-first-use.md). The [V2 data and reconciliation contract](v2-data-and-reconciliation.md)
describes source semantics and gaps. The relay POST and Core nonce GET are the
only non-V2 dependencies, kept under the exception agreed on 12 October 2026 for
account setup only; it is not a blanket V2 exception. The separate [paper simulator](paper-trading.md) is available for virtual funds;
it does not simulate account setup or run the live runner. See also
[history and performance](history-and-performance.md) for read-only reconciliation.

> **Traders: use `agent init` instead of this section.** The guided setup ([command reference](command-reference.md#agent-setup-agentpy)) creates and funds the dedicated agent account through the app with the owner's own wallet, and the kit signs with an app-approved agent key. The `account` commands below are the own-key path (`REYA_SIGNER=own-key`): they need a wallet key file on this machine and are kept for operators and dedicated-wallet setups.

## 1. Create and fund a dedicated account

Configure the API deployment and owner as in [the mainnet guide](mainnet.md) or `.env.example`. No RPC endpoint or gas budget is required. Core, Periphery and rUSD addresses used in the relay payload are selected by the exact API deployment, since devnet1 and Cronos share a chain ID. Custom deployments fail closed until their supported API configuration is integrated.

**Creation needs no owner signature or private key.** It sends allowlisted `createAccount(owner)` calldata to Reya's executor. The executor submits the transaction and pays gas. The kit verifies the returned receipt-derived IDs against the owner's `/v2/wallet/{address}/accounts` read. The owner's first creation can also create a spot account: only a newly created, owned `MAINPERP`/`SUBPERP` account from that relay response can be confirmed. A wallet difference alone is not sufficient evidence.

**Transfers sign an authorization, not an Ethereum transaction.** They require `REYA_ACCOUNT_OWNER_KEY_FILE`: an absolute path to an owner-only regular file (0600/0400), provisioned outside the checkout. The signer must match `PERP_WALLET_ADDRESS_1`; this flow does not support contract/Safe owner signatures or assume a trading delegate has transfer permission. The kit reads `coreSigNonce` from `/api/owner/{address}/metadata`, signs the next nonce and a 60-second deadline using Core's `ExecuteBySig` schema, and sends the corresponding `transferFromMAToMA` calldata to the relay. Reya's executor submits it and pays gas. Preview, creation and status never read a key.

```bash
# Discover existing accounts; choose the source yourself.
python -m agent_kit.scripts.query accounts

# Preview, then relay unsigned creation with a stable ID for this intent.
python -m agent_kit.scripts.account create
python -m agent_kit.scripts.account create --execute --request-id strategy-account-1

# Use only the account_id from a confirmed result.
python -m agent_kit.scripts.account transfer --from-account SOURCE_ID --to-account NEW_ID --amount AMOUNT_RUSD
python -m agent_kit.scripts.account transfer --from-account SOURCE_ID --to-account NEW_ID --amount AMOUNT_RUSD --execute --request-id strategy-funding-1

# Reconcile using reads; never submit the same intent with a fresh ID.
python -m agent_kit.scripts.account status
python -m agent_kit.scripts.account status --request-id strategy-funding-1
```

Transfers require two distinct margin accounts in the configured owner's account read and a positive amount represented in exact rUSD units (six decimals). Preview validates those inputs and reads the nonce; it is not a contract simulation or a reservation of collateral. The contract applies its current access and margin rules at execution. This flow does not bridge funds, approve allowances or grant trading permissions.

Transfer confirmation requires a matching debit and credit in `/v2/wallet/{address}/transfers`: the same transaction hash, source/destination accounts, RUSD asset and exact signed amounts, with exactly one debit and one credit after available cursor pages are traversed. Sequence IDs are opaque: parity or adjacency is never used to infer a transfer leg. Balance differences, nonce advancement and a transaction hash alone do not establish success. Reads walk at most ten cursor pages (100 entries each); missing entries or indexing lag leave the result unknown. `status` can be repeated to retry reads without signing or posting again. `confirmed` reports the supported APIs' indexed/receipt evidence, not independent chain finality against later reorgs.

Without `--request-id`, `status` also includes `pending_requests`: every locally unresolved request with its action, API deployment, saved status and transaction hash. These are local journal summaries; only the selected pending/latest request is reconciled during that call. Reconcile others explicitly with their request IDs. No additional POST is made.

### Duplicate protection and unknown outcomes

`--request-id` is required for execution. Reuse it for the same intent, including after a crash or timeout. The journal is persisted before the single relay POST, and the relay hash is saved before follow-up reads. Reusing a completed ID returns its stored outcome; reusing an ID with different parameters is rejected. Unknown attempts block further setup writes for that owner, even with a new ID. Completed IDs remain in the journal to prevent repeat submission; this is local protection, not server-side idempotency across independent machines or state directories.

If a transfer's response was lost and an operator recovers its actual hash independently, provide it for exact ledger verification:

```bash
python -m agent_kit.scripts.account status --request-id strategy-funding-1 --tx-hash ACTUAL_TRANSACTION_HASH
```

#### Confirmation by unique observation

When the relay's reply is lost, `account status` can confirm the request from supported reads alone if exactly one matching operation appeared after it. For a transfer, that is one debit/credit pair in the transfer ledger with the same accounts and exact amount, timestamped after the request was journalled. For a create, it is one new margin account in the owner's account list compared with the list saved before the request. The result is `confirmed` with `verification: "unique_observation_after_request"`, which is weaker evidence than a relay hash (`transaction_hash_and_exact_owned_transfer_ledger_pair`).

Limitations:

- **Another client can be mistaken for this request.** An identical operation by another client, process or the web app after the request is indistinguishable: the same accounts and amount for a transfer, or any new margin account for this owner for a create. Balances end up as intended, but the confirmation may describe someone else's operation. Use a dedicated owner wallet with one writer if that matters.
- **It only ever confirms.** No observation means the request stays `unknown`. It never concludes that a request did not execute, so it cannot cause a repeated submission.
- **Ambiguity stays unknown.** That includes two matching transfers after the request; a match already recorded for another request; another unresolved request with the same parameters (or, for creates, any other unresolved create); and journal records written by older kit versions without `created_at_ms`.
- **Bounded scan.** The ledger walk stops after ten pages. If it cannot show that it covered every entry since the request, the request stays unknown.
- **Clock allowance.** Ledger timestamps are block times, so entries up to two seconds before the request count as after it. An identical transfer in that window would still make the match ambiguous or wrong.
- **Lost replies only.** When the relay did reply, hash-based verification or the relay's own error is used instead.

A future option for transfers: each signed authorization carries a Core nonce and a 60-second deadline. Once the deadline has passed and the owner's nonce has not been consumed, the transfer provably never executed and could be closed as such. This is not implemented.

A supplied hash is saved only if it verifies this request's exact debit/credit pair; an unverified hash leaves the request unknown and does not block the correct one. A hash already recorded for another request in this journal is refused, so one transfer cannot confirm two requests. Never guess a hash or delete/reset journal state to bypass uncertainty. Creation cannot be recovered from a hash alone with the current reads. A crash before POST can therefore leave an intent blocked even if nothing was sent. Unknown, generic server errors remain blocked; only an explicit relay `TRANSACTION_FAILED_TO_SEND` outcome or a hash-bound `TRANSACTION_REVERTED` result is recorded as terminal failure. A terminal request ID is never posted again. Any new attempt needs a deliberately new ID. Old pre-relay pending-transaction markers also block migration rather than being silently discarded.

### API gaps and endpoint migration

- The pinned Python SDK lacks account relay and Core owner-nonce wrappers. It has V2 transfer reads; the isolated setup adapter preserves exact raw response validation. `_account_relay.py` integrates the supported HTTP APIs directly, with local ABI encoding/EIP-712 signing, no provider and no transaction broadcasting.
- The relay has no client idempotency key or request-status lookup. After losing a response, the kit cannot prove nonexecution or automatically recover the transaction hash. Creation also lacks a supported creation-by-transaction lookup. Such cases require operator reconciliation/API support; they never trigger a fallback write.
- Owner nonce and account ID reads use JavaScript numbers. Values outside the exact integer range are refused until lossless API representations are supported.
- Transfer reads can be delayed, unavailable on a deployment or outside the bounded page window. The kit stays unknown; it does not substitute balance arithmetic or RPC reads.
- Dedicated account-operation endpoints were not yet available on 7 October 2026. Once their deployment and request/response contracts are verified, migrate the isolated relay adapter and its contract tests to those endpoints. Do not probe writes for availability or retry an ambiguous `executeReya` attempt on a new route. A retired/unavailable relay fails closed.

After funding, set `PERP_ACCOUNT_ID_1` to the confirmed ID and inspect balances, positions and orders. Use the owner as trading signer or establish delegated trading permission through your existing workflow. Setup does not update `.env` or start a runner.

## 2. Preview a runner policy

Copy [the example policy](../examples/runner/policy.json) and choose your own limits. All six fields are required:

| Field | Meaning |
| --- | --- |
| `symbols` | Only these perpetual markets can receive new orders |
| `max_order_notional` | Maximum notional for each exposure-increasing order or modification |
| `max_session_notional` | Cumulative submission budget for the session |
| `max_slippage` | Maximum CLI slippage fraction, at most 0.05 |
| `max_commands` | Total requests admitted, including status; 1–1000 |
| `idle_timeout_s` | Wait this long for each next input, excluding command execution time; 10–3600 seconds |

```bash
python -m agent_kit.scripts.runner --policy examples/runner/policy.json
```

This reads account state without a key and reports the proposed scope. Live startup requires that the owned account has **no positions and no open orders**. Dedicate it to this runner: a flat snapshot does not prevent another, uncoordinated strategy from using it later.

Budget is charged at `qty * max(submitted limit, current mark)` before submission, using a price snapshot no older than 30 seconds (up to five seconds of clock skew accepted). Each modification charges its full intended total size again. Cancellation, partial fills and failed submissions do not refund budget: lagging data cannot manufacture new capacity. Reduce-only closes and protective TP/SL do not consume the exposure-increasing budget; reduce-only closes can exceed the per-order cap so they can unwind accumulated exposure. Slippage and price availability checks still apply.

These are **submission limits**, not a maximum-loss guarantee, a mark-to-market exposure limit or a liquidation safeguard. Fees, funding, price moves and outside writers remain separate. A new, reconciled session gets a new budget. Direct CLI commands outside a runner do not inherit its policy.

## 3. Run one persistent trading session

The runner signs with the same key as every other trade command: by default the agent key from `agent init` (`PERP_AGENT_PRIVATE_KEY`), which trades only the recorded Agent account; with `REYA_SIGNER=own-key`, `PERP_PRIVATE_KEY_FILE` or `PERP_PRIVATE_KEY_1`. The setup owner key is not used by the runner.

```bash
python -m agent_kit.scripts.runner --policy examples/runner/policy.json --execute --ack-account-wide-cancel
```

Keep stdin open while running. Stdin must be a pipe or terminal. Use `cat commands.jsonl | python -m agent_kit.scripts.runner ...` for a command file; direct file and `/dev/null` redirection are rejected as JSON errors before loading a signer or arming the timer. The program emits one JSON object per line after startup. Wait for `event: ready`, then send requests with unique IDs and ordinary trade arguments:

```json
{"id":"quote-1","command":["order","limit","ETHRUSDPERP","--side","long","--qty","0.01","--price","1900","--post_only","--client_order_id","101"]}
{"id":"reprice-1","command":["order","modify","--client_order_id","101","--price","1899","--qty","0.01"]}
{"id":"cancel-1","command":["order","cancel","--client_order_id","101"]}
{"id":"check-1","command":["status"]}
{"id":"stop-1","command":["stop"]}
```

Prices and sizes are illustrative; inspect current market metadata and prices first. Supported trade operations are market/limit/TP/SL, market entries with `--sl`/`--tp` (allowed up to 30 seconds instead of 15, so the command is not cut off between the fill and the stop), cancellation, scoped cleanup, reduce-only close, and GTC limit modification. For market/close commands, pass a `--slippage` within your policy (the CLI default 0.01 may exceed it). The kit's fat-finger guards (limit-price band, per-order notional cap, estimated-leverage limit) apply to every command; `--force`, `--allow_large`, abbreviated flags and help requests are not accepted inside the runner. No shell or arbitrary Python is executed from the input.

A modification changes a resting **GTC limit only**, retains its venue/client IDs and immutable side/type/TIF, and restates the total quantity, which must exceed already-filled quantity. It preserves the known maker flag unless you explicitly pass `--post_only` or `--no-post_only`; if readback omits that flag, you must specify it. Price/size changes can lose venue queue priority. Cancel can resolve a client ID to the owned account's open venue order ID. These two capabilities are also available through `trade` outside the runner.

`status` includes `orders` with type, buy/sell `order_side`, client ID, trigger/limit price, TIF, fill quantity, maker/reduce-only flags and trigger state. TP/SL include `position_side`, `close_side` and `quantity_mode: "whole_position"`. `open_order_ids` remains for existing consumers. The ambiguous order `side` alias is removed from readback; unknown maker flags remain null.

Immediately after an entry, the API may not yet show the position. TP/SL re-read positions every 0.5 seconds, up to seven reads within four seconds, inside the runner and from the CLI alike, until the owned position appears with the requested side (a flip shows the old side first). A mismatch, timeout or still-missing position fails without a protective write; inspect status and reconcile before retrying protection, never repeat the entry. `--force` remains unavailable. `order market --sl/--tp` instead waits for the position to include the entry's fill (a newer trade sequence number) and reports `protection`; see [entry protection](command-reference.md#entry-protection). Neither can guarantee immediate protection or trigger fills.

Create commands support optional `--fees_wait_s 1` through `5`, including inside the runner, to poll execution reads for that total time after the one submission. The default `0` keeps one two-second read. Pending/partial/unavailable fees stay null and preserve the order/fill references. SDK errors now expose `error_code`, `message` and HTTP status without response headers. Readback schema and fee/pagination semantics are described in [the command reference](command-reference.md#settled-executions-and-fees).

## Coordination, cancellation and recovery

All kit trading commands now use cooperative owner **and** signer locks, plus a persistent nonce counter. Two updated kit processes on the same machine/state directory cannot write concurrently for an overlapping owner/signer context. The runner holds those locks for its entire session and serializes order writes with cancellation heartbeats. Account setup also locks the owner so funding cannot race the local runner.

This is host-local coordination. An existing MM bot, older kit checkout, another OS user, another state directory or another host does not automatically participate. Stop those writers or integrate them with a common writer service. Do not assume separate subaccounts isolate a shared signer.

Before accepting a command, the runner arms the venue's account-wide **30-second cancel-all-after timer**, refreshing every five seconds. If refresh fails or the monotonic deadline since the last refresh attempt expires, it halts new submissions and attempts immediate account-wide cancellation. Deadline checks occur before admitting input and before each write, including renewal, so a resumed/stalled process cannot silently rearm the timer and continue trading. A late refresh response cannot erase the gap. Such a session remains unreconciled even if final reads are flat; inspect what expired before acknowledging a restart. The venue timer is the fallback when this process or its connection disappears. When it fires it cancels every open order **except TP/SL** and does not close positions.

EOF, `stop`, idle/command limits, heartbeat failure, termination signals and caught errors cause cleanup. A user account-wide cancel (`cancelAll`) also clears armed TP/SL in the matching engine, unlike the timer's own fire. So the runner first reads positions and orders: if the account is visibly flat with no TP/SL, it sends one account-wide cancel; otherwise, including when either read fails, it cancels each non-TP/SL order individually and leaves TP/SL armed. In that case the timer stays armed and sweeps anything the reads missed, still keeping TP/SL. The `stopped` event and journal record `cleanup` (`mode`, `kept_order_ids`, `cancelled_order_ids`, `errors`) and the event reports `protection`: for each remaining position, the stop-loss and take-profit still armed for it, or `null`. The runner then reads back orders/positions. It also persists each SDK order acknowledgement before optional reads, then makes up to three bounded V2 order-history/execution/bust observations (six-second total budget). An OPEN acknowledgement requires terminal order history because it can fill later. Every matched quantity must have exact scoped settled-fill or bust evidence; the net settled quantity from the initially flat, exclusively used account must be zero. Only this positive evidence plus a clean, flat final read disarms the timer and reports `reconciled: true`. Empty reads after FILLED are insufficient. Otherwise it exits with an error, reports remaining/unknown exposure (naming any position left without a stop) and leaves the timer potentially armed. Inspect this state before placing new manual orders: non-protective orders can be cancelled until the timer expires. Closing a position must be an explicit command; shutdown never promises to flatten it.

Writer state lives in `~/.local/state/reya-agent-kit` (0700), or an absolute `REYA_STATE_DIR` shared by all participating kit checkouts. Files are 0600. The ready event gives the current journal path. It stores public command intent/outcomes, session ID and nonce state; it never stores keys or signed transactions. Do not delete/change the nonce files to bypass a lock or reset a counter.

Requests are recorded before submission and never replayed automatically. Ambiguous/partial/error write results stop admission. An unfinished session blocks the next runner, even when reads appear flat. Inspect its journal, order/execution history, balances and positions; allow outstanding requests/countdowns to settle. Only after operator reconciliation can you acknowledge that exact session ID with `--ack-previous-session SESSION_ID`. The next session still requires a flat, empty account and never replays old requests. The state retains the most recent session, not a complete historical audit archive.

## Validation boundary

Offline tests exercise ABI/EIP-712 compatibility against independent ethers vectors, unsigned creation, supported API outcome verification, SDK trading signatures, cross-process locks, persisted nonces, budget admission, heartbeat failure, uncertain outcomes and recovery gates. CI checks both new entry points through the installed package on Python 3.12/3.13.

No account, collateral or trading writes were executed while implementing this feature. Before production use, an operator should rehearse create/fund on devnet1, verify the returned account and balance, then test the runner on an isolated account with small chosen limits. Include a stopped-process/network-loss test proving venue cancellation, a deliberately exhausted budget, a graceful flat exit and a reconciliation case. The earlier mainnet canary does not establish live acceptance of these new paths.

## Stabilization rehearsal

Recheck this PR's exact commit on devnet before a small, authorized mainnet rehearsal:

- After an entry, submit TP/SL while delaying position publication. Expect one protective submission after a matching read, or a clear refusal with no write when reads remain missing, mismatched or unavailable.
- With a flat dedicated account and resting order, pause beyond 30 seconds. On resume, expect `heartbeat_failed`, immediate cleanup and an unreconciled journal; no new trade or countdown rearm. Verify venue cancellation independently.
- Set 10-second idle and delay a command for 12 seconds while the next request is buffered. It should accept that input; a genuinely idle input wait should still time out. Verify full protective-order details through runner status.
- Check `/dev/null` yields one JSON error without an asyncio traceback, structured venue rejections omit headers, post-only readback reports the API flag, and optional fee polling sends exactly one create.

An operator reported devnet setup, runner, process-loss cancellation and TP/SL firing in an earlier devnet run. Mainnet creation passed; owner-authorized transfer and the funded dedicated-account runner remain untested. These reports do not validate the new stabilization changes.

### Settlement evidence and remaining API limits

The journal's `acknowledged_orders` survives interruption; `settlement` explains
resolved orders/fill IDs, quantities and remaining uncertainty. Capped history,
missing stable fill IDs, conflicts, delayed fills or delayed closes keep the
restart gate closed. Polling never repeats an order. A fully busted entry can
be flat; a busted close leaves the settled entry exposed. Fee availability is
separate from settlement evidence. No automatic reset or automatic acknowledgement
of a previous session is performed.

Supported reads still lack a shared atomic watermark for engine exposure and
indexed account state. This check covers known session orders on an exclusively
used, initially flat account; it cannot certify external writers or unknown prior
orders. Those require operator reconciliation. If history has aged outside the
bounded page, use the documented read-only history tools and manually inspect
the exact journal before acknowledging its session ID.

The bracketed example sends one `order market --sl --tp`; when no stop-loss is
armed it requires positive settled-entry evidence and a visible position (bounded
re-reads) before its cleanup. Unknown entries stop for manual
reconciliation without blind close/cancel calls; close acknowledgements require
settlement evidence before being treated as flat. It remains an example, not a
durable hosted supervisor. The local runner keeps TP/SL on exit while a
position is open; this is separate from proposed hosted stop semantics.
