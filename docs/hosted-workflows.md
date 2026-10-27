# Hosted strategies

A strategy is a set of rules that Reya's hosted strategy service runs for you on your Agent
account, with your computer off: "when the ETH mark crosses up through 3,000, sell $1,000 of
ETH with a stop at 3,300 and buy $500 each of BTC and SOL, up to $2,000, for a week". The kit
is a thin client of that service:

- `draft` fills the service's `basket` template from your flags. The service expands it into
  the strategy document and checks it against the live markets; the kit writes that document
  to a file once it has checked it is what you asked for. The kit never writes a strategy
  document itself.
- `validate` and `preview` ask the service. The service is the only evaluator: the kit
  never decides on its own whether a trigger would fire, and there is no replay of past
  prices.
- `start` and `stop` are signed requests (EIP-712) signed with the agent key that
  `agent init` generated, never with your wallet's key. The key is never printed.
- `status` reads a strategy back from the service, with a read grant the agent key signs
  ([below](#private-run-reads)).

Strategies are a private pilot. Each environment says which modes it allows (`strategy
info`). The kit and the Reya app call the modes Paper and Live; the service, `--mode` and the
signed request use the values `paper` and `production`. A Paper strategy (`paper`, the
default) simulates its fills and stops into a labelled ledger and sends no orders; a Live one
(`production`) sends real orders on the Agent account. Every strategy, paper ones included,
also needs hosted runs enabled for the wallet: a one-time approval in the Reya app
([below](#enabling-hosted-runs)).

An Agent account runs up to a few strategies at once (`limits.max_live_strategies_per_account`
in `strategy info`, 3 in the pilot), and each market belongs to one of them: a strategy cannot
start on a market another live strategy on the account trades ([below](#one-market-one-strategy)).
To the user, strategy 7 is "Strategy #7", as in the app.

## Commands

All print JSON and exit 0 on success or 1 with an `error` field, like every kit command.

```bash
python -m agent_kit.scripts.strategy info [--open]
python -m agent_kit.scripts.strategy draft --trigger SPEC --order SPEC [--order SPEC ...] --allocation USD --expires 7d [options]
python -m agent_kit.scripts.strategy validate --file FILE
python -m agent_kit.scripts.strategy preview --file FILE
python -m agent_kit.scripts.strategy start --file FILE [--label NAME] [--fire-now | --wait-for-rearm] [--mode paper|production] [--no-open]
python -m agent_kit.scripts.strategy status [RUN_ID] [--all [--limit N] [--before RUN_ID]] [--account-id ID]
python -m agent_kit.scripts.strategy stop RUN_ID | --all [--keep-position] [--account-id ID]
```

`python -m agent_kit.scripts.workflow` runs the same commands: it is the name earlier kits used, kept as an
alias.

### `info`

The service's `environment`, `chain_id`, EIP-712 `eip712_domain`, the active
`hosted_signer` (the address that places the strategies' orders), the `modes` this environment
allows, the strategy documents it runs (`schemas`) and the pilot `limits`:
`max_allocation_usd` per strategy, `max_order_usd` per order, `max_live_strategies_per_account`,
`max_orders_per_action` (immediate orders one step sends) and `max_resting_orders`. The kit
reads these from the service on every signed request instead of building them in.

It also says whether hosted runs are enabled for the wallet ([below](#enabling-hosted-runs)):

| Field | Meaning |
| -- | -- |
| `hosted_signer_enabled` | `true` when the owner has approved `hosted_signer`, `false` when not, `null` when the kit could not tell: no Agent account recorded yet (`next` says to run `agent status`), or the approvals could not be read (`hosted_signer_note` says why). Absent when the service names no active hosted signer. |
| `enable_url` | With `false`: the setup page's Enable hosted runs step in the Reya app, for this agent key, owner and Agent account. `null` when no app is known for the deployment. |
| `next` | With `false` or `null`: what to tell the user. |

`info` is read-only and signs nothing. It opens nothing unless you pass `--open`. Then, with `false`, it opens `enable_url` once, like a refused start, and reports `opened`, `link_already_opened` or `browser_unavailable` ([below](#enabling-hosted-runs)).

### `draft`

A strategy has a trigger, an entry of one or more orders, and optionally conditions, exits and
a repeat. `SYMBOL` is `eth` or `ETHRUSDPERP`; prices and amounts are plain numbers.

| Option | Meaning |
| -- | -- |
| `--trigger price:SYMBOL:up\|down:LEVEL` | When SYMBOL's mark crosses LEVEL. `down`: from above the level to at or below it; `up`: the reverse. |
| `--trigger every:PERIOD[@ANCHOR]` | Every PERIOD (`5m` to `90d`; `m`, `h`, `d` or `w`), counted from ANCHOR: `HH:MM` (UTC, today), Unix seconds or an ISO time with a zone. Without ANCHOR it is 00:00 UTC today, so `every:4h` acts at 00:00, 04:00, 08:00… UTC. |
| `--trigger at:daily@HH:MM`, `at:mon@HH:MM` … `at:sun@HH:MM` | Every day, or every week on that day, at HH:MM UTC. |
| `--trigger start` | Once, right after the start. |
| `--order SYMBOL:buy\|sell:USD\|WEIGHT%[:STOP[:TAKE_PROFIT]]` | An immediate order: an IOC within `--bound-bps` of the mark. `USD` dollars, or `WEIGHT%` of `--total`. `STOP` and `TAKE_PROFIT` (both optional) are the prices of a stop-loss and a take-profit on the whole position in that market, armed once its first fill settles; leave `STOP` empty for a take-profit alone (`eth:buy:1000::3200`). Repeatable, one immediate order per market, up to 14. |
| `--resting SYMBOL:buy\|sell:USD\|WEIGHT%:PRICE\|±OFFSETbps[:STOP[:TAKE_PROFIT]]` | A resting limit order at PRICE, or OFFSET bps from the mark when it is placed (`-50bps` is 0.5% below), rounded onto the tick to the passive side (down for a buy, up for a sell). It rests until it fills, its state is left or the strategy expires. Repeatable, up to 40; markets may repeat. |
| `--no-post-only` | Resting orders may take liquidity when placed. By default they are post-only: one that would cross the book is refused, not filled. |
| `--total USD` | With weights: the dollars the weights are shares of. Weights add up to 100%, and a weighted entry is all immediate or all resting orders. |
| `--if funding:SYMBOL:above\|below:PCT_PER_HOUR` | Act only while the 15-minute average funding is above or below that, in per cent per hour. Positive funding means longs pay shorts, negative means shorts pay longs. Repeatable. |
| `--if position:SYMBOL:above\|below:QTY` | Act only while the position there is above or below QTY, in base units (long positive). Repeatable. |
| `--if balance:above\|below:USD` | Act only while the account's available balance (the engine's free margin) is above or below USD. Only where the service prices account margins: `strategy info` says `reads_balance: true`; elsewhere the kit refuses it before asking the service. |
| `--exit target:SYMBOL:up\|down:LEVEL` | After the entry, close this strategy's own share in every market when SYMBOL's mark crosses LEVEL. |
| `--exit profit:PCT` | … at a +PCT% price move of the strategy's own positions over all its markets: unleveraged, before fees and funding (not a return on margin, which a strategy does not have of its own). The preview shows it in dollars too. |
| `--exit after:DURATION` | … DURATION after the entry (`1m` to `90d`). Exits combine: the first one met acts, then the strategy ends. An exit target on a market exactly one entry order trades, on that order's winning side (up for a buy, down for a sell), also becomes that order's take-profit on Reya. |
| `--on-stop keep-others\|close-all` | When a stop-loss or take-profit fires: `keep-others` keeps the strategy running on its other markets, with its exits live, until nothing is left; `close-all` closes its share in every other market and ends it. By default `close-all` when the entry both buys and sells (legs that hedge each other), else `keep-others`. `rules` says which. |
| `--repeat once\|each` | `once` (the default): the entry acts once. `each`: again at every trigger, with the re-arm band below for a price; not with an exit or a `start` trigger. `each_crossing` is the same as `each`. |
| `--allocation` | USD in total, every order of every step. A step the remainder cannot cover is skipped; a strategy without an exit then completes (`allocation_used`). |
| `--expires` | `7d`, `12h`, `90m`, Unix seconds, or an ISO time with a zone (at most 90 days). The strategy completes then: it sends no new orders and cancels its resting orders; its stops stay ([below](#the-protective-stops)). |
| `--bound-bps` | How far from the mark an immediate order may fill, 10–100 bps; 25 by default. |
| `--max-resting` | How many resting orders it may have at once, 1–40; 20 by default. |
| `--account-id` | The Agent account, when more than one is recorded. |
| `--out FILE` | Where to write it; by default the kit's state directory. `--force` replaces a file. |

`draft` sends the parameters to the service (`POST /v1/validate` with
`{"template": {"id": "basket", "version": 1}, "params": {...}}`); nothing is signed or
started. The service expands them into the document and checks it as it checks any document,
against the live markets: an invalid draft returns `ok: false`, `error` and every reason in
`errors`, each with the parameter's `path` (`params.entry.orders.2.market` is the third order's), and writes no
file. Before it writes one, the kit checks the document keeps the envelope (account,
environment, allocation, expiry, bound, resting cap and exactly the markets the orders trade),
every order's market, side, size, price and stop under the ids the template gives them (`o1`…
for immediate orders, `r1`… for resting ones, in the order given), and a price trigger's
level.

The result has `file`, the `document`, `version_hash`, `markets`, `rules` (the strategy as
sentences, one per transition: "In waiting (the start): When the ETH mark crosses up through
3,000: send 3 immediate orders (…), then it moves to done, where it ends."), `orders` (a row per
order: `order`, `type`, `market`, `side`, `usd`, `stop`, and for resting ones `price`,
`post_only`, each with a `text`), `workflow` (all of it in one paragraph),
`warnings`, the `params` sent, and `app_review_url`, the page in the Reya app where the user
reviews the draft and can start it ([below](#in-the-reya-app)). The file is the canonical
document itself (`reya.workflow/2`), so its bytes are what `version_hash` is over; amounts and
prices are decimal strings.

### `validate`

The service checks the document against the live market. On success: `version_hash` and
`canonical` (the document in its canonical form, defaults filled in), `markets`, `rules`,
`orders` and `warnings`. An invalid document returns `ok: false`, `error` and every reason in
`errors`, each with its `code` and `path` (`signals.trigger.level`). An older single-market
document (`reya.workflow/1`) is refused before anything is sent: draft it again.

### `preview`

What the strategy would do right now, from the same evaluator the live strategies use:

- `now`: where the start state's triggers stand. For a crossing, the value against the level
  ("the ETH mark is 2,950; the level 3,000 is 1.69% (169 bps) above it"), or that it is already
  past; for a schedule, the next time; for `start`, that it acts right after the start.
- `markets`: every market a signal or order reads, with its `mark` and `mark_at`.
- `signals`: each value the rules read, with its `value`, `age_secs`, `stale` and a `text`. A funding
  value also has `value_text`, rounded as the Reya app shows funding: `-0.0001%/h (-0.72% a year)`.
- `start_transitions`: each transition of the start state as the service sizes it now:
  `orders` (side, quantity, limit price, the price bound, what it reserves, its stop),
  `resting` (side, quantity, limit, post-only, what it reserves), `closes`, `order_errors`
  (an order that could not be sized now, and why), `reserved_usd`, `already_met`, and a `text`
  for each. Each sized order carries `sizing`: its dollars against what it really sends. The
  service divides an order's dollars by its limit and floors the quantity to the market's lot,
  so $27 at a limit of 2,512.188 sends 0.010 ETH, about $25.12. `sizing` gives
  `requested_notional_usd`, `effective_notional_usd`, the shortfall in dollars and per cent,
  and, when rounding took something off, `next_step_up`: the next quantity, its notional and
  `draft_usd`, the least dollars that reach it. Its `message` is appended to the order's
  `text`. The lots come from the public market definitions; when that read fails, `sizing`
  still compares the two amounts.
- `rules`: the service's own sentence for each transition, in the order the strategy reaches its
  states, `orders` a row per order of the document, and `schedule`: the next three times of
  each schedule.
- `warnings` (below), and `app_review_url`, the same draft's review page in the Reya app.

When the start trigger is already met (`fire_now_needed: true`), `already_triggered` says
so: start with `strategy start --fire-now` to act on the first fresh value, or with
`--wait-for-rearm` to wait until the value moves back past the re-arm band (0.5%) and crosses
again. Preview itself takes neither: what to do with a met trigger is the start's choice. A
level close to the mark can be crossed in the minute between the preview and the start: set it
well away (about 0.5% or more) for a strategy that should wait for a move.

| Warning | Meaning |
| -- | -- |
| `order_without_stop` | An order trades a market with no stop from this strategy. |
| `size_above_balance` | The start state's largest step reserves more than the account's available balance, at 1x: orders may be refused for margin. |
| `budgets_above_balance` | That step plus the remaining budgets of the account's other live strategies is more than the available balance: they share that margin. |
| `many_immediate_orders` | More than 4 immediate orders in one step: after the first 4, about one goes out a second, at prices that may have moved. |
| `market_in_use` | Another live strategy on the account trades one of these markets (`details.run_id`, `details.market`); the start would be refused. `next` names it. |
| `resting_may_open` | A resting order in the closing direction: resting orders cannot be reduce-only, so if the position is gone before it fills, it opens one the other way. |
| `account_markets_over_14`, `resting_over_account_cap` | Across the account's strategies: more than 14 markets, or resting orders past the account's caps. |

A service that prices account margins (`strategy info` says `reads_balance: true`) sends the two
balance warnings itself, against the engine's free margin, and also skips a step the margin
cannot take (`skip_margin`). Elsewhere they are the kit's own: `preview` reads the available
balance (equity less initial margin) with the V2 account reads `query margin` makes, and the
live strategies' remaining budgets from the service, and adds them to `warnings` with
`source: "kit"`; `budgets_above_balance` then only when there is another live strategy.
`balance_warnings_from` says which (`service`, `kit`, or `null` when the kit could not read the
balance), and `available_balance_usd` is the balance the kit read. Never both.

Exits on the price move come with dollars: `profit_exits` has one entry per exit on the
strategy's price move, with `level_pct`, `entry_usd` (what the entry would buy now) and
`profit_usd`, and a `text` such as "exit at a +10% price move, about +$100.00 on $1,000.00 of
entry (unleveraged, before fees and funding)". A row's `take_profit_px` is its take-profit.
| `limit_far_from_mark`, `level_far_from_mark` | A resting price or a trigger level more than 20% from the mark: perhaps a typo. |

### `start`

Checks the document again with the service, then signs `WorkflowStart` with the agent key
and sends it. The signed request binds the document's version hash, the account, the
environment, the hosted signer, the allocation and expiry from the envelope, the mode,
`fireNow`, a nonce and a deadline five minutes ahead. `--label NAME` (up to 60 characters)
names the strategy; it travels beside the signed request, outside the hash, and the app and
`status` show it as plain text. Before signing, the kit refuses:

- any account that `agent init` did not record as the Agent account
  (`PERP_AGENT_ACCOUNT_IDS`), and `REYA_SIGNER=own-key`;
- a recorded account that Reya does not report as an Agent account (`AGENTPERP`), which
  the service would refuse (`NOT_AGENT_ACCOUNT`; `validate` and `preview` refuse it the same
  way). `next` and `agent_account_fix` give the fix: `agent init --account-id <id>`
  switches the kit to one of the wallet's Agent accounts (with several, ask which);
- a service that serves another environment or chain than the kit is configured for, that
  publishes a signing domain or message the kit does not know, or that says it does not run
  `reya.workflow/2`;
- a canonical form from the service that changes anything in your document, or whose
  version hash is not its hash;
- a mode the environment does not allow, an allocation above the pilot cap, and any order
  above the per-order cap (a weighted order counts as its share of the total);
- a start trigger that is already met, unless you chose `--fire-now` or `--wait-for-rearm`.
  The kit asks the service for a fresh preview just before signing; when `fire_now_needed` is
  true and neither was given, it signs nothing and returns `error_code: "ALREADY_TRIGGERED"`,
  `now`, `start_transitions` and a `next` naming both commands.

The result has `run_id`, `label`, `state`, `mode` with `mode_text`, `markets`, `rules`,
`signer` (the agent key's address), `hosted_signer`, the allocation and expiry, `now`,
`status_url`, and the strategy's pages in the Reya app, `app_run_url` and `app_runs_url`
([below](#in-the-reya-app)). A start with `--fire-now` or `--wait-for-rearm` on a met trigger
adds `already_triggered`, saying which it does.

The service refuses a start, and nothing starts, when:

- another live strategy on the account trades one of its markets: `MARKET_IN_USE`, with
  `details.run_id` and `details.market`; `next` names it ("Strategy #41 already trades BTC …:
  stop it first"), and `app_run_url` links it;
- the account already runs as many strategies as it may: `ACCOUNT_STRATEGY_LIMIT`, with
  `details.limit` and `details.live`; `next` lists them;
- hosted runs are not enabled for the wallet: `HOSTED_SIGNER_NOT_ENABLED`. The result adds
  `hosted_signer` and `enable_url`, and the kit opens that page in the browser once
  ([below](#enabling-hosted-runs)); `--no-open` only returns the link.

There are no live edits: to change a strategy, stop it, draft again and start a new one.

### `status`

With a run ID, one strategy:

- `summary`: one line, e.g. "Strategy #7 "ETH breakout" (Paper) is active; it trades
  BTC, ETH and SOL; in waiting since 2026-10-19T00:00:00Z; $1,975.00 of $2,000.00 remaining
  ($25.00 spent, $0.00 reserved); 1 step fired, 0 skips."
- `label`, `state` (`active`, `blocked`, `held`, `completed` or `stopped`), `mode` and
  `mode_text`, `graph_state` (the state it is in), `entered_at` and `final`.
- `markets`: a row per market: `own_qty` (this strategy's own share: its settled fills there,
  up to the live position), `own_entry`, `own_profit_pct` and `own_profit_usd` (the price move
  of that share at the mark, unleveraged, before fees and funding), `position_qty` (the whole
  account's), its stop-loss (`protection`) and take-profit (`take_profit`) and, after a stop,
  its `close`, with a `text`.
- `profit`: the strategy's price move over all its markets (`pct`, `usd`, and `entry_usd`, what
  its own shares cost), and `profit_exits`: each exit on the price move with its level, the move
  now (`current_usd`) and the level in dollars (`target_usd`). All `null` while it holds
  nothing, or a mark or the account is stale. A price move is not a return on margin.
- `resting_orders`: each with its `status` (`placing`, `resting`, `partial`, `filled`,
  `cancelled`, `cancelled_by_owner`, `expired`, `rejected` or `unknown`), `filled_qty` and a
  `text`.
- `signals`: each value now; `next_wake_at`: the next scheduled time; `stale`: while inputs
  are stale nothing acts, and `reason` names why.
- `firings`: one entry per step that fired, with the service's own sentence ("Sent 3 orders:
  2 filled, SOL rejected for margin") and a row per order: "buy BTC: executed 0.0055 at 90,012
  (+1.3 bps from the mark 90,000) for $495.07", or why it did not fill, with its `settlement`.
- `skips`: each step that did not act, with its named `reason` and the service's `text`;
  `skip_counts` by reason.
- `allocation`: total, reserved, spent and remaining; `completion` and `stop`, each with a
  named reason; `rules`; and `app_run_url` and `app_runs_url`.

Without a run ID, `strategies`: every live strategy on the Agent account (active, blocked or
held, and a completed one whose stop still holds a market, `holds_markets`), each with
`label`, `markets`, `state`, `graph_state`, a `text` ("Strategy #7 "ETH breakout" (Live)
trades BTC, ETH and SOL: active, in waiting") and its `app_run_url`; with exactly one live,
`active_run` is its full status. `--all` lists ended ones too, newest first, up to `--limit`
(20 by default, at most 200). When older ones follow, `next_before` names the oldest run listed
and `next` the command for the next page, `strategy status --all --before RUN_ID`.

### `stop`

Signs `WorkflowStop` with the agent key. When it returns, the stop is durable: no further step
acts, and an order already sent completes. `stop --all` stops every live strategy on the
account, one signed stop each; one refusal does not hold up the others, and `stopped` gives
each one's result. A completed strategy that still keeps a stop is left alone by `--all`
(`holding` lists it); stop it by its ID to take its stop down.

- **By default it stops and closes** (`keepPosition: false`). Its resting orders are
  cancelled first. Then, market by market, once its last orders and fills there are done, the
  service closes this strategy's own share: what its fills there added up to, capped at the
  live position, so anything held before, traded by hand or by another strategy stays open. It
  sends up to `close.max_attempts` reduce-only orders, `close.close_retry_secs` apart, each
  priced from a fresh mark, then cancels that market's stop. Each market's `close` follows
  it: `state` (`pending`, `closing`, then `closed`, `nothing_to_close`, `stopped_still_open`
  or `unknown`), `attempts` of `max_attempts`, `closed_qty`, `open_qty`, `next_attempt_at`,
  `simulated` and `text`. `stopped_still_open` means part of the strategy's share is still open
  (`open_qty`): close it by hand. `unknown` means a close order's answer was lost: check the
  position. A paper strategy's close is simulated at the mark (`simulated: true`).
- **`--keep-position`** stops without closing: its resting orders are cancelled, and **the
  positions stay open with no stop order from the strategy**, since its stops are cancelled.
  Treat a market as having no stop from it only once its `protection.state` is `cancelled`.
- Either way `protection.state` reads `cancel_pending` while the cancel is in progress and
  `cancelled` (reason `run_stopped`) once it has succeeded; if it reads `cancel_failed`, the
  stop can still fire: clear it with `cancel_all --symbol <market>` (preview first).

The result has `state`, `stop`, `markets` (each with its `close` and `protection`, read back
just after the stop), `resting_orders`, `note` (what the stop does) and `next`. Stopping a
stopped strategy changes nothing (`already_stopped: true`), and `note` says when that stop
chose otherwise than this one. The result links the strategy's pages in the app, like `start`.

## What a strategy does

- **States.** A strategy is a few states, each with transitions: when an event fires and its
  conditions hold, the transition runs its actions and moves to the next state. A `basket` has
  `waiting` (the start), then `in_trade` when it has exits, then `done`, which is final: the
  strategy completes there once none of its resting orders is left. With `--repeat each` there
  is only `waiting`, which the entry returns to after each step; it runs until it expires, its
  allocation is used or it is stopped. One transition acts per wake-up.
- **Events.** A crossing fires when the value moves from one side of the level to the other
  (touching counts). After it fires or skips, it re-arms once the value is back past the level
  by 0.5% and, for a repeat, 15 minutes have passed. A scheduled time fires if its conditions
  are fresh and hold, at most 300 s late after a gap; a slot more than 300 s late is skipped
  (`schedule_missed`) and never caught up, and a slot never fires twice. Nothing fires on a
  stale value: a mark older than 10 s is stale.
- **One step is all or nothing at the claim.** Before a step sends anything, the service
  reserves every order of it from the allocation, or none: the step is skipped with its reason
  when a condition fails, a needed mark is stale, a market is halted, an order is below its
  market's minimum at the current mark, a stop is already crossed, or the remaining allocation
  cannot cover the step.
- **Partial fills.** Once claimed, each immediate order goes out on its own, as an IOC at the
  market's fresh mark ± the bound, never worse than its claimed limit. An order the exchange
  rejects, that fills nothing or that the market moved past before it went out releases its
  reservation, and the others stand: the step keeps what filled (the service's sentence says
  "Sent 3 orders: 2 filled, SOL rejected for margin"). An order whose outcome is unknown blocks
  the strategy (`blocked`) until the outcome is known; it sends nothing new meanwhile.
- **The rate limit.** The account's strategies share its order pacing: a burst of 4 creates,
  then about 1 a second (cancels 8, then 2 a second). So the first 4 orders of a step go out at
  once and the rest about a second apart, at prices that may have moved (the preview warns
  above 4). An order not sent within 10 s is dropped and releases its reservation.
- **Resting orders.** Placed after the claim, each good until the strategy expires, post-only
  unless drafted with `--no-post-only` (one that would cross is `rejected` and releases its
  reservation). Each reserves its dollars when claimed, then quantity × limit once placed,
  until it fills, is cancelled or expires. They belong to the state the transition went to, and
  are cancelled when the strategy leaves it. The owner can cancel one in the app or with
  `order cancel`: it reads `cancelled_by_owner`. A resting order is never reduce-only (the
  venue takes reduce-only only on immediate orders), so one in the closing direction opens a
  position the other way if the position is gone before it fills; the preview warns
  (`resting_may_open`).
- **Shared margin.** Every strategy on the Agent account, and every order you place there
  yourself, trades on the account's one margin balance. A strategy's allocation is a budget
  for its own orders, not margin set aside: a strategy can be refused for margin (the order is
  `rejected`) because of another strategy's position or your own. Several strategies with
  allocations above the available balance get the `budgets_above_balance` warning.

### One market, one strategy

Each market belongs to one live strategy on an account: a start whose markets include one
that another live strategy trades is refused (`MARKET_IN_USE`), and the preview warns first
(`market_in_use`). A completed strategy still holds a market while the stop it armed there is
live. To free a market, stop the strategy that holds it. Your own orders in that market are
not affected, and they share the position: the strategy's own share counts only its fills.

## The protective stops

An order with a `STOP` asks the service for one stop-loss on the whole position in that
market, and one with a `TAKE_PROFIT` for one take-profit, signed by the hosted signer (at most
one of each per market in a strategy, from orders on the same side). The venue pairs them
one-cancels-other: whichever fires cancels the other (`oco_sibling_fired`). A take-profit is on
the winning side of the price the order fills at (above it for a buy, below for a sell) and
only ever reduces the position. Everything below applies to both; status shows the stop-loss
in `protection` and the take-profit in `take_profit`, and activity `protection` events carry
`protection_kind` (`stop_loss` or `take_profit`):

- **When it arms.** Never at start. Once one of the strategy's fills in that market settles,
  the service arms the stop (`arming`, then `armed`). It covers the whole position there.
  A market with no settled fill reads `pending`.
- **The order.** A stop-loss on the closing side, triggered at your price on the market's
  tick, rounded away from the mark, with its limit at the edge of the venue's band around
  the trigger. When the mark touches the trigger, the venue closes the position with an IOC
  order down to that limit. `protection` shows `trigger_px`, `limit_px`, `order_id`,
  `client_order_id` and `armed_at`.
- **When it fires.** The strategy does what its `on_stop` says (`rules` names it). With
  `close_all` it closes its share in every other market (a close with cause `protection`),
  cancels its resting orders and completes (`protection_fired`, with the `market`). With
  `keep_others` it keeps running on its other markets, with its exits, schedules and other
  stops live, and completes only once nothing is left; the fired market's stops arm again if
  it holds a share there again. `protection.fired` gives the quantity the close executed and
  its average price. The close is IOC, so it can fill only part of the position, or nothing
  when the book has nothing inside the limit. Read the position back after a stop fires.
- **When the strategy stops.** See `stop`: each market's own share is closed first (unless
  the stop kept the positions), then that market's stop is cancelled (`cancel_pending`, then
  `cancelled`; a close that emptied the position retires it, `position_closed`).
- **When the strategy completes or expires.** It keeps its stops: expiry stops new orders,
  never protection. A stop stays on its position, watched, until it fires, the venue retires
  it, the strategy is stopped (`strategy stop RUN_ID`, which cancels its stops), or the owner
  clears it with `cancel_all --symbol <market>`. Status confirms retained protection only for
  `armed`: `arming` and `unknown` mean protection is not confirmed, while `cancel_pending`
  means cancellation is in progress and the stop may still fire until it is confirmed.
- **While it cannot fire.** The venue skips stops while the mark is stale or the venue is
  halted; `protection.suppressed` then reads `mark_stale` or `venue_halted`.

What `protection.reason` means when a stop is not armed or was taken down:

| `state`, `reason` | What happened | Next |
| -- | -- | -- |
| `not_armed`, `account_has_stop` / `account_has_take_profit` | The account already has a stop-loss (or take-profit) on this market, which the service never touches. It covers the whole position, the strategy's fills included, at its own price. | The strategy keeps going and tries again after its next settled fill there. |
| `not_armed`, `earlier_run_stop` | An earlier strategy's stop on this market is still armed and covers the position. | As above. |
| `not_armed`, `outside_band` / `account_suspended` / `refused` | The venue refused the stop. | As above. |
| `cancelled`, `position_closed` / `oco_sibling_fired` | The venue took the stop down: nothing left to close, or the account's take-profit fired. | Armed again after the next settled fill. |
| `cancelled`, `risk_rejected` | The stop fired and the venue's risk checks refused the close. **The position is open with no stop.** | Armed again after the next settled fill. |
| `cancelled`, `cleared_by_owner` | The owner's `cancel_all` cleared it. | This strategy arms no other stop there. |
| `cancelled`, `run_stopped` | The strategy stopped and the service cancelled it. | — |
| `cancel_failed`, `hosted_signer_revoked` | The owner revoked the hosted signer, so the service can no longer cancel the stop. It can still fire, and its fill would not settle. | Clear it with `cancel_all --symbol <market>`. |
| `unknown` | The service lost the answer to an arm twice and never saw the stop on the venue. The strategy is blocked until it is seen. | `strategy status`; the service keeps reading the venue. |

`paper` strategies simulate their stops.

## Enabling hosted runs

The hosted signer places a strategy's orders, so every start, paper ones included, needs the owner
to have approved it once, the same kind of approval as the agent key. In the Reya app this
is the last step of the setup page that `agent init` opens, **Enable hosted runs**: one
signature from the owner wallet, with the hosted signer's address shown to compare with
`hosted_signer`. The page reads the address from the service, and offers the step once the
agent key is approved and the Agent account exists and is funded; before that it finishes
the step still open.

`agent init` offers this step once setup is `ready` (`hosted_runs.offer`): `agent init --hosted-runs now
--until-ready --timeout 100` keeps waiting on the same setup page through it, and `--hosted-runs later` records the answer
so it is not offered again. Setup is `ready` either way; see the
[command reference](command-reference.md#agent-setup-agentpy).

`enable_url` is that page's link, the same one `agent init` gives once the Agent account is
recorded:
`/agents/setup?agent=<agent key>&owner=<owner>&account=<Agent account>#access=<code>`.
Like the other app links, the access code is only in the part after `#`.

- `strategy info` checks the approval the way `agent status` checks the agent key's: the
  owner's approved signers from the trading API's API-wallet status, which the app's setup
  page reads too. With `hosted_signer_enabled: false` it returns `enable_url`, and opens
  nothing. `strategy info --open` also opens it, as a refused `start` does.
- A refused `start` returns `enable_url` and opens it, like the setup links: once, and not
  again while it is open. A link opened in the last 30 minutes, by `start` or by `agent init`,
  counts as open, and so does the setup link without `account` when the wallet has a single
  Agent account (`AGENTPERP`, whatever its name) and it is the recorded one: that page shows
  the same account. A wallet may have several; with more than one, the page needs `account`. The result then says `link_already_opened: true`. When no browser can open on this
  machine (an SSH session, no display, WSL without Windows access), it says `opened: false`
  and `browser_unavailable`, and prints the link for the user to open on any device where
  their wallet is.

Once the owner has approved, run the same `start` again. Revoking the hosted signer on the
app's API page stops its strategies (`hosted_signer_revoked`).

## In the Reya app

`draft` and `preview` link the draft's review page, and `start`, `status` and `stop` the
strategy's pages, in the Reya app for the deployment the kit is configured for (the same app as
the `agent init` setup page):

| Field | Page |
| -- | -- |
| `app_review_url` | `/agents/strategies?draft&agent=<agent>&owner=<owner>#access=<code>&doc=<document>`: the draft, shown from the service's own preview (its states as sentences, a row per order, the markets it may trade, warnings), with the mode, the pilot caps, what its stops do and what Stop does. The owner, connected, starts it there: the app signs `WorkflowStart` with the owner's one-click trading wallet, or the owner's wallet, in Paper mode unless they choose Live and the service allows it, and the page then becomes the strategy's page. Any other wallet is asked to switch. |
| `app_run_url` | `/agents/strategies?id=<run_id>#access=<code>`: one strategy, with its state, markets, resting orders, firings, skips and history, and Stop for the connected owner. |
| `app_runs_url` | `/agents/accounts?account=<account_id>#access=<code>`: the Agent account's page, with its strategies (each opening its strategy's page), funds and positions. Links from kits before 0.14.1 point at `/agents/workflows?account=…`; the app redirects them here. |

Like the setup page, these pages open only with the kit's access code in the part after
`#`, which the browser never sends to the server; the code is never in the query. The review
link's document is there too: the draft as compact JSON, base64url without padding, up to
the kit's 12 KiB document cap. The app treats it as untrusted, refuses a link that is
oversized or damaged, and shows nothing about what the draft would do except the service's
preview of it. `agent` and `owner`, as on the setup link, only let the page link the setup
page (to approve the hosted signer) and ask for a wallet switch. Give the user the whole link:
opened without the fragment, in a tab that has not opened a kit link before, the page only
says to start from the assistant. A start or stop whose outcome is unknown still links the
pages, which show what the service recorded.

A strategy started in the app is the same as one started by the kit, except that the owner's
wallet signed it rather than the agent key: `status` lists it, and `stop` stops it. A draft
reviewed in the app is never stored anywhere: Discard there only clears the page.

All three are `null` when no app is known for the deployment (set `REYA_APP_URL`, as for
the setup links) or when `REYA_APP_URL` is not a plain `https` URL; that never fails the
command.

## Named reasons

Skips (a step that did not act):

| Code | Reason |
| -- | -- |
| `skip_level` | a condition did not hold |
| `skip_no_value` | a value it reads has none yet (the strategy holds nothing there) |
| `skip_stale` | a value it needs was stale |
| `skip_superseded` | an earlier transition acted on the same wake-up |
| `skip_firing_held` | firing held by an operator |
| `skip_collar_too_tight` | market price band narrower than the bound |
| `skip_below_minimum` | an order below the market minimum |
| `skip_price_out_of_range` | price outside the venue's range |
| `skip_stop_crossed` | a stop it would arm is already crossed |
| `skip_take_profit_crossed` | a take-profit it would arm is already crossed |
| `skip_budget` | the remaining allocation cannot cover the step |
| `skip_margin` | the account's margin cannot take the step's orders (where the service prices margins) |
| `schedule_missed` | scheduled times passed while it could not act; they are never caught up |
| `crossed_while_stale` | crossed while data was stale, not acted on |

A code the kit does not name yet is shown as itself, next to the service's own text, which
always says what happened.

Stale inputs: `mark_missing`, `mark_stale`, `mark_from_future`, `market_halted`, `account_stale`,
`funding_stale`, `permissions_stale`, `restarted`. A missed move is reported, never acted on
late.

Stops: `user` (a signed stop), `permission_revoked` (the starter's trading permission was
revoked), `hosted_signer_revoked`, `outcome_unknown`, `operator`. Completions: `final_state`
(it reached its final state with no resting order left), `allocation_used`, `expired` (no new
orders, resting orders cancelled, its stops stay), `protection_fired` (a stop-loss or
take-profit it armed fired, with the `market`; with `keep_others` only once nothing is left).

Order outcomes: `filled` and `partial` ("executed at"), `past_bound` and `zero_fill` (not
filled within the bound), `rejected`, and `unknown`: the strategy is blocked, keeping its
reservations and stops, until the order's outcome is known.

## Private run reads

A run shows the whole strategy before it fires, so the service is making run reads private:
`GET /v1/runs/{id}`, its activity and `GET /v1/accounts/{id}/runs` will need a read grant.
The kit already sends one on those three reads, and on nothing else:

- **What it is.** An EIP-712 `ReadGrant` under the same domain as start and stop
  (`Reya Workflows`, version `1`): `verifyingChainId`, `environment`, `owner`,
  `accountIds` (the recorded Agent accounts), `expiresAt` (one hour) and a random 16-byte
  `salt`. It is signed with the agent key, an approved delegate of the owner; never with the
  owner's key, so with `REYA_SIGNER=own-key` no grant is sent.
- **How it travels.** `Authorization: Bearer <token>`, where the token is base64url without
  padding of the JSON `{"grant": {...}, "signature": "0x..."}`. Integers are decimal
  strings, as in start and stop.
- **Reuse.** The grant is kept in the state directory (`workflow-read-grant.json`,
  owner-only) and reused by later commands until 80% of its hour has passed, then replaced.
  Revoking the agent key in the app ends its grants too.
- **Until the service requires grants.** Reads work without one. If something in front of
  the service (a gateway) refuses a read that carried a grant, without the service's own
  error, the kit asks once more without it and drops the kept grant, so a grant never costs
  an answer. The service's own refusals stand and are never asked again without the grant:
  `INVALID_READ_GRANT` (the grant was not accepted; the kept grant is dropped),
  `READ_GRANT_REQUIRED` (none was sent) and `RUN_NOT_FOUND` (also for a run the grant does
  not cover; the kept grant stays). The grant never appears in a result or an error.

## Errors and lost answers

A refusal from the service carries `error_code`, `retryable`, `http_status`,
`request_id` (quote it when reporting a problem), `retry_after_s` when the service asks for
a pause, and `next` for the common codes:

| Code | What to do |
| -- | -- |
| `HOSTED_SIGNER_NOT_ENABLED` | Hosted runs are not enabled for the wallet: the owner approves the hosted signer on the setup page's Enable hosted runs step (`enable_url`, opened once), then starts again. |
| `NOT_AGENT_ACCOUNT` (and validate's or preview's `not_agent_account` error) | Nothing was signed or started. Follow `next`: `agent init --account-id <id>` switches the kit to the wallet's Agent account (`agent_account_fix`); `agent status` names it. |
| `NOT_PERMITTED` | Run `agent status`. |
| `MARKET_IN_USE` | Another live strategy on the account trades that market (`next` names it): stop it first, or draft this one without that market. |
| `ACCOUNT_STRATEGY_LIMIT` | The account runs as many strategies as it may (`next` lists them): stop one first. |
| `NONCE_TOO_LOW`, `REQUEST_EXPIRED` | Nothing happened; run the same command again (check the clock for `REQUEST_EXPIRED`). |
| `MODE_NOT_ALLOWED`, `ABOVE_PILOT_LIMIT` | Use a mode and amounts `strategy info` allows. |
| `ALREADY_TRIGGERED` (the kit, before signing) | The start trigger was met since the preview; nothing was signed. Ask the user: `start --fire-now` or `start --wait-for-rearm`. |
| `RATE_LIMITED`, `BUSY`, `NOT_READY`, `ACCOUNT_STATE_UNAVAILABLE`, `MARKET_DATA_UNAVAILABLE` | Retryable: wait `retry_after_s`, then try again. |
| `INVALID_READ_GRANT`, `READ_GRANT_REQUIRED` | A run read without a grant the service takes: run `agent status` and re-approve the agent key if it asks, and check this computer's clock ([private run reads](#private-run-reads)). |

If a start's answer is lost (a timeout, or an answer the kit cannot read), the kit reads the
account's newest active strategies once, as many as may be live: when one is this document,
started by this agent key, the result says `confirmed_by_read: true`. Otherwise it returns
`outcome: "unknown"` and resends nothing; run `strategy status` before starting again. A stop
whose answer is lost is confirmed the same way, and running `stop` again is always safe.

## Common shapes

Each of these is the `basket` template, so `draft` builds it from flags; each command below was
checked by the service. Strategies trade perps only. Every order must clear its market's minimum
at the mark (`query market info`): $50 of BTC is refused while BTC's minimum, 0.001 BTC, is about
$80. The allocation counts every fill, buys and sells alike.

- **Funding spread.** Sell the market with the higher funding and buy the one with the lower, in
  equal dollars, while both stay apart; close after two weeks. Positive funding means longs pay
  shorts, so the short leg collects where funding is positive and the long leg where it is
  negative. Check every hour with `every`, not `start`: a `start` trigger checks its conditions
  once, and if they fail then it never trades. Put each threshold inside today's value, nearer
  zero (for a market at −0.0016 %/h, `below:-0.001`), never at it: a market pinned at a rate never
  goes past it. Many markets often sit at the same base rate, and a spread between two of those
  pays nothing.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger every:1h --if funding:aave:below:-0.001 --if funding:eth:above:0.0005 \
    --order aave:buy:100 --order eth:sell:100 --exit after:14d --allocation 200 --expires 15d
  ```

- **Static grid.** Resting buys below the mark and sells above it, placed once. A fill is not
  replaced; the strategy ends once none rests.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger start \
    --resting eth:buy:25:-50bps --resting eth:buy:25:-100bps --resting eth:buy:25:-150bps \
    --resting eth:sell:25:+50bps --resting eth:sell:25:+100bps --resting eth:sell:25:+150bps --allocation 150 --expires 14d
  ```

- **Grid that follows the price.** The same ladder placed again around the mark on a schedule:
  each time, what still rests is cancelled and the ladder is placed afresh.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger every:4h --repeat each \
    --resting eth:buy:25:-50bps --resting eth:buy:25:-100bps --resting eth:sell:25:+50bps --resting eth:sell:25:+100bps \
    --allocation 500 --expires 14d
  ```

- **Recurring buy (DCA).** A schedule with `--repeat each`; the allocation is each buy times the
  number of buys.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger at:mon@09:00 --repeat each --order btc:buy:100 --order eth:buy:50 \
    --allocation 600 --expires 30d
  ```

- **Dip buy and breakout.** A price trigger with an immediate order, its stop and, if wanted, its
  take-profit.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger price:eth:down:2250 --repeat each --order eth:buy:50:2100 \
    --allocation 200 --expires 14d
  python -m agent_kit.scripts.strategy draft --trigger price:btc:up:82000 --order btc:buy:100:79000:86000 \
    --allocation 100 --expires 7d
  ```

- **Scale-in ladder.** Resting buys at falling offsets, closed at a price move or after a time.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger start \
    --resting sol:buy:25:-200bps:90 --resting sol:buy:25:-400bps --resting sol:buy:25:-600bps \
    --exit profit:3 --exit after:14d --allocation 75 --expires 15d
  ```

- **Pair.** A price trigger on one market; buy one and sell the other in equal dollars.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger price:eth:down:2250 --order eth:buy:50 --order sol:sell:50 \
    --exit profit:3 --exit after:5d --allocation 100 --expires 8d
  ```

- **Time window.** Buy at a daily time and close some hours later, once.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger at:daily@13:30 --order spy:buy:50 --exit after:6h \
    --allocation 50 --expires 7d
  ```

- **TWAP.** The same small order every few minutes until the allocation is used.

  ```bash
  python -m agent_kit.scripts.strategy draft --trigger every:5m --repeat each --order eth:buy:25 --allocation 250 --expires 1d
  ```

Not a strategy yet: indicators (RSI, moving averages), a trailing stop, a ratio or spread between
markets as the trigger, a grid that places the opposite order after each fill (a fill moves the
strategy on, which cancels the rest of its ladder), and spot, so no spot-perp cash-and-carry.

## Beyond what a strategy does

A strategy runs a basket of orders on one trigger, with conditions, resting orders, stops and
exits, which covers static grids and ladders ([common shapes](#common-shapes)). For rules it
cannot express (a grid that places the opposite order after each fill, trailing stops,
indicators, pairs by ratio, sizes that follow a hedge, loops through earlier states, signals from
outside Reya), write the rule yourself against the API: [from strategies to the
API](from-workflows-to-the-api.md) maps each part of a strategy to API orders and points to
runnable examples.

## Environments

The service for each deployment:

| `REYA_API_URL` | Strategy service | App pages |
| -- | -- | -- |
| `https://api.reya.xyz/v2` | `https://api.reya.xyz/workflows` | `https://app.reya.xyz` |
| `https://api-devnet.reya-cronos.network/v2` | `https://api-devnet.reya-cronos.network/workflows` | `https://app-devnet-cf.reya.xyz` |

The service is a path on the same API host: the kit sends `/workflows/v1/...` there, and the
host's gateway passes `/v1/...` on to the service.

Set `REYA_WORKFLOWS_URL` to use another service: its base URL, an origin with an optional base
path (`https://api.reya.xyz/workflows`), without `/v1`, credentials, a query or a fragment. Set
`REYA_WORKFLOWS_ENVIRONMENT` (`mainnet`, `testnet` or `devnet1`) for a custom `REYA_API_URL`.
`REYA_APP_URL` sets the app for the page links.
