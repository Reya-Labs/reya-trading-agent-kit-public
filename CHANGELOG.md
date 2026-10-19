# Changelog

## 0.10.4 — 2026-10-19

From the first live rehearsal of 0.9.0 on devnet1 (an agent account, 0.01 ETH orders):

- **The runner's `fees` event waits for the fill to be indexed.** The deferred read ran about 100 ms after the result, while the venue indexed the executions 12–13 s after the fill (they settled on chain within about a second). Every `fees` event therefore said `pending` with null fees. The runner now waits before it reads: with warm state, until the execution feed shows the whole fill, which costs no request; without it, it re-reads at 1, 2, then 4 s. The event goes out once the fees are complete, or after 30 s with what is known (reading REST then, in case the feed missed the fill). A resting order that filled again since its acknowledgement ends the wait at once as `partial`, and a failed re-read keeps the executions an earlier read found. At shutdown outstanding reads still emit, as `pending`, after the existing five-second drain. So a `stop` soon after a fill can take up to five seconds longer, and the settlement reads that follow get that time too. The result still says `fees_status: "deferred"`, and `--fees_wait_s` still waits inline.
- **A reduce-only close refused for lack of a settled position no longer ends the session.** The engine counts an opening fill only once it has settled, so `order market --reduce_only` sent right after a fill is refused with `REDUCE_ONLY_CONDITION_NOT_MET_ERROR`. Live, the refusal came 7 ms after the fill's acknowledgement, and the same close 12 s later filled. Nothing is created, so the result now carries `session_continues: true` like the other definitive verdicts. It is not retried. Before, it stopped the session for reconciliation.

Measured live on devnet1 from Europe, on warm state with no reads before the send, from request to result:

| Order | Time |
| --- | --- |
| Post-only limit, rested | 39–60 ms |
| Post-only limit, refused | 56 ms |
| IOC fill | 122–137 ms |
| Modify | 54 ms |
| Cancel | 53–86 ms |

The order call itself took 29–48 ms for a resting limit and 114–127 ms for an IOC fill; the kit added 7–14 ms. Not exercised live: the two fixes above, which are tested offline.

## 0.10.3 — 2026-10-19

`order market --sl/--tp` inside the runner without its last two REST reads.

- **Trigger read-back from the WebSocket.** After the stop-loss and take-profit are acknowledged, `visible` per leg and `verified` come from the runner's verified order feed (`orderChanges`) instead of REST open-order reads (up to 5 within 2 s, 0.5 s apart). The kit checks at once and again at each order update, so the result returns as soon as the feed shows both armed.
  - A trigger counts only when the feed shows it open and not fired, as the REST read-back counts it. An order the runner has acknowledged but the feed has not shown never counts, so `verified: true` still means the venue lists both triggers.
  - When the feed is unverified, or drops or is invalidated during the wait, REST reads take over for the rest of the same 2 s. A trigger the feed already showed armed stays visible.
  - The venue documents armed stops as `OPEN` with `triggered: false` on `walletOrderChanges` and `openOrders` alike; both come from the matching engine's order stream.
- **The `--sl/--tp` checks before the entry use the warm state.** `protection_opposite_position` and `protection_exists` take the position and the armed TP/SL from the runner's verified wallet state, read at one instant, instead of a REST read of positions and open orders. That view counts what REST cannot show yet: the runner's own acknowledged fills (after a sell that flipped a long to a short, a long entry with `--sl` is refused as opposite) and its acknowledged resting orders (a stop-loss armed by the previous command is refused as existing). When the state is unavailable, including while an own fill cannot yet be placed in the position, the checks read REST as before. Fills and orders made elsewhere reach the feed from the same venue streams REST reads, and the feed is checked against REST every 60 s, as for the order guards since 0.9.0.
- **Unchanged rules and bounds.** The entry is never resent and no trigger is retried. The stop-loss goes first, then the take-profit. Position evidence and its 5 s bound are those of 0.10.2. The read-back bound stays 2 s, with the same `protected`, `partial` and `unprotected` results and messages; an unverified read-back still says to check `query orders`. Outside the runner nothing changes.

Not exercised live: no orders were placed for this release. Offline tests run the runner against a fake WebSocket.

## 0.10.2 — 2026-10-19

TP/SL straight after an entry, inside the runner, without polling REST.

- **Position evidence from the WebSocket.** Inside the runner, `order market --sl/--tp` and `order sl` / `order tp` take the position from the runner's verified wallet feed instead of reading REST positions every 0.5 s. They check it at once and again at each position update, so the stop-loss is sent as soon as the feed shows the fill. Before, they made at least one REST read and usually several, because REST positions trail a fill by about a second.
  - The feed is used only while its wallet state is verified (connected, snapshotted and matched against REST; see 0.9.0). For `order market --sl/--tp` it must also have been verified just before the entry was sent, and the position must be newer than the feed's own view at that moment as well as the REST read for the plan.
  - When the feed is unverified, or drops or is invalidated during the wait, REST reads take over for the rest of the same bound. Outside the runner nothing changes.
  - The evidence is the position as the venue published it, never the runner's own acknowledged fill. Like REST positions, the feed's are built from settled trades (each carries its transaction), so a busted fill appears in neither.
- **Unchanged rules and bounds.** The entry is never resent and no trigger is retried. The position must show the entry's side with a newer `lastTradeSequenceNumber` than before the entry. The stop-loss goes first, then the take-profit. The bounds stay 5 s after an entry and 4 s for `order sl` / `order tp`, with the same `unprotected` and `partial` results and messages. A feed that stays verified for the whole bound without showing the fill places nothing and reports the position UNPROTECTED, as REST reads that never show it do.
- **Still REST:** the `--sl/--tp` checks before the entry (the position and any armed TP/SL) and the read-back of armed triggers.

Not exercised live: no orders were placed for this release. Offline tests run the runner against a fake WebSocket.

## 0.10.0 — 2026-10-19

Paper trading for the rehearsals that matter, from two tester reports (kit 0.5.1, WSL Ubuntu, mainnet data): the simulator rejected the live book as "stale or in the future" when Reya's timestamp was about 1 s ahead of the local clock, and it modelled only 1x, so a leveraged order could not be dry-run.

- **Clock skew.** Paper accepts book and mark timestamps up to 5 s ahead of this computer's clock, and up to 30 s old: the runner's band. It accepted none ahead before. Every paper command that reads V2 reports `clock`: this computer's clock minus Reya's, from the response's HTTP `Date` header (`offset_ms`, `uncertainty_ms`, about ±0.5 s), with no extra request; from 2 s it adds `clock_warning`. Data outside the band refuses the command, ledger unchanged, with the skew in seconds, which way, and the fix (on WSL `sudo hwclock -s` or `wsl --shutdown`). Old data is blamed on the clock only when the server's clock says it is fresh; otherwise the error says the market data has not changed for that long. There is no flag to widen the band: the runner refuses the same skew.
- **Engine margin replaces the 1x rule.** Paper admits orders with the matching engine's checks, ported in 0.8.0: health ≥ 0 before, initial margin before and after, and the 1% settlement reserve after, on the order projected as fully filled at its limit price. The real balance is the ledger's virtual rUSD (no haircut) plus unsettled funding. Reduce-only orders owe the first check only. A refused order exits 1 with the engine's message, `error_code`, `refused_by: [{"check": "engine_margin"}]` and the `margin` block (`shortfall_usd`, `max_qty`); nothing is simulated and the request ID stays unused. The 0.8.0 engine vectors hold in paper: on a 25x market with 1,000 rUSD at mark 100, 245 is admitted, 247 fails the reserve and 250 initial margin. The tester's 0.8895 SPY on 34 USDC is refused with the same `margin` block the live preview builds.
- **Every order result carries the live preview's `margin` block** (`engine_check`, usage and liquidation price after a full fill at the limit, reserve, `max_qty`) and `account_after`: equity, margin figures and the position with its liquidation price after the simulated fill.
- **`--max_size` and `--usage U`** size paper orders as live does, with the default 0.2% headroom. Paper has no live order-size guards, and `REYA_MARGIN_HEADROOM` is ignored like the rest of the live configuration.
- **Funding.** Each position remembers V2's cumulative funding value (`longFundingValue` / `shortFundingValue`) at its last fill. Funding since then is unsettled, counts in equity and margin as a credit or a debit, and is settled into cash at the next fill on that market, as Core does. `funding` is now the settled total (it was always `null`), `unsettled_funding` the rest; results add `funding_settled`.
- **Liquidation.** Every command that reads marks (`state`, `refresh`, `order`, `order sl|tp`) is an observation. When equity is below the liquidation margin, paper closes every position at the observed marks, gives the remaining balance to the pool (`penalty_to_pool`) and sets cash to 0, never below (an insolvent deficit is reported as `uncovered_loss`). This is the backstop outcome, the worst case: live usually liquidates part of the exposure first. History gets a `liquidation` event. An `order` that finds the account liquidatable returns `status: not_simulated` with the liquidation in `events` and exits 1.
- **TP/SL in paper.** `order market --sl/--tp [--trigger_slippage]`, `order sl|tp <symbol> --side --trigger_px --limit_px --time_in_force IOC [--force]` and `order cancel_all --symbol S | --all`, parsed, rounded and refused as live (`protection_exists`, `protection_fires_at_once`, `protection_opposite_position`). Whole position, one of each per market. At each observation, before the liquidation check, a trigger the mark has reached fires an IOC child for the whole position against the current book at its limit; stop-loss first. A stop beyond the position's liquidation price loses to the liquidation. A fired trigger is used up even if its child fills partly; a flat or flipped position drops its triggers. History gets `stop_loss` / `take_profit` events, and results list what happened at the observation in `events`.
- **Ledgers continue in place.** A ledger made before 0.10.0 keeps its cash, positions, history and request IDs (a recorded request still replays); it moves to schema 2 on first use, then follows the rules above, and its open positions accrue funding from that first observation. Kits before 0.10.0 refuse an upgraded ledger.
- Removed: the 1x gross-notional rule and `virtual_1x_headroom`. `risk_model` is `reya_engine_margin`.
- SKILL.md: offer a paper rehearsal of the same command before a first leveraged order or one above 50% usage; report paper's margin figures as live and what paper leaves out; say so first when `events` shows a TP/SL fill or a liquidation; on a clock error, give the user the skew and the fix instead of retrying. `docs/paper-trading.md` (contract v2) adds what paper models, how faithful it is, and a leverage fixture; `docs/margin.md` is updated.

Where paper can still differ from live: it settles instantly (live, an order seconds after a fill also reserves for that fill); TP/SL and liquidation happen only when a command runs; liquidation always takes everything at the 100% line; no ADL, insurance fund, other collateral or haircuts.

Not exercised live: no orders were placed. A paper session ran against devnet1's public data (a `--usage 0.5` long with `--sl 2% --tp 3%`, `state`, `cancel_all`, a reduce-only close).


## 0.9.1 — 2026-10-19

- **A successful `order modify` returns its computed plan.** `docs/margin.md` and the 0.8.0 notes say `order market`, `order limit` and `order modify` all return a `margin` block, but a modify that the venue accepted reported only `action`, `account_id`, `symbol`, `qty`, `limit_px_usd`, `exec_qty` and the order fields. It now also returns the figures computed before the send, as `order limit` does: `mark_price_usd`, `limit_vs_mark_pct`, the account value and leverage figures, `reduces_position` and the `margin` block, counting only the unfilled remainder. The existing keys are unchanged. A failed send already carried the plan (0.8.3). The command reference describes the modify result.

Not exercised live: no orders were placed for this release.

## 0.9.0 — 2026-10-19

Runner orders in about one round trip, from a tester report: from East Asia an order through the runner took 1–2 s, while the order call itself took about 350 ms. The rest was reads before and after each command, one after another.

- **Warm state from the public WebSocket.** The runner keeps marks, balances, positions, open orders and fills from one connection to the public market and wallet feeds (`ws.reya.xyz`; it carries no orders and needs no IP allowlisting). The fat-finger and margin guards run on that state, so an order reads nothing over the network before it is sent. The ready event reports `warm_state`.
  - **Measured on devnet** from Europe, with order entry stubbed (`python -m agent_kit.scripts.latency`): a limit order went from 402 ms with eight sequential reads before the send to 13 ms with none. At about 0.25 s per read, that is roughly 2 s less per order from East Asia (estimate).
  - **Marks** are used only if their channel pushed within the last second (it pushes every 500 ms). The budget's 30-second staleness check still applies.
  - **Wallet state** is used only while the socket is connected, every wallet channel has its subscribe snapshot, and REST reads made after that snapshot matched it. REST re-checks it every 60 s, and a mismatch resubscribes. Outside these bounds the runner reads REST, exactly as before.
  - **The runner's own fills** count in the position from the acknowledgement until the feed shows them included (matched by trade sequence number), with an upper bound on the fee and any realized loss debited, never a gain. When the position changed and that cannot be told, the next order reads REST. A fill the feed does not show within 5 s resubscribes.
  - **Reference data** (market and collateral definitions, fee tiers) is read once and refreshed in the background every 30 s. The owned-account check runs once per session.
- **Fees after the result.** Inside the runner a fill's fee read no longer delays the result. The result carries `fees_status: "deferred"`, and the same single read follows as `{"event":"fees","request_id":…}` with `fees_status`, `fees_rusd` and `executions`. It is also journaled as `fee_reads`. `--fees_wait_s N` keeps the inline wait.
- **Cancel and modify without a read.** Inside the runner the named order is resolved from the warm open-order view, including orders the session placed that the feed has not shown yet. The account's open-order read decides when it is not there or the identifier is ambiguous.
- **A definitive rejection no longer ends the session.** A single write that the venue answered with `RATE_LIMITED_ERROR` (with `retry_after_ms`), `POST_ONLY_WOULD_CROSS_ERROR`, `ORDER_NOT_FOUND_ERROR` (cancel or modify) or `MODIFY_QTY_BELOW_FILLED_ERROR` (modify), an edge 429, or a market/limit/modify acknowledged as `CANCELLED` (an IOC partly filled, a post-only that would cross) now adds `session_continues: true`, and the session keeps running. Nothing is retried, and the budget stays charged. Timeouts, transport and 5xx errors, `UNKNOWN`, other codes and any failure in a multi-write command still stop it for reconciliation. A quoting loop meets the venue's per-account order-rate limit and post-only crosses routinely; each used to end the session.
- **Heartbeat every 10 s** instead of 5 s: three renewals per 30-second countdown, and half as many orders wait behind a renewal.
- **`python -m agent_kit.scripts.latency`** times one market-data request and the same order through the runner with REST reads and with warm state, from the user's own connection. Order entry is a local stub, orders are signed with a throwaway key, and writer state goes to a temporary directory, so nothing reaches the venue.
- Unchanged: no write is resent; the journal still records intent before the send and the acknowledgement before any read. Budgets are unchanged. TP/SL position checks, `order market --sl/--tp`, `close_all`, `status`, shutdown cleanup and settlement read REST.

Not exercised live: a real order on the warm path, and the WebSocket path on mainnet. Measured on devnet with order entry stubbed.

## 0.8.4 — 2026-10-19

- **The setup link names the agent account.** Once the kit has recorded the agent account, the setup link carries it (`&account=<id>`), and the page uses that account whatever its name, without looking for "Agent account" or asking which one. Before, the page went by the name alone: with an account recorded through `--account-id` or found as a new sub-account under another name, it asked the user to create an "Agent account", and funding that one left the kit waiting on the account it records. A reinstall whose approval has to be given again carries the recorded account from the first step. An app without the parameter ignores it, so the link works either way.
- **Still one tab.** A fresh setup's link gains `&account=` when the new Agent account is recorded, after the page is already open. `init` (with or without `--until-ready`) does not open that link again while the open page uses the same account: the owner's one sub-account named "Agent account" is the recorded one. When it is not, for example after `--account-id`, the page that is open would use another account, so `init` opens the new link.

## 0.8.3 — 2026-10-19

Two fixes to the 0.8.0 margin figures, from a devnet check of the predictions against the matching engine (they matched its reject messages to within 0.00012 rUSD).

- **A failed send keeps the kit's figures.** When `order market`, `order limit` or `order modify` is sent (for example with `--skip_margin_check`) and the venue rejects it, or the reply is lost, the error result now carries the computed plan, including the `margin` block, next to the venue's `error`, `error_code` and `message`. The kit's `engine_check`, `engine_message` and `shortfall_usd` can be read against the engine's reply. Exit codes and the `client_order_id` reported for creates are unchanged; a failure before the plan is built still reports the error alone.
- **After-fill figures are a worst case, and say so.** The preview's `margin` block adds `after_fill_basis`: every `*_after` figure assumes a full fill at the limit price, the worst case for an order that opens or adds to a position (a market order's limit is its slippage bound). On devnet a BTC market order sized with `--max_size` previewed `margin_usage_after` 0.61 and left 0.50 after the fill. SKILL.md's "Before every live order" rule now has assistants report these figures as "up to" and read `query margin` after the fill; `docs/margin.md` and the command reference are updated.

Not exercised live: no orders were placed for this release.

## 0.8.2 — 2026-10-18

Installing on Windows (WSL), on a server, or anywhere without a browser, from tester reports on WSL Ubuntu 26.04 and a headless Linux machine.

- **The installer no longer hangs on a hidden GitHub prompt.** `install.sh --release=latest` fetched release tags with plain git. With only `gh auth login` done (run by an assistant, it does not set up git), git waited for a username on a terminal nobody could see. Git now never prompts: terminal prompts are off, and SSH runs in batch mode unless you set your own SSH command. A signed-in GitHub CLI supplies the credentials for that one command, as `gh` does itself, without changing your git configuration. If git still cannot reach GitHub and the release is not in the checkout, the installer stops and says to run `gh auth login` (or `gh auth setup-git`).
- **The update check works for the same users.** `kit.latest_release` was always `null` when git had no GitHub credentials of its own. After a plain failure the check now retries once with the GitHub CLI's credentials.
- **A Python without its venv module no longer stops the install.** Ubuntu and Debian ship that module separately (`python3.X-venv`). The installer tries each Python 3.12+, then uv: one already on the machine, or a pinned uv 0.12.19 from its GitHub release, checked against the SHA-256 in `install.sh` before it runs and kept in `.uv/` inside the kit with its Python and cache. No admin rights, no PATH or shell-profile changes, and deleting the kit folder removes it. uv also supplies Python 3.12 when the machine has none (a stock Mac ships 3.9), so `--bootstrap-python` is no longer needed; it is still accepted.
- **A failed install no longer breaks the next one.** A failed or interrupted run left a half-made `.venv` (Python but no pip), and every retry then failed with `.venv/bin/pip: No such file`, even after the missing package was installed. A `.venv` whose pip does not run is now rebuilt.
- **The setup page on WSL and without a browser.** Python's browser lookup reports success when nothing opened (`gio: Operation not supported` under WSL, or a text browser on a server), so `agent init` said `opened: true`, and later runs said `link_already_opened`. Now:
  - on WSL the page opens in the Windows browser (`wslview`, else `explorer.exe`);
  - in an SSH session, on Linux without a display, or on WSL that cannot start Windows programs, the kit does not try. The result has `opened: false` and `browser_unavailable` (`ssh`, `no_display`, `wsl_no_windows_browser`, or `no_browser` when a browser did not start), `next` asks the user to open `url` on any device where their wallet is, and the link is printed on stderr;
  - in that case `init --until-ready` (and `--wait`) returns at once with `waited: false` when its output goes to a program such as an assistant, instead of waiting 100 seconds on a link nobody has seen. The next run waits. At a terminal it prints the link and keeps waiting;
  - `opened`, `link_already_opened` and `browser_unavailable` stay in the result after a wait; they were dropped.
- **README:** new sections "On Windows" (WSL), "Signing in to GitHub" (`gh auth login` in your own terminal, entering the one-time code on any device), "No browser on this computer", and "If your assistant's safety mode stops a step". The assistant steps ask the user to run `gh auth login` themselves and handle `browser_unavailable`. SKILL.md and the command reference document the new fields.
- `.gitattributes` keeps `*.sh` at LF line endings, so a checkout made with Git for Windows still runs in WSL.
- CI runs `install.sh` in an Ubuntu 26.04 container, with Ubuntu's Python but no `python3-venv` and with no Python at all, and checks that a half-made `.venv` is rebuilt and that a fetch with no GitHub credentials fails within seconds.

Not tested on Windows itself: opening the Windows browser from WSL (`wslview`, `explorer.exe`) is covered by unit tests only.

## 0.8.1 — 2026-10-18

- **Warning: a take-profit that can close at a loss.** A trigger fires on the mark, but its IOC child fills against the order book at any price up to its limit. On devnet a long's take-profit triggered at 2356.567 and filled at 2353.681, below the 2358.069 entry, because the best bid sat below the mark. When a take-profit's child limit is past the position's average entry (below it for a long, above it for a short), the kit still places it and adds `warning` with the distance and the fix:
  - `order tp`: in the result's `warning`; set `--limit_px` at or beyond the entry to take only a profit (it may then not fill). `order tp` and `order sl` results add `entry_px_usd`.
  - `order market --tp`: on `protection_plan.take_profit` in a preview (against the estimated entry: the mark, blended with any position the order adds to) and on `protection.take_profit` once armed (against the position's average entry). The child limit sits `--trigger_slippage` (default 1%) past the trigger, so any `--tp` closer than that is below the entry; the documented `--tp 0.5%` example now warns.
- SKILL.md: when a take-profit carries `warning`, say it can close at a loss and offer the fix.

## 0.8.0 — 2026-10-18

Margin before the trade, from a tester report (kit 0.5.1, mainnet): a long sized to SPY's 25x was rejected as `ACCOUNT_BELOW_INITIAL_MARGIN_ERROR`, "0.58 rUSD below its settlement reserve", and nothing in the kit showed the reserve, equity with unrealized PnL or a liquidation price. The kit now rebuilds the matching engine's margin model from public V2 data: a port of its risk engine (source revision `4d8ed012`), tested with vectors from the engine's own tests. [`docs/margin.md`](docs/margin.md) has the model and its limits.

- **What the engine checks:** health = min(equity − liquidation margin, real balance) must stay at or above 0 before an order; equity must cover initial margin (1.3 × liquidation margin) before and after; and health after must cover a settlement reserve of 1% of the order's notional at its limit price. A market order is charged its full slippage up front. A reduce-only order or TP/SL owes only the first check. On the 25x and 40x markets the reserve binds before initial margin: the largest single order from flat is about 24.5x on SPY and 34.2x on BTC with the limit at the mark, and 19.7x and 25.4x with the default 1% slippage. V2's `max_leverage` is `round(1 / IM)`, so SPY shows 25 while initial margin allows 24.997.
- **`query margin`** adds `equity_usd` (collateral after haircuts + unrealized PnL at mark + an unsettled-funding estimate), `real_balance_usd`, `unrealized_pnl_usd`, `unsettled_funding_estimate_usd`, `initial_margin_req_usd`, `margin_usage` / `margin_usage_pct` (liquidation margin / equity; 1 is liquidation), `liquidatable`, `initial_margin_buffer_usd`, `health_usd`, `im_multiplier`, `settlement_reserve_rate` and `assumptions`. Each position adds `avg_entry_price`, `unrealized_pnl_usd`, `unsettled_funding_estimate_usd`, `liquidation_price_usd` and `distance_to_liquidation_pct`. **`margin_buffer_usd` is now equity − liquidation margin**; it was collateral − liquidation margin, which does not move with price.
- **`query positions`** adds `liquidation_price_usd`, `distance_to_liquidation_pct` and `account_margin_usage`. If the margin read fails, positions are still returned with `margin_unavailable_reason`.
- **Liquidation price** is cross margin: the price in that market at which equity equals the liquidation margin, every other mark, collateral price and funding unchanged, rounded onto the tick toward the mark.
- **Order previews and live orders** (`order market`, `order limit`, `order modify`) carry a `margin` block for a full fill at the limit price. It holds the engine's verdict (`engine_check`: `ok`, `tight`, or the reason with `engine_message` and `shortfall_usd`), equity and margin usage before and after, the projected requirements and reserve, the liquidation price and distance after the fill, and `max_qty`.
- **New refusal `engine_margin`:** an order the engine would reject for margin is refused before signing, with the engine's own message and the largest size that fits. `--skip_margin_check` overrides it for when the kit is stricter than the engine; the runner refuses the flag.
- **Sizing by risk:** `--max_size` sizes to the largest qty the engine would admit at the order's price, leaving `REYA_MARGIN_HEADROOM` spare (default 0.2% of the position notional after the fill). `--usage U` sizes so margin usage after the fill is at most U. Both stay within `REYA_MAX_ORDER_NOTIONAL` and `REYA_MAX_LEVERAGE` unless `--allow_large`, report `sizing.size_limited_by`, and size only orders that open or add to a position.
- **Reduce-only orders, TP/SL and `close_all` are never refused for margin.** When the account is below its liquidation threshold, the engine rejects every order, closes included; the preview notes this and the kit still sends the reduce-only order. An opposite-side order without `--reduce_only` is ordinary to the engine, and the preview's `reduce_only_hint` says so.
- **`account_value_usd` (0.6.0 guards) is now equity as above:** collateral after haircuts, with withdrawal-only collateral counted as 0, plus the funding estimate. It used collateral without haircuts and no funding.
- **Paper:** `status` and `refresh` add a `margin` block and a per-position `liquidation_price` from the same model. Paper admission stays the 1x rule, and the top-level `liquidation_price: null` is gone.
- **What the kit cannot see:**
  - Matched fills not yet settled (seconds). Size the next order after the previous fill shows in `query positions`.
  - The last settled funding value. V2 has only the average entry value, so unsettled funding is estimated as a debit only.
  - ADL multipliers.
  - The 1% reserve rate, which V2 does not publish; it is pinned.
- SKILL.md: size with `--max_size` or `--usage`, never from `max_leverage`; state margin usage and the liquidation price with every opening order, and ask again above 50% usage or when `tight`; monitor with equity from `query margin`, not balances; close with `--reduce_only`.
- `docs/margin.md` (new), the command reference, error codes (both causes of `ACCOUNT_BELOW_INITIAL_MARGIN_ERROR`), paper trading, README and the `.env` examples are updated.

Not exercised live: no orders were placed for this release, and the figures were not compared with the engine's own projection on a live account.

## 0.7.0 — 2026-10-18

A stop-loss and take-profit straight after an entry, in one command, armed only once the position includes the fill.

- **New: `order market ... --sl L --tp L`.** `L` is a percent of the entry's average fill price (`0.35%`) or a price (`5790`); either flag can be used alone. After the fill, the command reads positions until the position shows the entry's side with a newer `lastTradeSequenceNumber` than before the entry (every 0.5 s, up to 5 s). It then arms the stop-loss, then the take-profit, as IOC children `--trigger_slippage` beyond each trigger (default 1%, at most 3%, inside the venue's 300 bp band), reads both back, and returns `protection`:
  - `state` is `protected`, `partial` (stop-loss armed, take-profit not), `unprotected`, `not_placed` (nothing filled), `unknown` (entry outcome unknown) or `pending` (stopped mid-way).
  - Each leg has its prices, order and client IDs, `armed`, `visible` and the exact `order sl`/`order tp` command to arm it by hand.
  - On any state but `protected` and `not_placed` the command exits 1, and `error` starts with what is missing, says the position is UNPROTECTED when there is no stop-loss, and gives the command to arm it.
  - The command never resends the entry, never retries a trigger and never closes the position. A position not visible within 5 s gets no trigger: a busted fill leaves no position, and a stop armed without one would act on the next position.
  - Partial fills are protected as filled.
- **Refused before sending,** in `refused_by` next to the fat-finger guards:
  - a stop-loss or take-profit of the requested kind already armed on the market (`protection_exists`; the venue allows one of each per position);
  - an open position on the other side (`protection_opposite_position`);
  - a level already through the mark (`protection_fires_at_once`).
  - Also refused: `--reduce_only` with `--sl/--tp`, and `--sl/--tp` on `order limit` (a resting limit can fill after the command exits). `--preview` adds `protection_plan`, with estimated trigger and child prices and signed P&L at each.
- **Fix: standalone `order tp` / `order sl` straight after an entry.** They read the position once outside the runner, usually before it caught up with the fill, and refused (or needed `--force`). They now read up to 7 times within 4 s, as the runner did. They also keep reading through a wrong-side read, since a flip shows the old side first; before, the runner refused at that read. The no-position refusal no longer suggests `--force`.
- **Runner:** a market entry with `--sl/--tp` may take up to 30 s instead of 15 s, so a timeout cannot fall between the fill and the stop.
- **`bracketed_long.py`:** previews, then sends one `order market --sl --tp`. It flattens only when the entry filled with no stop-loss armed. Before, it placed TP before SL, and one missed position read could make it exit with the position open and no stops. Its settlement check re-reads positions (10 reads, 0.5 s apart) and accepts a partly filled IOC.
- **SKILL.md:**
  - Use `--sl/--tp` for entries with protection.
  - The preview line adds the stop and target prices and the estimated result at the stop.
  - Every reply after a protected entry states the protection, and an unprotected result opens with "Your position is UNPROTECTED".
  - The "Sliced entry with a bracket" recipe arms protection on slice 1.
  - User-run orders: entry and protection are one command.

Not exercised live: no orders were placed for this release. The venue facts it relies on were read from the SDK's API schema and the matching engine's error codes: `Position.lastTradeSequenceNumber` uses the execution sequence counter, the engine has no rejection for a trigger without a position, and the venue allows one SL and one TP per position.

## 0.6.1 — 2026-10-18

- **`close_all --preview` lists the TP/SL the close drops.** TP/SL cover the whole position, so the venue cancels them once the position is flat, but the preview showed only the position. Each `would_close` entry now has `drops_protection`: that account's take-profit and stop-loss on the market (`order_id`, `order_type`, `trigger_px`, `limit_px`), or `null` when the open orders could not be read. `would_drop_protection_count` totals them (`0` when nothing would close). When a close is planned and the order list is unreadable, the preview reports it under `errors` (context `TP/SL read`, also with `--cancel_orders`) and exits 1. With `--cancel_orders` the same orders also appear in `would_cancel`, because the kit cancels them before closing. The real close is unchanged and reads no orders without `--cancel_orders`.
- SKILL.md: when showing a `close_all` preview, name the TP/SL it drops.

## 0.6.0 — 2026-10-16

Safety before the tester launch: a runner exit no longer leaves a position without its stop, and every new order is checked for fat-finger size, price and leverage before it is signed.

- **Fix: the runner kept positions but cancelled their TP/SL on exit.** Every exit the runner handles itself (stop, EOF, idle or command limit, heartbeat failure, a caught error) sent an account-wide cancel, and in the matching engine a user `cancelAll` also clears armed TP/SL; only the venue countdown's own fire keeps them. Now the runner reads positions and orders first. With no position and no TP/SL it still sends one account-wide cancel. Otherwise, including when a read fails, it cancels each other order individually and leaves TP/SL armed; the countdown stays armed and sweeps anything the reads missed, still keeping TP/SL. The `stopped` event adds `cleanup` (`mode`, `kept_order_ids`, `cancelled_order_ids`, `errors`) and `protection` (each remaining position with its armed stop-loss and take-profit, or `null`), and its `error` names any position left open and whether it has a stop. Reconciliation and countdown rules are unchanged: a session that ends with a position is never reconciled and never disarms the countdown.
- **Docs corrected:** SKILL.md, README and the runner guide said the venue timer cancels TP/SL. It keeps them; it was the runner's own cleanup that cancelled them.
- **Limit-price band:** `order limit` and `order modify` refuse a price more than 5% through the mark (a buy above it, a sell below it) unless `--force`, and more than 50% even with it. 5% is the market-order slippage cap, so a marketable limit cannot go further than `order market` without `--force`, and the venue busts fills more than 5% from its mark on chain anyway. Passive prices are never refused.
- **Per-order notional cap and leverage limit:** `order market`, `order limit` and `order modify` refuse an order above `REYA_MAX_ORDER_NOTIONAL` (USDC, in `.env`; default: the account value) or one that takes estimated leverage after a full fill above `REYA_MAX_LEVERAGE` (default 5) and higher than before. `--allow_large` overrides both, after the user confirms. Account value is collateral plus estimated unrealized PnL at mark; leverage counts all positions at mark, not resting orders. Orders that only reduce a position are never refused for size, and a reduce-only market order is sent even if the account reads fail.
- Refusals send nothing and assign no client order ID. They exit 1 with `refused_by` (`check`, `message`, `override`) and the computed figures.
- **`--preview` on `order market` and `order limit`:** returns qty, price bound, notional, `limit_vs_mark_pct`, `account_value_usd`, `current_leverage`, `est_leverage`, the limits, `would_send` and `refused_by`, and sends nothing. Live results carry the same figures.
- Inside the runner the guards apply and `--allow_large` is refused like `--force`.
- **SKILL.md:**
  - Before every live order, preview it and state the notional in USDC, the estimated leverage and the distance from the mark.
  - Ask for an explicit yes for the first live order in a conversation, above 25% of the account value, above 2x estimated leverage, or for any override.
  - New recipe, "Sliced entry with a bracket": slice with `order market --notional` at tight slippage; after the first fill place one venue-side SL and one TP, which cover later slices; re-check the position and triggers before each slice and stop if a trigger fired; report any shortfall and never chase.
- Examples, the runner guide (§3) and `docs/mainnet.md` no longer say `PERP_PRIVATE_KEY_1` is required: the agent key from `agent init` is the default signer.
- `.env.example` and `.env.mainnet.example` document `REYA_MAX_ORDER_NOTIONAL` and `REYA_MAX_LEVERAGE`.

Not exercised live: no orders were placed for this release. The engine behaviour it relies on (a user `cancelAll` clears TP/SL, a countdown fire keeps them, a single cancel touches one order) was read from the matching engine source.

## 0.5.1 — 2026-10-16

- **The setup page checks the wallet.** The setup link now carries the owner (`&owner=0x…`). With another wallet connected, the page shows only a switch: both wallets, and a button that switches to the owner (or reconnects). It no longer approves the agent, creates the Agent account and funds it on a wallet the kit is not watching, after which `init --until-ready` waited on approval for ever. Needs the matching Reya app update; the app ignored the parameter before it, so the link works either way.
- When `init --until-ready` times out still waiting for approval, `next` says the likely cause (the page connected to another wallet) and both ways out: switch on the page, or set up the connected wallet instead.
- Switching owner: the "already set to a different owner" error names the configured owner and says how to switch (remove `PERP_WALLET_ADDRESS_1` from `.env`, run `init --owner` again; the agent key is kept). README and SKILL.md say the same.

## 0.5.0 — 2026-10-15

One page and one wait for real-trading setup, instead of three pages and a reply after each.

- **One setup page.** For the live steps, `url` is now the Reya app's setup page (`/agents/setup`), which walks the user through approving the agent, creating the Agent account and funding it. It opens once, in one tab. The page is unlisted: the link carries an access code in its `#` fragment, which the browser never sends to the server. This keeps casual visitors out while the kit is private; it is not a secret. `step_url` keeps the app's regular page for the current step as a fallback.
- **One wait.** `agent init --until-ready` polls through every step on the page and returns at `ready` (or when a decision is needed, or at `--timeout`), with `progress` (step N of 3). Assistants no longer ask the user to reply "done" after each step. `--wait` still returns at the next step change.
- **Practise first, then open.** A fresh setup asks "practise with virtual money first, or set up real trading now?" before anything opens in the browser; the next `init` run opens the setup page.
- **Duplicate "Agent account"s:** the kit asks which one to use and no longer suggests renaming the others, which the app cannot do yet for accounts without positions. The others are left alone.
- `install.sh` and README point assistants at `init --until-ready`.

Needs the Reya app's setup page (`/agents/setup`), live since 15 October 2026.

## 0.4.2 — 2026-10-15

- The sub-account named exactly "Agent account" is always the agent's. `init` uses it whenever the wallet has one (from this setup, an earlier one or a reinstall) instead of asking for another, ahead of any other new sub-account, and `next` names it. If several have that name, it asks which one and suggests renaming the others in the app for now; nothing new is created.
- Setup no longer stalls at the Agent account step. The README told assistants to poll `agent status --wait` after the install, but `status` is read-only, so when the user created the account before an `init` had recorded which accounts existed, neither `status` nor `init` ever picked it up. `status` now reports such an account (`found_account_id`, no create link), and README and SKILL.md poll with `init --wait`.

## 0.4.1 — 2026-10-15

- Say USDC, as the Reya app does. The app shows the rUSD collateral as "USDC" everywhere, so setup told users to move "rUSD" while the transfer page they opened said USDC. The funding step's `next`, the `--amount` help, README and SKILL.md now say USDC, and SKILL.md tells assistants to say USDC to users. JSON field names are unchanged (`funding_guidance.min_order_rusd`, `suggested_amount_rusd`, `fee_rusd`).

## 0.4.0 — 2026-10-15

First-run polish from a walkthrough on 15 October 2026.

- One browser tab per setup step: a step's page is no longer reopened when `agent init` / `agent status` is re-run (the walkthrough ended with six tabs). The kit remembers opened links for 30 minutes in its state directory; results report `link_already_opened: true` instead.
- Funding guidance: the funding step now reads the smallest ETH order and the mark price and returns `funding_guidance` (minimum order in ETH and rUSD, and a suggested first deposit: at least 50 rUSD, or room for two minimum orders), says it is the last step, and asks the assistant to confirm the amount and pre-fill it with `--amount`. Market reads failing never block setup.
- Practice first: a fresh setup's result carries `practice_first`, the question "practise with virtual money first, or set up real trading now?" and the paper-trading command, so real funds are a choice rather than a silent default.
- `install.sh` finds a Python 3.12+ through uv when the system has none (a stock Mac ships 3.9); `--bootstrap-python` installs a pinned uv and a private Python without admin rights. The installer now prints a short summary instead of the raw doctor and health JSON (`--verbose` restores it).
- SKILL.md: say so when a requested size is below the market minimum, with the minimum in rUSD, before preparing anything.

## 0.3.2 — 2026-10-15

Docs only.

- SKILL.md: "User-run orders", the flow for an assistant that cannot place trades itself (as of October 2026 the Claude desktop app): run reads and previews itself, describe each write in plain words, show exactly one runnable command per write (absolute path, explicit client order ID, one `bash` block so a Run button appears), let the user run it, then verify with reads; never batch writes or re-show a write whose outcome is unknown.

## 0.3.1 — 2026-10-15

Docs only. In testing on 15 October 2026 the Claude desktop app, including its
Code tab, declined to place a live trade after an explicit request, while Claude
Code in a terminal and Codex both placed trades through the kit on mainnet:
each placed and cancelled a post-only order, and Codex also bought and closed
0.01 ETH with market orders on request.

- README: "Start here" now asks for Claude Code in a terminal (`claude`, with an install link) or Codex, and says why the desktop app and regular chat won't work. The "For AI assistants" steps tell a trade-restricted assistant to say so before setup.
- SKILL.md: an assistant that cannot place trades must say so the first time trading comes up, never claim it can trade and then refuse, and offer the terminal path or the exact command for the user to run.

## 0.3.0 — 2026-10-15

Traders no longer hand the kit a private key, and setup is one prompt to Claude
Code or Codex. Exercised live on mainnet on 14 October 2026 with the owner's
wallet: install, approval on the API page, agent account creation, funding, an
order placed and cancelled by the agent key, revocation refusing the next
order, and re-approval restoring it. The app pre-fills for the approval,
create-account and transfer links (app PRs #1619 and #1620) apply once the app
is deployed. Not exercised live: the one-prompt path end to end on a fresh
machine, and the update check against a newer release (none exists yet).

- One-prompt onboarding: the README starts with a single prompt to paste into Claude Code or Codex, what happens next (three steps in the app), things to try, what the kit can and cannot do, and update/remove prompts. A "For AI assistants" section gives the exact bootstrap, including what to do in a chat-only app and when the private repository is not accessible. Developer material moved under "For developers and operators". Installs no longer use commit SHAs.
- `install.sh --release=latest|vX.Y.Z` switches the checkout to a published release before installing (GitHub's Latest release, else the highest `vX.Y.Z` tag), refusing when tracked files have local changes and never touching `.env` or `.venv`; the switch re-runs that release's own installer. `install.sh --owner=0x…` validates the address up front and continues into `agent init`, so `./install.sh --release=latest --owner=0x…` takes a new user from nothing to the first step in the app.
- `agent init` / `agent status`: at `ready` the result carries `try_asking` (four safe first prompts), and every result carries `kit` with the installed version, the latest release and `update_available` (best-effort `git ls-remote` against the checkout's origin, 3 s timeout, no credential or SSH prompts; `REYA_KIT_UPDATE_CHECK=0` disables it).

- New `agent init` / `agent status` (`agent_kit/scripts/agent.py`): an idempotent, state-driven setup. The kit generates its own agent key into `.env` (0600, never printed), the owner approves its address once on the app's API page (the same on-chain permission as one-click trading: orders only, no withdrawals or transfers), the app creates and funds a dedicated "Agent account" through deep links, and the kit records which account the agent key may trade. A wallet with no Reya accounts is sent to the app first; the kit never creates a wallet's first account.
- Signer selection: the agent key (`PERP_AGENT_PRIVATE_KEY`) is the default; `PERP_PRIVATE_KEY_1` / `PERP_PRIVATE_KEY_FILE` now require an explicit `REYA_SIGNER=own-key` and are otherwise an error, so an old `.env` fails loudly. Own-key mode remains supported for users who deliberately hand the kit a dedicated wallet.
- With the agent key, writes to any account not in `PERP_AGENT_ACCOUNT_IDS` are refused before any network call; `UNAUTHORIZED_SIGNATURE_ERROR` carries a hint to run `agent status` (approval may have been revoked).
- `doctor` reports `signer_mode`, `agent_address` and `agent_account_ids`, flags own-key variables without the opt-in, and warns about a world-readable `.env` holding either key.
- Non-V2 exception: `GET /api/trading/wallet/{owner}/apiWalletStatus` (read-only) for approval status, a documented exception until a V2 read exists, reported by `doctor`.
- Docs: README, SKILL.md, SECURITY.md, `.env.example` and `.env.mainnet.example` rewritten around "approve, don't paste"; `REYA_APP_URL` for non-mainnet deep links.
- `agent init` / `agent status` open the current step's app link in the default browser (`--no-open` to suppress). The page still requires the user's own click and wallet signature; nothing is submitted automatically.
- `agent init --wait` / `agent status --wait` now return as soon as the current step completes (the step changes) and report `timed_out`; previously they kept polling until `ready`, so the next instruction was never shown.
- When several new sub-accounts appear during setup, `agent init` picks the one named exactly "Agent account" if there is one; otherwise it still asks for `--account-id`.
- The execution and margin reference is the mark price, not the oracle index. `order market` sizes `--notional` using mark price and bounds its IOC limit at `mark × (1 ± slippage)`; `positions close_all` uses mark price for its IOC limit and preview estimate; `query margin` values positions at mark; the runner charges budget at `max(limit, mark)`; paper market orders are mark/slippage-bounded. Fills on the order book are bounded against the venue mark, so sizing off the oracle was a pool-era assumption that could miss when the two diverge. JSON contract: `oracle_price_usd` becomes `mark_price_usd` in `order market`, in `positions close_all` entries and in `query margin` positions (`query prices` still returns both). Collateral is still valued at asset oracle prices. The stale pool-price note left the command reference.
- Walkthrough fixes: the approval link targets the app's real route `/api-page` (was `/api`, which falls through to the trade view); devnet1 defaults to `https://app-devnet-cf.reya.xyz`; `.env.example` is now mainnet-first (mainnet is the default and `agent init` needs no `.env` beforehand), with devnet1 and Cronos as commented alternatives; the installer's next steps say so.

## 0.2.1 — 2026-10-12

Usability and safety improvements for agent workflows. No new non-V2 endpoints
and no change to write-retry behaviour. Not exercised live: the
`close_all --cancel_orders` write path, the uPnL fields on a funded account,
and real regional-403 / edge-429 responses (covered by offline tests).

- Symbols accept bare tickers in any case (`eth` → `ETHRUSDPERP`) everywhere except `data market search`, which stays a substring search.
- Every market, limit, TP and SL create carries a client order ID: `--client_order_id` or a random one. It is returned in the result, including error results, so a lost reply can be found.
- `query positions` adds `mark_price`, `mark_updated_at` and `estimated_unrealized_pnl_usd` (price PnL at the V2 mark; excludes funding and fees; null, with `mark_unavailable_reason`, when marks cannot be read).
- `query fees` reads fee tiers and global discounts, and with `PERP_WALLET_ADDRESS_1` set that wallet's tier and status. No key needed.
- `positions close_all --cancel_orders` attempts to cancel each open perp order in the same scope before reading and closing positions. Failed cancellations and a failed order read are reported under `errors` while flattening continues; a failed position read after cancelling returns the confirmed cancellations and closes nothing. Previews list `would_cancel`.
- `paper order market <symbol> --side long|short ... --client_order_id ID` accepts the live order shape; the flag form still works and `paper order limit` is refused.
- Error envelopes: the regional 403 page becomes `REGION_UNAVAILABLE`, a per-IP edge 429 becomes `EDGE_RATE_LIMITED`, and a venue `retryAfterMs` is returned as `retry_after_ms`. The kit's HTTP clients send a `reya-trading-agent-kit/<version>` User-Agent.
- `doctor` warns, without reading the value, when `.env` holds an inline `PERP_PRIVATE_KEY_1` and other users can read it.
- Docs: new [error codes](docs/error-codes.md) table with a retry-safety column and [command reference](docs/command-reference.md); SKILL.md trimmed to the operating rules (662 → ~350 lines); SECURITY.md compromise steps and audit commands corrected; the non-V2 account-setup exception is recorded as agreed on 12 October 2026.

## 0.2.0 — 2026-10-12

Private pilot release. It adds supported V2 market/account data with resilient
WebSocket feeds, and account-state and uncertain-operation reconciliation. It also
adds credential-free local paper trading, account-period history and performance,
API-relayed account setup with a bounded trading runner, and a versioned private
first-use path.

### Devnet acceptance fixes

- Paper refuses reduce-only closes below the market minimum, as the live API does (devnet1 rejected both a subminimum and an oversized reduce-only close).
- `close_all` explains a rejected below-minimum close with a `hint`.
- `account status --tx-hash` saves an operator hash only once it verifies, refuses a hash already recorded for another request, and refuses a different hash for a request that is already resolved.
- `account status` confirms a lost-reply create or transfer from a unique, unclaimed matching observation after the request (`unique_observation_after_request`); limitations are documented in the operator guide.
- Stream `state` reports `incomplete` before `stale`; a separate `stale` flag reports quiet data.
- `orderChanges` keeps observed terminal orders across resubscriptions, including the observer's own reconnect loop, and recovers fills made while disconnected from REST order history; `order_scope` states what a fresh observer excludes.
- The stream reconnect budget counts consecutive failed attempts per outage instead of all drops in an observation.
- Paper charges taker fees on quantity × the observed V2 mark with settlement rounding (6 dp; notional away from zero, fee down), matching on-chain settlement, instead of on fill price.
- History marks `orderHistory` / `transfers` incomplete when in-scope fills prove served rows are missing (`orders_missing_for_fills`, `fills_without_fee_transfers`), and adds `cashflow_totals_state` so incomplete transfer totals never read as zero. The history example now shows realized PnL as price variation plus funding, excluding fees.

### First-use walkthrough fixes

- Name `PERP_PRIVATE_KEY_FILE` / `PERP_PRIVATE_KEY_1` when a trade or runner command has no signing key, instead of the SDK's generic signer error.
- Add clone troubleshooting for SSH protocol/`insteadOf` rewrites without organization access.
- Align README and SKILL install steps with the private guide: reviewed commit SHA and explicit `--agent` link target.
- Link the skill for Codex (`--agent=codex`, honouring `CODEX_HOME`; included by default when Codex is installed, and `--agent=all` links every client). `uninstall.sh` and `doctor` cover the Codex link.

### Review stabilization

- Qualify cancel/modify by market and reject ambiguous IDs before signing.
- Preserve acknowledged-fill uncertainty through runner shutdown; verify settled/busted quantities and flat session exposure.
- Accept object summary updates; orient maker history to the reported account.
- Retain legacy unidentified execution evidence without guessed sums; retain balanced transfer observations without guessing leg identity.

### Paper trading and history

- Add isolated local paper market/IOC trading with durable virtual accounts, request replay,
  live V2 depth, partial fills, explicit fee assumptions and documented simulation limits.
- Add account-period V2 history/performance reports with source-separated PnL/fees,
  same-wallet transfer pairing, pagination coverage and explicit unavailable metrics.

### Production configuration and account safety

- Document mainnet production configuration and operator acceptance; test mainnet signing.
- Account reads no longer need a private key. Reads/writes verify owner/account
  membership before inspecting or modifying an account.

### Current SDK and API

- Upgrade the pinned SDK to `v3.6.3.0` and migrate existing perp reads/writes.
- Prices now expose mark price instead of pool price; funding exposes total
  open interest instead of removed velocity and per-side OI fields.
- TP/SL use `TAKE_PROFIT` / `STOP_LOSS` and require `--limit_px` plus
  `--time_in_force IOC` or `GTC`. Guardrails use mark price.
- Orders retain fill/cancellation details. Incomplete IOC execution and batch
  failures exit 1. Close-all requires flat readback and never retries writes.
- Unify deployment configuration and distinguish devnet1 from Cronos.
- Add current-SDK regression tests and a documented devnet smoke procedure.

## 0.1.0

Initial release. Three CLI scripts wrapping the pinned `reya-python-sdk`, each
emitting JSON to stdout: `health.py` (connectivity check), `query.py` (prices,
market info, funding, positions, orders, balances, margin with haircuts) and
`trade.py` (market/limit orders, TP/SL triggers, cancel, and the batch risk
primitives `cancel_all` / `close_all`, both with `--preview`). Order math is
exact decimal arithmetic; every failure — including bad CLI arguments — exits
`1` with `{"error": "..."}` on stdout. Ships as an
[agentskills.io](https://agentskills.io)-format skill (`SKILL.md` at the repo
root) for Claude Code, Cursor, and compatible runtimes. Live execution only —
no paper trading; test on the Cronos testnet first (see `DISCLAIMER.md`).
