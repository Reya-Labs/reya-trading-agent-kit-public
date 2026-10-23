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

**Creation needs no owner signature or private key.** It sends allowlisted `createAccount(owner)` calldata to Reya's executor. The executor submits the transaction and pays gas. The kit verifies the returned receipt-derived IDs against the owner's `/v2/wallet/{address}/accounts` read. The owner's first creation can also create a spot account: only a newly created, owned perp account (`MAINPERP`, `SUBPERP` or `AGENTPERP`) from that relay response can be confirmed. A wallet difference alone is not sufficient evidence.

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

Prices and sizes are illustrative; inspect current market metadata and prices first. Supported trade operations are market/limit/TP/SL (`order tp` / `order sl` are allowed 15 seconds plus the position ceiling and one trigger write: 35 seconds on mainnet and 50 elsewhere), market entries with `--sl`/`--tp` (allowed 15 seconds plus the position ceiling, two trigger writes and the read-back instead of 15: 42 seconds on mainnet and 57 elsewhere, so the command is not cut off between the fill and the stop), cancellation, scoped cleanup (`positions close_all` is allowed 15 seconds plus the position ceiling, to wait for the session's own fills and for a position its TP/SL say is there: 30 seconds on mainnet and 45 elsewhere), reduce-only close, and GTC limit modification. For market/close commands, pass a `--slippage` within your policy (the CLI default 0.01 may exceed it). The kit's fat-finger guards (limit-price band, per-order notional cap, estimated-leverage limit) apply to every command; `--force`, `--allow_large`, abbreviated flags and help requests are not accepted inside the runner. No shell or arbitrary Python is executed from the input.

A modification changes a resting **GTC limit only**, retains its venue/client IDs and immutable side/type/TIF, and restates the total quantity, which must exceed already-filled quantity. It preserves the known maker flag unless you explicitly pass `--post_only` or `--no-post_only`; if readback omits that flag, you must specify it. Price/size changes can lose venue queue priority. Cancel can resolve a client ID to the owned account's open venue order ID. These two capabilities are also available through `trade` outside the runner.

`status` includes `orders` with type, buy/sell `order_side`, client ID, trigger/limit price, TIF, fill quantity, maker/reduce-only flags and trigger state. TP/SL include `position_side`, `close_side` and `quantity_mode: "whole_position"`. `open_order_ids` remains for existing consumers. The ambiguous order `side` alias is removed from readback; unknown maker flags remain null.

Immediately after an entry, the API may not yet show the position. `order tp` / `order sl` wait until the owned position appears with the requested side (a flip shows the old side first), within the indexing ceiling: 15 seconds on mainnet, 30 seconds elsewhere. From the CLI, and inside the runner while its WebSocket wallet state is unavailable, they re-read positions every 0.5 seconds (at most 31 reads on mainnet, 61 elsewhere); inside the runner with verified wallet state they check the feed's position at each update instead (see [latency and warm state](#latency-and-warm-state)). A mismatch, timeout or still-missing position fails without a protective write, after the full ceiling; inspect status and reconcile before retrying protection, never repeat the entry. `--force` remains unavailable. `order market --sl/--tp` instead waits for the position to include the entry's fill (a newer trade sequence number), until that evidence or the same ceiling, and reports `protection`; see [entry protection](command-reference.md#entry-protection). Neither can guarantee immediate protection or trigger fills.

Create commands support optional `--fees_wait_s 1` through `5`, including inside the runner, to poll execution reads for that total time after the one submission. Outside the runner the default `0` keeps one two-second read. Inside the runner the default does not delay the result: a fill's result has `fees_status: "deferred"`, and the read runs after the result is out and arrives as its own line, `{"event":"fees","request_id":"…","order_id":"…","fees_status":"…","fees_rusd":…,"executions":[…]}`. A read straight after a fill finds nothing, because the venue indexes an execution only after the fill settles (12–13 seconds on devnet1 on 19 October 2026). So the runner first waits for the fill: with warm state, until the execution feed shows it, which costs no request; without it, it re-reads at 1, 2, then 4-second intervals. It emits the event once the fees are complete, or after 30 seconds with what is known (reading REST then, in case the feed missed the fill). An order that has filled more since its acknowledgement ends the wait at once as `partial`, with all its executions. A failed re-read never drops executions an earlier read found. At shutdown it first waits for the warm feed to show the session's fills (part of the settlement budget below), then up to five seconds for outstanding reads, then emits each with `fees_status: "pending"`. It is also kept in the journal as `fee_reads`. Match it to the request by `request_id`; it never arrives before that request's result, and a later request's result can come first. Pending/partial/unavailable fees stay null and preserve the order/fill references. SDK errors now expose `error_code`, `message` and HTTP status without response headers. Readback schema and fee/pagination semantics are described in [the command reference](command-reference.md#settled-executions-and-fees).

### Latency and warm state

An order costs about one round trip to the venue. Before the send, the runner reads nothing over the network while its warm state is within its bounds:

- **Market prices.** Each policy symbol's summary comes from the public WebSocket (`ws.reya.xyz`, market and wallet data only; it carries no orders and needs no IP allowlisting). A mark is used only if its channel pushed within the last second; the venue pushes every 500 ms. The budget's own staleness check still applies.
- **Balances, positions, open orders and fills.** These come from the same socket's wallet channels. Each channel sends a snapshot on subscribe, then every change; the venue closes the socket to resubscribe rather than drop an update. The runner uses them only while the socket is connected, every channel has its snapshot, and REST reads made after that snapshot matched it. It re-checks them against REST every 60 seconds and resubscribes after a mismatch.
- **The runner's own fills.** A fill counts in the position from its acknowledgement until the feed shows it included, matched by trade sequence number. The venue indexes a fill after it settles: about a second on mainnet (p99.9 2.1 seconds over six days to 19 October 2026), but 12–15 seconds in devnet1 episodes on 19 October. REST shows it no sooner than the feed, which leads it by about 0.5–0.8 seconds. The runner's waits for a fill end as soon as the feed shows it, and are bounded by an indexing ceiling: 15 seconds on mainnet, 30 seconds elsewhere. Until the feed shows it, the guards also debit an upper bound on its fee and any realized loss (never a gain); the debit stays until 10 seconds after the feed shows the fill, because the balance can update a little later.
  - When the runner cannot place a pending fill in the position, REST would leave it out as well, so an order that needs the account checks is refused before sending (`no order submitted`). That happens when the position changed and the runner cannot tell whether the change included the fill, when the socket dropped or resubscribed (the new snapshot may or may not include it), or without a fee bound or a fresh mark. The session continues and there is nothing to reconcile; retry once the venue shows the fill. A reduce-only `order market` close is still sent, as when the account read fails, and the engine decides.
  - While the socket is down or resubscribing, REST can end that refusal early. In the background the runner reads the account's positions and, until the fill's trade sequence number is known, the wallet's executions from just before the acknowledgement. A position whose `lastTradeSequenceNumber` is at or past the fill's sequence includes it, and from then on REST decides. These reads only read and never resend anything. Each is bounded to 2 seconds, they back off from 0.5 to 4 seconds apart, and they stop once the feed is verified again or nothing is left to prove. A fill that leaves no position (a close to flat) has no REST row to prove it, so it waits for the feed or the ceiling. Once REST decides, the fill's fee and realized-loss debit no longer applies (REST's balance is used as read), as when a fill is dropped at the ceiling.
  - Because the feed leads REST, a fill the feed has shown is not assumed to be in REST yet. If the socket drops or resubscribes within 5 seconds of the feed showing a fill, an order that needs the account checks is refused the same way until REST proves the fill or those 5 seconds pass.
  - A fill the feed has not shown within the indexing ceiling (busted, or missed by the feed) is dropped, the runner resubscribes, and REST decides, as before.
  - Each own fill's indexing lag is kept in the journal as `fill_lags`, one record per fill when it leaves the count: `order_id`, `symbol`, `sequence`, `resolved_by` (`feed`, `rest`, or `ceiling` when dropped unshown), `ack_to_resolved_s` (seconds from the acknowledgement), `trade_timestamp_ms` (the fill's block timestamp), `venue_lag_s` and `ceiling_s`. `venue_lag_s` is this computer's clock when the runner saw the fill, minus the block timestamp. Block timestamps are whole seconds and the clock can be offset, so it is good to about a second; for `rest` it is an upper bound, because REST is polled. A rehearsal's journal therefore shows how long the deployment took to index each fill, which the indexing ceiling must cover. No event carries these fields.
- **The runner's own resting orders.** An order acknowledged as resting counts in the open-order view from its acknowledgement until the order feed shows it. If the socket drops first and the order ends while it is down (filled, or cancelled, including with `POSITION_CLOSED`), the new snapshot never shows it, because it lists open orders only. So after a reconnect, once REST read after the new snapshot matches it, the orders acknowledged before that snapshot leave the view: neither the snapshot nor REST lists them. Orders acknowledged after it keep counting until the feed shows them.
- **Position evidence for TP/SL.** After an entry, `order market --sl/--tp` and `order sl` / `order tp` take the position from the wallet feed, checked at once and at each position update, instead of polling REST every 0.5 seconds. The evidence is the position exactly as the venue published it, never the runner's own acknowledged fill; like REST, it is built from settled trades, so a busted fill never appears in it. For `--sl/--tp` the feed must also have been verified just before the entry was sent, and the position must be newer than both the feed's view then and the position the checks before the entry saw. If the feed is unavailable or drops during the wait, REST reads take over for the rest of the same bound. The rules, bounds and results are those described above and in [entry protection](command-reference.md#entry-protection): until the evidence or the indexing ceiling above (15 seconds on mainnet, 30 elsewhere), for `order sl` / `order tp` and `--sl/--tp` alike.
- **TP/SL checks before an entry.** `order market --sl/--tp` takes the position and any armed TP/SL for its refusals (`protection_opposite_position`, `protection_exists`) from the warm state, read at one instant: positions with the runner's own acknowledged fills applied, and open orders including the resting orders it has acknowledged but the feed has not shown yet. So a fill or a trigger that REST does not show yet still counts against the entry. When that state is unavailable, the checks read REST; while an own fill cannot yet be placed in the position, the entry is refused instead, as above.
- **Entries without a stop.** An `order market` entry without `--sl/--tp` reports whether a stop-loss already armed on its market covers the position ([entries without a stop](command-reference.md#entries-without-a-stop)). Inside the runner it takes the open orders from the same warm state, after the send, with no request; when that state is unavailable it reads REST once. Its `protection.state` is `not_requested`, which never stops the session.
- **Trigger read-back.** After arming, `visible` per leg and `verified` come from the wallet's order feed, checked at once and at each order update, instead of REST open-order reads every 0.5 seconds. A trigger counts only once the feed shows it open and not fired; one the runner has acknowledged but the feed has not shown never counts, so `verified: true` still means the venue lists it. The bound stays 2 seconds; if the feed is unavailable or drops during it, REST reads (at most 5) take over for the rest.
- **Reference data and ownership.** Market and collateral definitions and fee tiers are read once, then refreshed in the background every 30 seconds. The owned-account check runs at startup only, because account owners do not change.

Anything outside these bounds is read over REST, as without warm state, in the same order as before. The ready event reports `warm_state` (`connected`, `wallet: verified|unavailable`, `marks`). A disconnected or unverified socket makes the session slower, never less checked: while one of its own fills is not shown yet, orders that need the account checks are refused rather than checked against REST, until REST proves the fill as above. `close_all`, `status`, shutdown cleanup and settlement always read REST.

`order cancel` and `order modify` look up the named order in the warm open-order view, which includes orders this session placed that the feed has not shown yet. If it is not there, or the identifier is ambiguous there, the account's open-order read decides, as before.

Latency is dominated by the distance to Reya's API servers in Europe. A plain request from East Asia takes about 0.3 seconds, from within Europe a few tens of milliseconds. To see what your connection gives, without sending anything:

```bash
python -m agent_kit.scripts.latency --symbol ETH --samples 5
```

It times one market-data request, and the same order through the runner with REST reads and with warm state. Reads go to the live API for `PERP_ACCOUNT_ID_1`; order entry is replaced by a local stub, orders are signed with a throwaway key, and writer state goes to a temporary directory. A real order adds one order round trip to each figure. On devnet from Europe, with order entry stubbed: a limit order took 402 ms with eight sequential reads before the send on 0.8.2, and 13 ms with none on warm state.

## Coordination, cancellation and recovery

All kit trading commands now use cooperative owner **and** signer locks, plus a persistent nonce counter. Two updated kit processes on the same machine/state directory cannot write concurrently for an overlapping owner/signer context. The runner holds those locks for its entire session and serializes order writes with cancellation heartbeats (the venue requires each signer's nonces to increase, so two writes never race). Account setup also locks the owner so funding cannot race the local runner.

This is host-local coordination. An existing MM bot, older kit checkout, another OS user, another state directory or another host does not automatically participate. Stop those writers or integrate them with a common writer service. Do not assume separate subaccounts isolate a shared signer.

Before accepting a command, the runner arms the venue's account-wide **30-second cancel-all-after timer**, refreshing every ten seconds (three renewals per countdown; an order waits behind a renewal at most one round trip). A failed renewal leaves the previous countdown running at the venue, so the runner retries it when the failure is transient: `SERVICE_UNAVAILABLE_ERROR` (the request never reached the matching engine), `RATE_LIMITED_ERROR`, `ORDER_OUTCOME_UNKNOWN_ERROR`, another server error (5xx), a timeout or a transport error. It waits one second, or the venue's `retry_after_ms` when longer, and signs a new request each time. A retry is sent only while it can still be confirmed before the last confirmed deadline, with its 5-second write timeout and a 2-second margin to spare; a failed attempt never moves that deadline. Orders go on while a retry waits. The runner halts at once on `EDGE_RATE_LIMITED` (the 60-second ban outlasts the countdown), when the venue does not confirm the countdown (account, duration or trigger time), and on any other error. `status`, the `stopped` event and the journal report `heartbeat`: `retries`, `last_retry` and `halted_by`, each with the error code, message and time. If a renewal cannot be confirmed in time, or the monotonic deadline since the last confirmed renewal (measured from before it was sent) expires, the runner halts new submissions and attempts immediate account-wide cancellation. Deadline checks occur before admitting input and before each write, including renewal, so a resumed/stalled process cannot silently rearm the timer and continue trading. A late refresh response cannot erase the gap. Such a session remains unreconciled even if final reads are flat; inspect what expired before acknowledging a restart. The venue timer is the fallback when this process or its connection disappears. When it fires it cancels every open order **except TP/SL** and does not close positions.

EOF, `stop`, idle/command limits, heartbeat failure, termination signals and caught errors cause cleanup. A user account-wide cancel (`cancelAll`) also clears armed TP/SL in the matching engine, unlike the timer's own fire. So the runner first reads positions and orders: if the account is visibly flat with no TP/SL, it sends one account-wide cancel; otherwise, including when either read fails, it cancels each non-TP/SL order individually and leaves TP/SL armed. In that case the timer stays armed and sweeps anything the reads missed, still keeping TP/SL. The `stopped` event and journal record `cleanup` (`mode`, `kept_order_ids`, `cancelled_order_ids`, `errors`) and the event reports `protection`: for each remaining position, the stop-loss and take-profit still armed for it, or `null`. The runner then reads back orders/positions. It also persists each SDK order acknowledgement before optional reads. Settlement evidence exists only once the venue has indexed the session's fills, seconds after they settle. So within the indexing ceiling (15 seconds on mainnet, 30 elsewhere) the runner first waits for its warm feed to show its own fills, which costs no request, and then makes bounded V2 order-history/execution/bust observations, backing off 0.5, 1, 2, then 4 seconds apart, for what remains of the budget (at least 2 seconds). The first definitive answer ends them. A `stop` straight after a fill therefore takes about as long as the venue takes to index it. An OPEN acknowledgement requires terminal order history because it can fill later. Every matched quantity must have exact scoped settled-fill or bust evidence; the net settled quantity from the initially flat, exclusively used account must be zero. Only this positive evidence plus a clean, flat final read disarms the timer and reports `reconciled: true`. Empty reads after FILLED are insufficient. Otherwise it exits with an error, reports remaining/unknown exposure (naming any position left without a stop) and leaves the timer potentially armed. When settlement has not confirmed an acknowledged fill, the `stopped` event and the journal list it in `unconfirmed_fills` (`symbol`, `order_id`, `side` `B`/`A`, `filled_qty`), and the error names it: the final position reads can leave out a fill the venue has not indexed, so they can look flat while the account holds it. Inspect this state before placing new manual orders: non-protective orders can be cancelled until the timer expires. Closing a position must be an explicit command; shutdown never promises to flatten it.

Writer state lives in `~/.local/state/reya-agent-kit` (0700), or an absolute `REYA_STATE_DIR` shared by all participating kit checkouts. Files are 0600. The ready event gives the current journal path. It stores public command intent/outcomes, session ID and nonce state; it never stores keys or signed transactions. Do not delete/change the nonce files to bypass a lock or reset a counter.

Requests are recorded before submission and never replayed automatically. Ambiguous write results stop admission: a timeout, a transport error, a server error (5xx), an `UNKNOWN` status, an error code not listed below, or any failure in a command that wrote more than once (`close_all`, `cancel_all`, an entry with `--sl/--tp`).

A venue verdict on one write that changed nothing, or a terminal acknowledgement, leaves nothing to reconcile. The session continues and the result carries `session_continues: true`. The request is never retried and its budget stays charged:

- `RATE_LIMITED_ERROR` (HTTP 400; `retry_after_ms` when the venue sends it). The venue limits orders per account; back off before the next write.
- `POST_ONLY_WOULD_CROSS_ERROR` (HTTP 400).
- `SERVICE_UNAVAILABLE_ERROR` (HTTP 400): a dependency or the engine queue was unavailable, so the request never reached the matching engine. Back off before resubmitting; the kit signs a new nonce.
- `ORDER_NOT_FOUND_ERROR` on a cancel or modify, and `MODIFY_QTY_BELOW_FILLED_ERROR` on a modify (HTTP 400): the order had already filled or been cancelled.
- `REDUCE_ONLY_CONDITION_NOT_MET_ERROR` on `order market --reduce_only` (HTTP 400): the engine counts an opening fill only once it has settled, so a close sent right after the fill is refused. On devnet1 a close sent 7 ms after the fill's acknowledgement was refused, and the same close 12 seconds later filled. Wait for `status` to show the position before closing again.
- `EDGE_RATE_LIMITED` (HTTP 429): Reya's edge refused the request before the API. Pause all requests for at least 60 seconds.
- A market, limit or modify acknowledged as `CANCELLED`, for example an IOC that filled part (`filled_qty`) and cancelled the rest, or a post-only limit that would have crossed. The fill quantities in the result are what happened. An unfinished session blocks the next runner, even when reads appear flat. Inspect its journal, order/execution history, balances and positions; allow outstanding requests/countdowns to settle. Only after operator reconciliation can you acknowledge that exact session ID with `--ack-previous-session SESSION_ID`. The next session still requires a flat, empty account and never replays old requests. The state retains the most recent session, not a complete historical audit archive.

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
