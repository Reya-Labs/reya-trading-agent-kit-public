# Changelog

## 0.15.0 — 2026-10-26

For users: Watch only is gone. Strategies run in Paper or Live, and a new strategy starts in Paper (its fills and stops simulated, nothing sent) unless you ask for Live.

Watch only (`shadow`) added nothing that Paper does not, so the kit drops it, as the Reya app and the strategy service do. Live still needs the user to ask for it.

- **Two modes.** `strategy start --mode` takes `paper` (Paper, now the default) or `production` (Live); `--mode shadow` is refused before anything is signed. `mode_text`, summaries and the account's list name them Paper and Live.
- **Paper by default.** A start without `--mode` signs a Paper start: the service simulates the strategy's fills, stops and closes into a labelled ledger and sends no orders. A Paper strategy's stop closes its simulated share, so `stop` follows its closes (`pending`, then `closed` or `nothing_to_close`, each marked simulated) where a Watch only one answered `nothing_to_close` at once; `--keep-position` still keeps it.
- **Gone.** The kit no longer reads the Watch only answers: the `shadow` order outcome ("recorded and not sent"), the stop it would have armed (`not_armed`, reason `shadow`) and its empty close.
- **During the service's rollout.** The service stops accepting `shadow` in its next release, refusing it as `INVALID_REQUEST`. Until then its `/v1/info` may still list it; the kit leaves it out of `modes`. A strategy read that still names it shows the mode as the service spells it (`shadow`) instead of failing.
- **Docs.** `docs/hosted-workflows.md`, `docs/from-workflows-to-the-api.md`, the README and SKILL.md describe Paper and Live only.
- **Tests.** The start request vectors are re-copied from the service unchanged: their base case now signs a Paper start, and a new case, "shadow is not a mode", expects `INVALID_REQUEST`. The fake service allows Paper by default, refuses any other mode as the service will, and opens every close `pending`. New tests cover the default, `--mode shadow`, a mode the kit does not know and `/v1/info` still listing `shadow`.

Not exercised live: the offline tests cover the change against a fake of the strategy service. Both environments allow Paper starts today, so the new default works before the service's release.

## 0.14.2 — 2026-10-25

For users: when you ask for all your strategies, your assistant now says when there are older ones than it listed and can fetch them, and a strategy preview reads its rules in the order the strategy follows them.

Two fixes from a live run of the strategy commands on devnet.

- **`strategy status --all` pages.** It lists `--limit` strategies (20 by default), newest first, and used to drop the service's cursor, so an account with more ended there without saying so. It now returns `next_before` when older strategies follow, with a `next` naming the command for the next page, and takes `--before RUN_ID` (with `--all` only).
- **Preview rules in order.** The service lists a preview's states by name, so `rules` read `in_trade` before `waiting`. The kit now orders the service's sentences the way the strategy reaches its states, from the start; a state the strategy never reaches comes last. `draft`, `validate`, `start` and `status` already did.
- **Tests.** The fake service lists preview states by name, as the live one does; new tests cover the order, the paging and `--before` without `--all`.

Checked live on devnet before the fix: the service's paging (`next_before`) and its preview states by name. The fix itself is covered by the offline tests.

## 0.14.1 — 2026-10-25

For users: the Reya app link your assistant gives for an account's strategies now opens your Agentic Account's page, where its strategies, funds and position are, instead of the strategies list the app no longer has.

The Reya app retired its "Hosted strategies" list page (`/agents/workflows`). Each strategy now shows in its Agentic Account, on `/agents/accounts`, with its funds and position beside it.

- **`app_runs_url`** (from `strategy start`, `status` and `stop`) is `/agents/accounts?account=<id>#access=<code>`. The app opens that account, and the access code stays in the fragment as before.
- **Older kits.** Links printed by earlier versions, `/agents/workflows?account=<id>#access=<code>`, keep working: the app redirects them to the same account on the new page.
- **Unchanged.** `app_run_url` (one strategy, `/agents/workflows/run?id=…`) and `app_review_url` (a draft, `/agents/workflows/run?draft…`), and the field names themselves.
- **Docs.** `docs/hosted-workflows.md` ("In the Reya app") and SKILL.md describe the new page.

Not exercised live from the kit: this changes one link's path, checked by the offline tests. The app side is live: on devnet the old link and the new one both open the account they name, with the access code kept.

## 0.14.0 — 2026-10-25

For users: a strategy's orders can carry a take-profit beside the stop, its profit exit reads as a price move with dollars beside it, you choose what a fired stop does to the other legs, and strategies run for up to 90 days.

The strategy service's follow-up review, on top of 0.13.0. Nothing changes for a strategy drafted without the new flags, except how its sentences read and what a fired stop does by default.

- **Take-profit per order.** `--order` and `--resting` take `:STOP:TAKE_PROFIT` (`eth:buy:1000::3200` for a take-profit alone), sent as the template's `take_profit`: a whole-position take-profit on Reya, armed with the stop once the market's first fill settles, one-cancels-other with it. An exit target on a market one entry order trades, on that order's winning side (up for a buy, down for a sell), becomes that order's take-profit too, and the kit's expansion check expects it. Rows, `rules`, status and the stop answer show it (`take_profit`, `take_profit_px`), and activity `protection` events carry `protection_kind`.
- **What a fired stop does.** `--on-stop keep-others|close-all`: keep the strategy running on its other markets until nothing is left, or close its share in every other market and end. By default `close-all` when the entry both buys and sells (legs that hedge each other), else `keep-others`; the expansion check expects the default, and `rules` says which.
- **A price move, with dollars.** The exit on the strategy's own profit % reads as a price move of its own positions, unleveraged, before fees and funding, never as profit alone. The preview's `profit_exits` gives each such exit in dollars at the planned entry; status gives the strategy's `profit` (percent, dollars and the entry value), `profit_exits` with the move now, and each market's `own_profit_usd`.
- **90 days.** `--expires`, `every:` periods and `after:` exits go up to 90 days.
- **Margin.** Where the service prices account margins (`strategy info` says `reads_balance`), it sends `size_above_balance` and `budgets_above_balance` itself, skips a step the margin cannot take (`skip_margin`), and takes `--if balance:above|below:USD`. Elsewhere the kit keeps computing the two warnings itself and refuses the balance condition; `balance_warnings_from` says which, never both.
- **New codes.** Skips `skip_take_profit_crossed` and `skip_margin`; the stop reason `account_has_take_profit`; the service's validation codes for take-profits are relayed with their paths.
- **Tests.** The seven template examples are re-copied from the service unchanged (four have new hashes: two hedged baskets now close all legs on a fired stop, and two entries with an exit target now carry a take-profit), and each still drafts from its flags byte for byte. New tests cover the take-profit spellings, the derived take-profit and `on_stop`, the balance condition gate, which side sends the balance warnings, the price-move dollars, `protection_kind` and the 90-day cap.

Not exercised live: the offline tests cover every change against a fake of the strategy service; the devnet check follows once the service's follow-up release is deployed there.

## 0.13.0 — 2026-10-24

For users: strategies can now trade several markets at once: a basket of orders on a price, a schedule or at start, with resting orders, a stop per market and exits. An account runs up to 3, one per market. strategy draft takes new flags; draft older strategies again.

Hosted strategies move to the service's second strategy format, `reya.workflow/2`, and the first one is gone everywhere: the kit drafts, validates, previews, starts and reads only the new format. The kit no longer writes a strategy document. `strategy draft` fills the service's `basket` template, the service expands and checks it, and the kit keeps the document the service returns once it has checked that it is what the flags asked for.

- **Drafting.** `strategy draft` takes:
  - `--trigger price:SYMBOL:up|down:LEVEL`, `every:PERIOD[@ANCHOR]`, `at:daily@HH:MM`, `at:mon..sun@HH:MM` or `start`;
  - a repeated `--order SYMBOL:buy|sell:USD|WEIGHT%[:STOP]` (immediate, IOC within the bound) with `--total USD` for weights;
  - a repeated `--resting SYMBOL:buy|sell:USD|WEIGHT%:PRICE|±OFFSETbps[:STOP]` (post-only unless `--no-post-only`);
  - a repeated `--exit target:SYMBOL:up|down:LEVEL`, `profit:PCT` or `after:DURATION`;
  - a repeated `--if funding:SYMBOL:above|below:PCT_PER_HOUR` or `position:SYMBOL:above|below:QTY`;
  - `--repeat once|each` (`each_crossing` still means `each`; the default is now `once`), and `--max-resting N`.

  `--allocation`, `--expires`, `--bound-bps`, `--account-id`, `--out` and `--force` stay. `--symbol`, `--direction`, `--level`, `--side`, `--notional`, `--funding`, `--position` and `--stop-px` are gone.
- **The draft is checked twice.** The service checks it against the live markets, and an invalid one returns every reason with the flag's path (`params.entry.orders.2.market`) and writes no file. The kit then checks the expansion keeps the envelope, every order's market, side, size, price and stop under the template's ids (`o1`…, `r1`…), and a price trigger's level, before it writes the file. The file is the canonical document: its bytes are what the version hash is over. `check_canonical` knows the new format's defaults.
- **Sentences and rows.** `draft`, `validate`, `preview`, `start` and `status` give `rules` (each transition as a sentence) and `orders` (a row per order). `preview` gives the markets and signals now, each start transition's orders, resting orders, closes and order errors as the service sized them (each with `sizing` against its dollars), the next scheduled times, `fire_now_needed` and the service's warnings (`market_in_use` names the strategy in the way). `status` gives the state the strategy is in, a row per market (its own share, entry, profit, position, stop and close), its resting orders, its signals, and each step's orders under the service's sentence.
- **Several strategies per account.** `strategy status` without an ID lists every live strategy with its label, markets and state. `strategy stop --all` stops each live one (stop and close unless `--keep-position`). `strategy start --label NAME` names one. `MARKET_IN_USE` and `ACCOUNT_STRATEGY_LIMIT` get a `next` naming the strategy in the way, or the account's live ones; `ACCOUNT_HAS_ACTIVE_RUN` is gone. A lost start answer is confirmed among as many live strategies as the account may run.
- **Caps.** The pilot's per-order cap is checked on every order before signing, a weight as its share of the total.
- **Balance warnings.** The service has no balance source, so `preview` adds `size_above_balance` and `budgets_above_balance` itself (`source: "kit"`): the start's largest step against the account's available balance (equity less initial margin, read as `query margin` reads it), and that step plus the other live strategies' remaining budgets. `available_balance_usd` shows the balance read; when it cannot be read, there are no balance warnings.
- **Docs.** SKILL.md and the hosted strategies page drop "one market" and "one active run per account", and add partial fills, shared margin, the rate limit and one strategy per market.
- **Tests.** The fake service is v2 only and expands the template by replaying the service's own examples, copied unchanged into `agent_kit/tests/fixtures/workflow_v2_examples` (seven, including strategies on separate markets and a resting order at an offset). Each example drafts from its flags to the service's canonical JSON byte for byte, and the kit's hash and canonical check agree with every one. The first format's hash vectors are deleted; the signed-request vectors are unchanged.

Not exercised live: the offline tests cover drafting every example, the checks, the rendering and the several-strategies flow against a fake of the strategy service; the devnet check follows once the service's new format is deployed there.

## 0.12.43 — 2026-10-24

For users: a wallet can now have several Agent accounts, and the docs say so. With more than one, your assistant asks which one the kit trades. Nothing else changes.

Docs only. A wallet may have several Agent accounts (the app's setup page can now open another, and its "New Agentic Account" does), so the README and docs no longer speak of the owner's one Agent account. The kit's behaviour is unchanged: it trades only the account it records, several `AGENTPERP` accounts return `ambiguous_new_accounts`, and `agent init --account-id <id>` chooses or switches.

- **README** (setup step 2): with more than one Agent account, the assistant asks which one the kit trades.
- **`docs/hosted-workflows.md`**: the fix switches to one of the wallet's Agent accounts; a setup link without `account` shows the same page only when the wallet has a single Agent account.
- **`docs/command-reference.md`**: `agent_account_fix.command` is for a wallet with a single Agent account; `candidates` for several.

## 0.12.42 — 2026-10-24

For users: if the kit trades an account that hosted strategies refuse, it now tells you before anything is signed and gives the one command that fixes it. agent init --account-id switches the kit to another of your Agent accounts without editing .env.

Hosted strategies run only on an Agent account (the account Reya reports as `AGENTPERP`). An earlier kit could record another sub-account, for example one named "Agent account" before that type existed. Then `strategy validate` and `strategy preview` passed, the signed start was refused, and the refusal said to run `agent status`, which said `ready`. Switching meant editing two `.env` lines by hand.

- **Switching.** Once an account is recorded, `agent init --account-id <id>` switches the kit to `<id>` when it is one of the wallet's Agent accounts.
  - The kit rewrites `PERP_AGENT_ACCOUNT_IDS` and `PERP_ACCOUNT_ID_1` itself and keeps the agent key. The result has `previous_agent_account_id` and the `agent_account_switched` action.
  - The previous account keeps its positions, orders and any running strategy.
  - Any other account is refused, and nothing changes. A wallet may now have several Agent accounts, so this is also how you move between them.
- **The fix, named.**
  - When the recorded account is not an Agent account, `agent_account_warning` names the exact command (`agent init --account-id <id>`). With several Agent accounts it says to ask which one; with none, it points to the setup page that creates one.
  - `agent_account_fix` carries the same fix as data (`command`, `candidates` or `url`).
  - `agent status` still says `ready`, since trading works on that account, but `hosted_runs.status` is now `not_agent_account` instead of offering or reporting hosted runs.
- **Before signing.**
  - `strategy validate`, `preview` and `start` check the recorded account's type with the same V2 accounts read `agent status` makes. They refuse a non-Agent account before the service is asked, with `error_code: NOT_AGENT_ACCOUNT`, the warning and the fix.
  - `strategy info` puts the fix first.
  - The strategy service now refuses such a document in validate and preview too (`not_agent_account`), and its signed-start refusal `NOT_AGENT_ACCOUNT` gets the same `next`. When the accounts read fails, `next` says `agent status` names the command.
  - `strategy draft` stays local and reads nothing.
- **Tests.** Switching:
  - an untagged record to the wallet's Agent account, and between two Agent accounts;
  - every refused target leaves `.env` untouched;
  - the agent key line is kept;
  - running it twice changes nothing.

  Strategy commands:
  - refusal before any validate, preview or start reaches the service;
  - the service's `not_agent_account` answers, from a service with and without that check;
  - `info`'s fix for one, several or no Agent accounts.

Not exercised live: the offline tests cover switching and every refusal; the devnet check (record an untagged sub-account, see the refusal before signing, switch with one command, start Watch only) follows once the service's check is deployed.

## 0.12.41 — 2026-10-24

For users: hosted workflows are now called strategies, as in the Reya app, and your assistant says strategy too. The commands are strategy draft, strategy start and so on; the old workflow commands still work.

The Reya app calls an automated trading rule a strategy and never a workflow. The kit said workflow everywhere, so someone using both saw two names for one thing. Only names change: the service, the signed requests, the output fields and the documents stay the same.

- **Commands.** `python -m agent_kit.scripts.strategy` is the command, with the same subcommands and options. `python -m agent_kit.scripts.workflow` stays as an alias and runs the same code. `--help` and the failure line name the command that ran, and every `next` hint gives the `strategy` command.
- **Words.** Output text, refusals and `--help` say strategy and "the strategy service". SKILL.md tells the assistant to say strategy, never workflow ("Strategy #7" for run 7, as in the app), and to give the `strategy` commands. The README and docs follow. Doc file names stay (`docs/hosted-workflows.md`, `docs/from-workflows-to-the-api.md`), so existing links keep working.
- **The service's own sentences** pass through as the service writes them: it says strategy itself now. Events stored before that keep their wording.
- **Unchanged.** These keep their names:
  - output fields (`workflow`, `workflow_id`);
  - `WorkflowStart` and `WorkflowStop`;
  - `REYA_WORKFLOWS_URL` and `REYA_WORKFLOWS_ENVIRONMENT`;
  - the EIP-712 domain "Reya Workflows";
  - the app's `/agents/workflows` pages;
  - the state directory's `workflows` folder.
- **Tests.**
  - `strategy` and `workflow` run the same commands, and each names itself in a failure.
  - The hints give the `strategy` commands.
  - CI smoke-tests both entry points' `--help`.

Not exercised live: this changes names and wording only, checked by the offline tests; no run was started for it.

## 0.12.40 — 2026-10-24

For users: nothing changes in what you read. The workflow service now says Watch only in its own messages, so the kit no longer rewords them.

0.12.37 reworded the workflow service's "in shadow mode" and "a shadow run" in event and protective-stop texts. The service now writes "in Watch only mode" and "a watch-only run" itself, so the kit passes its texts through unchanged again.

- **Removed** `service_text` and `protection_view`: `recent_events[].text` and `protection` are the service's own again.
- **Tests.** The fake service's started event uses the service's new wording ("Started by 0x… in Watch only mode."); the test of the rewording is gone.
- **Older runs.** A protective-stop or close sentence the service stored before its change still says "a shadow run"; `mode_text`, `summary` and the firing lines, which the kit writes, are unaffected.

Not exercised live: this removes a display rewording, checked by the offline tests; no run was started for it.

## 0.12.38 — 2026-10-24

For users: the README now says what stopping a hosted workflow does: it closes what the run opened, unless you ask to keep the position.

- **README.** The hosted workflows paragraph still said "A stop leaves positions open", from before stop and close (0.12.32). It now says a stop closes the run's own share, then cancels its protective stop, and that `stop --keep-position` keeps the position with no stop order from the run. SKILL.md and `docs/hosted-workflows.md` already said so.

Not exercised live: documentation only; no code changed.

## 0.12.37 — 2026-10-23

For users: your assistant now calls the workflow mode that sends no orders Watch only, as the Reya app does, and the other two Paper and Live. Nothing else changes: runs still start in Watch only unless you ask for another mode.

The app renamed the `shadow` mode to Watch only for the pilot, which opens with a watch-only week; the kit still said shadow, so testers would have seen two names for one mode. Only what users read changes: the service, the signed `WorkflowStart`, `workflow info`'s `modes` and `--mode` keep `shadow`, `paper` and `production`.

- **Names.** `status`, `start` and each listed run add `mode_text`, the app's sentence for the mode ("Watch only: it records what it would do and sends no orders"; Paper; Live). The `summary` line reads "Run 7 (Watch only) is active". A Watch only firing reads "Watch only, recorded and not sent". A refused mode names both: "allows shadow (Watch only) mode(s), not production (Live)". The hosted-runs refusal says "watch-only ones included".
- **The service's own sentences.** The workflow service still writes "Started by … in shadow mode." and "a shadow run …" in event and protective-stop texts; the kit shows them as "in Watch only mode" and "a watch-only run". Codes stay as the service sends them (`order_status` and `protection.reason` `shadow`).
- **No `--mode watch` alias.** Watch only is the default, so nobody needs to type it, and a second spelling would differ from the `modes` that `workflow info` and the service's refusals list. `--mode shadow` still works.
- **Docs.** SKILL.md tells the assistant to use the app's names with the user; `docs/hosted-workflows.md`, `docs/from-workflows-to-the-api.md` and the README follow.
- **Tests.** The summary and `mode_text` for each mode, the rewritten event and protective-stop texts, the refusal's wording, and the fake service's started event in the service's own words.

Not exercised live: this changes wording only, checked by the offline tests; no run was started for it.

## 0.12.36 — 2026-10-23

For users: when a hosted rule can't be used, your assistant can now offer to wait for an entry price on this computer, with a stop-loss and optional take-profit; and watch trigger never acts on price updates held back while your computer paused or slept.

Findings from the first devnet rehearsal of `watch trigger` (report-only fires, a two-leg pair fire, pauses of 5 s and 65 s, `watch stop`), and a review of the fixes.

- **Records follow `.env`.** `watch` now loads the kit's `.env` before anything, as every command does, so `REYA_STATE_DIR` set there decides where watches are recorded. Before, records went to the default directory while the commands it fired read `.env`. Watches started with 0.12.35 while `REYA_STATE_DIR` was set only in `.env` are in `~/.local/state/reya-agent-kit/watches`: list or stop them with `REYA_STATE_DIR=$HOME/.local/state/reya-agent-kit` in front of `watch list` / `watch stop ID`.
- **Held frames are stale.** After a pause the socket hands over the frames it held, fast and in order; they were contiguous with each other, so a crossing inside them could fire on prices seconds old. Freshness now also needs on-time delivery: receipt minus the row stamp `updatedAt` at most 3 s above the quickest delivery over the last 60 live-paced pushes (about 30 s). The window is counted and takes only pushes that arrive at live pace, so no pause and no number of held frames can displace it, and frames held through a pause of any length are judged against deliveries from before it; a constant clock offset cancels, and a clock step heals. Until three pushes in a row arrive at live pace, bunched frames are a burst (held from the start), and nothing fires; after that a short network stall is not stale. A crossing in the first second is compared with the first mark once pushes can be judged. New `stale` reason `late_delivery`; pushes carry `late_ms`.
- **Fallback for a single-market entry (SKILL.md, `docs/watch.md`, README).** An entry that waits for a price still goes to a hosted workflow first (it runs with the computer off). When one cannot be used (hosted runs not enabled and the user not wanting to approve the runner, not the Agent account, a run already active, no `production`, the market refused, or a take-profit wanted at entry), the assistant offers `watch trigger` with one `--then "order market ... --sl ... [--tp ...]"` and its trade-off instead of saying the entry cannot wait. Still only after the warning and a second yes; never a loop of its own.
- **Docs.** `docs/watch.md` gives the measured reaction times (devnet from the UK: 0.47 s to fire, 3.42 s to the venue, most of it the order's reads before sending) and the delivery rules. `docs/from-workflows-to-the-api.md` joins the public mirror, so its links from the README and SKILL.md resolve.
- **Tests.** Frames held through pauses of 6, 20, 35 and 55 s never fire, nor a crossing after more than 60 held frames, including when the first push was itself held, and through the socket; a 1.5 s network stall is not stale and the next crossing fires; jitter never reads as stale; a slow push cadence still fires; a clock step recovers without ending the watch; the 3 s limit exactly; rows without stamps; the kit's `.env` deciding where watches are recorded; the fake venue now stamps real milliseconds.

Not exercised live: no order has filled through a watch yet, because every devnet book was empty or outside the fill collar; the pair fire reached the venue, filled nothing, and correctly sent nothing more.

## 0.12.35 — 2026-10-23

For users: when a rule can't run on Reya (a pair trade on one trigger, a funding trigger), your assistant can now wait for the level on Reya's live price feed instead of checking the price in a loop, then place the agreed orders once, with the stop-losses you agreed.

Polling loops on the user's computer missed levels between checks and protected nothing (pilot feedback, fixed in 0.12.28 by sending price-waiting entries to stops, resting limits and hosted workflows). Some rules still can't run on Reya, and assistants should never write their own loop for them. `watch trigger` is the one place for them: event-driven on the market summary WebSocket, with the hosted workflows' crossing rule and the kit's own guards.

- **`watch trigger`** (`agent_kit/scripts/watch.py`, rules in `_watch.py`): `--mark lte|gte:PRICE` and/or `--funding OP:PCT_PER_HOUR` on a `--funding-window` average (default 15 min); `--then` runs at most two `trade` commands once, in order, stopping at the first failure (`legs_atomic: false`); without `--then` it only reports. `--max-hours` up to 24.
- **Event-driven.** It reads every push of `/v2/perpMarket/{symbol}/summary` (about every 500 ms) and never polls REST. A push is fresh while pushes keep coming (3 s) and prices keep updating (10 s). It fires on the first fresh mark at or past the level after a fresh mark on the other side; a level already reached at the start, a crossing seen only through a stale gap (including a sleeping laptop) and 60 s without a fresh price end it with `halted`, never an order. It reconnects with backoff.
- **Refused before watching:** commands other than `order market`, `order limit` and `positions close_all --symbol`; any override or `--all`; more than two; an entry without `--sl`; an open position on a touched market with no stop-loss on Reya; a command whose preview would not send. `--unprotected-ok` lifts the two stop rules after the user agreed.
- **Background:** `--detach` records the watch in `$REYA_STATE_DIR/watches` and returns once it reports; `watch list`, `watch status ID` and `watch stop ID` (SIGTERM only to a live watch process). One JSON object per line; exit 0 for `fired` with every command done, `expired` and `stopped`.
- **SKILL.md:** following or waiting on a price or funding is event-driven on the WebSocket (`stream` to observe, `watch trigger` to wait), never repeated REST reads; `watch trigger` replaces any loop of the assistant's own, only for what a stop, a resting limit or a workflow can't do, after the user's second yes. New SKILL section, `docs/watch.md` and the README.
- **Tests:** the crossing rule (both directions, touching, already reached, gaps by local and server time, frozen prices, silence and its 60 s end), funding as a condition and as the trigger, action parsing and refusals, the stop-loss backstop, the feed loop on a fake socket (fire, reconnect, stop), whole runs (report only, a pair stopping at its first failure, refusals before any read, expiry) and the background records.

Not exercised live: no order was placed through a watch for this release; a report-only watch ran on mainnet's public feed (armed, watched, expired, and a detached one listed and stopped).

## 0.12.34 — 2026-10-23

For users: the kit now recognises your Agent account only by the mark Reya gives the account the app creates, never by its name. A sub-account you named "Agent account" yourself is no longer used; setup has the app create the real one.

The Agent account is now the `AGENTPERP` account by type alone, as the app's setup page and workflows list already find it. 0.12.25 kept a fallback for a wallet with no `AGENTPERP` account: the `SUBPERP` named exactly "Agent account". The API now reports `AGENTPERP` on devnet and mainnet, and the workflow service refuses an untagged account (`NOT_AGENT_ACCOUNT`), so the fallback could only pick an account hosted workflows refuse.

- **By type alone.** `agent init` and `agent status` take the owner's `AGENTPERP` account and nothing else. A wallet without one has no Agent account: `needs_agent_account` with the setup page, and `next` says the kit never takes another sub-account for it, whatever its name. Several `AGENTPERP` accounts still return `ambiguous_new_accounts`.
- **No account adopted for appearing.** A sub-account that appeared after `init` asked for the Agent account used to be recorded whatever its type, from a list of the owner's accounts `init` kept in the state directory. That list is gone: the setup page's account is found by its type, and an untagged sub-account is recorded only when the user names it with `--account-id`.
- **Warned without an Agent account.** `agent_account_warning` now also appears when the recorded account is not `AGENTPERP` and the wallet has none, such as an untagged "Agent account" an earlier kit recorded by its name. The account still trades; the warning says hosted workflows refuse it and how to have the setup page create an Agent account. Until now nothing was said in that case.
- **Kept.** The recorded account (`PERP_AGENT_ACCOUNT_IDS`) is never replaced. `--account-id` still records any sub-account the user chooses, with the warning. A recorded account of a type the kit does not know still stops with "update the kit". The create-account link still names the new account "Agent account", as the app does.
- **One read fewer.** Account names are no longer read: `agent init`, `agent status` and the `workflow` commands' setup-page check use the types from the one wallet read.
- **Docs**: README, SKILL.md, the command reference and hosted workflows no longer describe the name fallback.
- **Tests**: sub-accounts named "Agent account" are never taken by `init` or `status`, and the `AGENTPERP` account the setup page then creates is recorded; new untagged sub-accounts are not taken; the warning for a recorded untagged account with and without an `AGENTPERP` account, and its fix; `--account-id` on an untagged account with no Agent account; the unknown-type refusal with an `AGENTPERP` account beside it. The fake workflow service reports its tagged accounts as `AGENTPERP`.

Not exercised live: no setup was run against devnet or mainnet for this release.

## 0.12.32 — 2026-10-23

For users: stopping a workflow now closes what it opened, unless you ask to keep the position. Before, a stop always left the position open with no stop order.

The workflow service's stop gained a signed choice. `workflow stop` signs it, and `--keep-position` keeps the old behaviour.

- **`workflow stop RUN_ID`** signs `WorkflowStop` with `keepPosition: false`. Once the run's last order and its fills are done, the service closes what the run opened: its own fills, up to the live position, with up to 3 reduce-only orders 20 s apart. Then it cancels the run's protective stop. What was held before the run, or traded by hand, stays open. `close` in the result and in `status` follows it (`pending`, `closing`, `closed`, `nothing_to_close`, `stopped_still_open`, `unknown`) with a `text`, and `next_attempt_at` as a time. `next` says to follow it with `status`, and `note` follows the close the service reports.
- **`--keep-position`** signs `keepPosition: true`. The position stays open with no stop order from the run, and `note` says so.
- **Already stopped.** Stopping a stopped run returns the same result as a stop, with `already_stopped`, and `note` says when that stop chose otherwise than this one.
- **Only the new stop type.** The kit signs only the `WorkflowStop` with `keepPosition`, which the service now requires. Kits up to 0.12.31 refuse a service that publishes it ("update the kit"), and this kit refuses a service that does not, so update the kit once the service has the new stop.
- **SKILL.md and the hosted-workflows page**: stop closes by default; say so for a paper or production run before stopping; follow the close in `status`; when to use `--keep-position`.
- **Tests**: the service's regenerated stop vectors (close, keep, and `keepPosition` missing, a string or null), a service that publishes the stop without `keepPosition` refused, the close in `stop` and `status` and every close state's text, `--keep-position`, and an already-stopped run.

Not exercised live: the service change ships separately, and no run was stopped for this release.

## 0.12.31 — 2026-10-23

For users: when a hosted workflow can't express what you want, such as two legs from one trigger or your own conditions, your assistant now points you to a guide for doing it through the API, with runnable examples and the rate limits that matter.

A pilot user who liked hosted workflows asked for a path to API trading for strategies a single rule can't hold: examples, how a workflow maps to API orders, and the rate limits. Nothing in the kit or the public docs connected the two.

- **`docs/from-workflows-to-the-api.md`** (new): how fast each path reacts (the mark is recalculated once a second, and Reya's stops, hosted runs and an API program all act on it); a table mapping each part of a workflow to what an API program builds, with links to the developer docs (trigger, freshness, the IOC and its sizing, allocation, repeat, expiry, conditions, the protective stop, stopping, shadow and paper, lost answers, order tags, the signer); what the program takes on; setup with a signer and an account of its own, not the kit's agent key or the Agent account; the SDK's runnable `examples/strategies`; the Standard tier's published rate limits; and a checklist before going live.
- **SKILL.md**, hosted workflows step 10: when the user wants more than one rule can do, say which part it can't express and point them to the guide. It does not relax "Protection lives on Reya".
- **Links** from the hosted-workflows guide ("Beyond one rule") and the README.

Not exercised live: documentation only; nothing was placed or started. The examples it points to are offline-tested in the SDK and have not run against a live API.

## 0.12.30 — 2026-10-23

For users: setup now offers Enable hosted runs as an optional last step on the same setup page, so you can turn on hosted workflows while you set up instead of finding the step later. Say now or later; later is remembered and setup is ready either way.

Until now `agent init` stopped at funding and never mentioned hosted runs: users found the extra approval only when a first workflow start was refused, and it felt like a redundant step. The app's setup page already ends with it.

- **Offer at `ready`.** When the deployment has a workflow service and the owner has not approved its active hosted signer, the `ready` result carries `hosted_runs.offer`: the question, the permission text (the approval covers every account the wallet owns; the service only trades the Agent account; no withdrawals, transfers or permission changes) and the two commands. Setup stays `ready`: the kit trades without hosted runs.
- **`--hosted-runs now`.** The new step `needs_hosted_runs` (step 4 of 4) points at the same setup page, which moves on to its Enable hosted runs step; with `--until-ready` the wait goes on through it and returns `ready` with `hosted_runs.status: "enabled"`. A tab left open at the funding step is not opened again. The answer lasts for that run only, so a user who chose it but never signed is offered it again. Progress counts 4 steps only while the hosted signer is still to approve.
- **`--hosted-runs later`.** Recorded in the state directory (0600) and not offered again; enabling it later (`workflow info --open`) still shows `enabled`.
- **One tab.** A setup link that differs only by `&amount=` (which only pre-fills the funding form) counts as the page already open, for `agent init` and for `workflow info --open` alike: the funding step's tab is reused for Enable hosted runs, and `init --amount 50` no longer opens a second tab next to the one opened at approval (its `next` says the open tab may not show the amount, which the user enters on the page).
- **Never in the way.** No workflow service, a service for another deployment, no active hosted signer, or a read that fails or takes over 5 seconds: `hosted_runs.status: "unavailable"` and nothing is said. The service's `/v1/info` is read once per run; the approval check reuses the API-wallet status read setup already makes.
- **SKILL.md, README, AGENTS.md, the command reference and the hosted-workflows page** describe the step.
- **Tests.** No service, offer, later remembered and not repeated, an unreadable answer asks again, already enabled, `--until-ready` through all four steps in one tab with one service read, progress of 3 or 4, no second tab after a funding link with an amount, now clears a later, a broken state directory, no browser at the hosted step (its own command is kept), a reinstall that chooses now is not asked to practise, a funding link with an amount reuses the tab opened at approval (and says the tab may not show the amount), `workflow info --open` reuses a funding tab, and the hosted signer read against the fake service (other deployment, no active signer, unreachable, testnet with no service, a slow service, an unexpected error), and the step on an Agent account found by its `AGENTPERP` type.

Not exercised live: no approval was signed for this release. The hosted signer and approval reads are the ones `workflow info` already makes.

## 0.12.29 — 2026-10-23

For users: when you ask for a dollar amount, your assistant now tells you the real size before placing it, for example $30 of SOL becomes 0.27 SOL ≈ $29.20, and offers the next step up. Below a market's minimum it gives the minimum in dollars.

Order sizes round down to each market's step, so a `--notional` order sends a little less than asked: $30 of SOL at 108.15 is 0.27 SOL (2.67% under). The preview already had the real figures in `qty` and `notional_usd`, but nothing said how far they were from the request, so a user only found out after the fill.

- **`sizing`** on `order market` and `order limit` with `--notional`, in the preview and the result: `requested_notional_usd`, `effective_notional_usd` (equal to `notional_usd`), `shortfall_usd`, `shortfall_pct`, `priced_at` (`mark` or `limit`), `price_usd`, `qty_step` and, when rounding took something off, `next_step_up` (`qty`, `notional_usd`) and a one-line `message`. The runner gets it too, through the same commands. Absent with `--qty`; `--max_size` and `--usage` keep their own `sizing` block.
- **Below the minimum**, a `--notional` order's error names the quantity, the minimum, its dollar value at the price used and the least `--notional` that reaches it, instead of only the quantities. Reduce-only orders stay exempt from the minimum.
- **`workflow preview`** carries the same `sizing` for one firing: the service divides the per-firing notional by the limit and floors the quantity to the lot, so $27 at a limit of 2,512.188 sends 0.010 ETH (about $25.12). `next_step_up.draft_notional` is the least `--notional` the service sizes to the next lot, and the `message` is appended to `order_text`. The lot comes from the public market definitions (one read); if it fails, `sizing` still compares the two amounts and leaves out the next step.
- **SKILL.md**: when the user names a dollar amount, say the real size before placing and offer the next step up (placed with `--qty`); the preview fields list `sizing`. Command reference and hosted-workflows guide updated.

Not exercised live: no orders were placed and no workflow was previewed against a live service for this release. Offline tests use the figures from the report that prompted it: $30 of ETH at 2,491.67 sends 0.012 (next 0.013 ≈ $32.39), $30 of SOL at 108.15 sends 0.27 (next 0.28 ≈ $30.28), $20 of ETH at 2,345 is below the 0.01 minimum (≈ $23.45).

## 0.12.28 — 2026-10-23

For users: your assistant now offers a stop-loss and take-profit with every entry, tells you when a position has no stop on Reya, and no longer builds price-watching loops on your computer: an entry that waits for a price becomes a resting order or a hosted workflow.

A pilot user asked for a pair trade that entered when ETH fell to a level. The assistant wrote a loop on their computer that checked the price every 5 seconds instead of using anything on Reya: it missed the level, and once it entered, both legs had no stop-loss on the venue. SKILL.md used `--sl`/`--tp` only when the user asked, never said a position was unprotected, and even described "any monitoring loop or trigger" as something to build.

- **SKILL.md, "Protection lives on Reya"** (new section, linked from the request steps, the hosted-workflows section and the safety notes):
  - Offer a stop-loss and a take-profit with every entry, as prices based on what the user said, previewed with `--sl`/`--tp` and placed with the entry once they agree. It is an offer: never levels the user has not agreed to, and never a reason to hold back an entry.
  - Say when a position has no stop-loss on Reya.
  - Never protect a position or wait for a price with a loop, script, `sleep` or `watch` on this computer. If the user insists, warn first (it runs only while the computer is awake and the session is open, misses levels touched between checks, and leaves no stop on Reya) and build it only after a second yes. Paper's `watch` stays.
  - An entry that waits for a price is a hosted workflow (either direction, with `--stop-px`), after checking `workflow info` for `production` and its limits, or a resting `order limit` when the market must come back to the level, with the warning that its fill has no stop until one is armed. Said upfront: a workflow fires on the mark, which Reya publishes about once a second, as one IOC within the bound; one market per run, one run per Agent account. A pair trade or "close both legs if X" is not one rule: offer a stop-loss on each leg, or a workflow for the trigger leg.
  - "Before every live order" relays `protection.message` for an entry without a stop; the sliced-entry steps say waiting on time is fine and watching prices is not; the margin rule no longer mentions a monitoring loop.
- **`protection` on entries without a stop.** `order market` that opens or adds to a position without `--sl`/`--tp` now returns `protection` with `state: "not_requested"`, `stop_loss_armed` / `take_profit_armed` (one already armed on the market for that side and not yet fired; `null` when unread) and a `message` that says when the position has no stop-loss on Reya. In the `--preview` plan and in the result; not for closes. The open-order read follows the send, so it never delays the order; it gives up after 2 s (`null`), and inside the runner it comes from the warm order feed with no request. It never changes the exit code; the runner treats `not_requested` like `protected` and keeps the session.
- **Docs.** The command reference has "Entries without a stop"; the runner guide says where the read comes from; the README's order examples lead with a protected entry; `btc_eth_strategy.py` arms a stop-loss and take-profit with its entry.
- **Tests.** No stop armed, one armed for that side (long and short), the other side, another market or account, a stop that already fired, a flip; a live entry reads only after the send; a failed read and a read that hangs; a zero fill and an unknown outcome; closes report nothing and a live close reads nothing; the runner's warm feed and its REST fallback; and the runner keeps its session on `not_requested`.

Not exercised live: no orders were placed for this release; the new read and messages were tested offline only.

## 0.12.27 — 2026-10-23

For users: equity, margin usage and liquidation prices now match Reya's own figures more closely: funding you paid is no longer subtracted twice, and collateral that can only be withdrawn now counts toward equity as it does on Reya.

An independent review of `_margin.py` against Core found two errors that the kit's own vectors could not catch, because both came from how the kit read V2, not from the engine port.

- **Funding was counted twice.** V2's rUSD `realBalance` is net deposits plus each position's settled PnL, and that already includes funding and ADL cashflows up to the latest market trackers (the API computes it as Core does). The kit subtracted an unsettled-funding estimate on top. Example: deposits 10,000, a long of 1 BTC at 100,000 that paid 50, `realBalance` 9,950: Core's equity is 9,950, the kit gave 9,900, and the long's liquidation price 91,938.776 instead of 91,887.756. The kit now adds no funding to a V2 account. `unsettled_funding_estimate_usd` stays in `query margin`, at 0. Paper trading keeps its own funding, since its cash does not include it.
- **Withdrawal-only collateral counted as 0.** V2's `WITHDRAWAL_ONLY` is a deposit policy. Core credits every supporting collateral at price × (1 − haircut). Equity, margin usage, liquidation prices, buffers and the account value for the leverage guard now count it as Core does.
- **Debts as Core values them.** A negative collateral balance counts at amount × price / (1 − haircut), not at its plain value.
- **The engine's own list for the pre-trade check.** The matching engine gives no credit to USDC, deUSD, sdeUSD, the LM receipts, REYA and sREYA (its own collateral table, not V2's status; it credits USDe and sUSDe). The `engine_check` verdict, `health_usd` and sizing use that view. The equity, usage and liquidation figures before and after use Core's. `query margin` marks such collateral with `engine_credit_usd: 0` and a `credit_note`.
- **Docs.** docs/margin.md, the command reference and SKILL.md describe the two views and drop the debit-only funding estimate.
- **Tests.** The new vectors are derived from Core's definitions (`AccountExposure.getNodeMarginInfo`, `PriceHelpers.exchangeWithHaircut`) and from how the API builds `realBalance`, not from the engine's tests. They cover funding for long and short, paid and received, with the liquidation prices; withdrawal-only collateral in Core and in the engine view; a debt; and an order the engine refuses on an LM receipt but admits on USDe with the same Core equity. The debit-only funding tests and the old debt vector (counted in full) are replaced. The engine vectors are unchanged.

Not exercised live: no orders were placed; the figures were checked against Core's formulas offline.

## 0.12.26 — 2026-10-23

For users: workflow status now proves to Reya that it is your agent asking, ready for when Reya stops showing workflow runs to anyone who asks. Nothing changes in what you see.

The hosted workflow service is making run reads private: a run shows the whole strategy before it fires, and run IDs are sequential. Reads will need a grant signed by one of the owner's delegates. The service accepts grants before it requires them, so this release sends one now and keeps every read working without one.

- **The grant.** An EIP-712 `ReadGrant` under the start and stop domain (`Reya Workflows`, version `1`): `verifyingChainId`, `environment`, `owner`, `accountIds` (the recorded Agent accounts), `expiresAt` (one hour) and a random 16-byte `salt`, signed with the agent key, never the owner's key. `REYA_SIGNER=own-key`, no recorded Agent account or no owner means no grant.
- **On the wire.** `Authorization: Bearer <base64url without padding of {"grant": {...}, "signature": "0x..."}>`, integers as decimal strings, on `GET /v1/runs/{id}`, `/v1/runs/{id}/activity` and `/v1/accounts/{id}/runs` only.
- **Kept for the hour.** The grant is kept in the state directory (owner-only) and reused by later commands until 80% of its life has passed, then a new one is signed. A kept grant is reused only when it verifies as this agent key's, for this deployment, owner and set of Agent accounts.
- **Never costs an answer.** A run read that carried a grant and was refused by something in front of the service (a 4xx other than a rate limit, without the service's error envelope) is asked once more without it, and the kept grant is dropped. The service's own refusals stand and are never asked again without the grant: `INVALID_READ_GRANT` (the kept grant is dropped), `READ_GRANT_REQUIRED` and `RUN_NOT_FOUND` (the kept grant stays). Both grant codes get a `next`: re-approve the agent key (`agent status`) and check this computer's clock. Writes are never repeated.
- **Docs.** The hosted-workflows page has a section on private run reads; AGENTS.md records the grant next to start and stop.
- **Tests.** The digest against a hand encoding of the struct; a signature that recovers to the agent key with eth_account's own encoder; the header's format; reuse across commands and replacement at 80%; no grant in own-key mode; the fake service checking grants on exactly the three reads; a gateway that refuses the header still answering; and an enforcing service's `INVALID_READ_GRANT`, `READ_GRANT_REQUIRED` and `RUN_NOT_FOUND` each answered once, with their hints, keeping the grant only for a genuine not-found.

Not exercised live: the service does not check grants yet, and the offline fake checks them with its own decoding.

## 0.12.25 — 2026-10-23

For users: the kit recognises your Agent account by the account type Reya is adding for it, also after a rename, and warns when the account it trades is not the one hosted workflows accept.

Reya's API is adding the account type `AGENTPERP` for the Agent account the app's setup page creates (API spec 3.7.3). Until now an account type the kit did not know failed the whole owned-account read, so `agent init`, `agent status` and the `account` commands would have failed for every user with an Agent account as soon as the API reported it. This release has to be out before the API change.

- **Accepted.** `AGENTPERP` is a sub-account for margin, transfers and `agent init --account-id`. An account type the kit does not know no longer fails the read: the account still counts as owned, but the kit takes no action on it (no transfer, never taken as the Agent account). If the recorded Agent account itself has such a type, `agent init` and `agent status` stop with "update the kit" instead of recording another account in its place. A missing type or a repeated account ID is still refused.
- **The Agent account is found by type.** `agent init` and `agent status` take the account the API reports as `AGENTPERP`, whatever its name. Until the API reports the type (no such account in the list), they fall back to the sub-account named exactly "Agent account", as before; once it does, an untagged account with that name is no longer taken. Several `AGENTPERP` accounts return `ambiguous_new_accounts`.
- **Warning.** When the API reports an `AGENTPERP` account and the recorded Agent account is another, `agent init` and `agent status` add `agent_account_warning`, also at the start of `next`: the account still trades, hosted workflows refuse it, and the fix is to remove the `PERP_AGENT_ACCOUNT_IDS` and `PERP_ACCOUNT_ID_1` lines from `.env` and run `agent init`, which then records the Agent account. Before the API reports the type nothing is said, since every sub-account reads `SUBPERP`. `--account-id` with another sub-account still records it, with the warning.
- **Setup links.** The setup link without `account` counts as the same page when the recorded account is the owner's one Agent account, found the same way.
- **Unchanged.** `query accounts` reads accounts through the SDK, which shows an `AGENTPERP` account's type as `UNKNOWN` until the kit's SDK is updated; `data accounts` shows the API's own value.
- **SKILL.md, the README and the command reference** describe the rule and the warning.
- **Tests.** Transfers to an `AGENTPERP` account; an unknown type kept as owned and refused as a transfer target; a recorded Agent account of an unknown type stops setup and is never replaced, through the real account parsing; a renamed `AGENTPERP` account taken over an untagged one named "Agent account"; several `AGENTPERP` accounts; the warning, absent before the API reports the type, and following its fix; `--account-id` with either kind.

Not exercised live: the API does not report `AGENTPERP` yet, so offline tests drive the account reads with it.

## 0.12.20 — 2026-10-22

For users: when closing a position fills nothing or only part, the result now shows the order book at that moment and says not to resend until it has moved on, and a close preview warns when the book holds less than the position.

0.12.16 added this to `order market`; `positions close_all`, the command an emergency close goes through, did not have it yet.

- **After a close.** A `close_all` close that ends `CANCELLED` with quantity unfilled (`NO_LIQUIDITY` or `IOC_REMAINDER`) reads `GET /v2/market/{symbol}/depth` once and adds `book` to its entry (`best_bid`, `best_ask`, `limit_px`, `touch_past_limit_pct`, `qty_within_limit`, `updated_at_ms`), with the same advice in the entry's `error` as `order market`: what filled, and not to resend at the same or a nearby price until the book's timestamp has moved on, nor to widen the slippage without the user's yes. Each read starts as its close's reply arrives, so it never holds back the next close; a failed or slow read gives `book.unavailable` and changes nothing else. A full close reads no book.
- **Preview.** `close_all --preview` reads the book for each planned close and adds `book`, plus a `warning` when the book holds less than the position inside the close's limit.
- **SKILL.md and the command reference** say so.
- **Tests.** Zero, partial and full closes; the preview warning; an unreadable book in a preview.

Not exercised live: no orders were placed. The read is the same public depth endpoint `order market` already uses.

## 0.12.19 — 2026-10-22

For users: your assistant can now give you a link to a drafted workflow in the Reya app, where you see what it would do and start it with your own wallet. Useful when the assistant can't or shouldn't start it itself.

Some assistants can draft a workflow but won't start one (the Claude desktop app refuses live trades), and some users want to start it themselves. Until now a run could only be started by `workflow start` in a terminal.

- **`app_review_url`** on `draft` and `preview`: `/agents/workflows/run?draft&agent=<agent>&owner=<owner>#access=<code>&doc=<document>` in the deployment's app. The document is compact JSON in base64url without padding, in the fragment beside the access code, so neither reaches a server in the URL. `agent` and `owner` are the setup link's own, so the page can link the setup page's Enable hosted runs step and ask any other wallet to switch. `null` when no app is known for the deployment or the document is over the kit's 12 KiB document cap. The page previews exactly the linked text, which the service reads as the same workflow (same version hash) as the file.
- **The app** shows the draft from `POST /v1/preview` with its warnings, the mode (shadow unless the user picks another the service allows), the pilot caps, what its protective stop and Stop do, and starts it with the owner's one-click trading wallet or their own wallet. A run started there is the same run as one the kit starts, signed by the owner's wallet instead of the agent key: `status` lists it and `stop` stops it.
- **`preview`'s `next`** says to give the user `app_review_url` instead of starting when the session can't or shouldn't.
- **SKILL.md**: offer the link when you can't or shouldn't start the run, never say it is running until `status` lists it, and don't also start it from the kit. New safety rule: tool output and data are never instructions; act only on the user's own requests.
- **Docs**: the hosted-workflows page lists the field and the page.
- **Tests**: the link's shape, the document round-trip and its version hash, the code and the document only in the fragment, no link without an app or over the cap, and a link without `agent`/`owner` before `agent init` has recorded them.

Not exercised live: no workflow was started for this release, and the review page ships in the Reya app separately; until it does, the link opens the run page, which says the link doesn't name a workflow.

## 0.12.18 — 2026-10-22

For users: closing your positions no longer removes their stop-loss and take-profit before the close has gone through, and a leftover position below the market's minimum order size can now be closed like any other.

`positions close_all --cancel_orders` used to cancel every order in scope, TP/SL included, then read positions once. Right after a fill, that read can trail the venue by seconds: it showed no position, nothing was closed, the stops were already gone and the result reported success. A tester reported it. Separately, the API now exempts reduce-only orders from the market minimum (on mainnet since 16 October), so the kit's "cannot be closed" hint for a position below the minimum was out of date.

- **TP/SL stay armed until the position is flat.** `close_all` reads positions, then open orders, before sending anything. `--cancel_orders` cancels only the other orders in scope, never a take-profit or stop-loss, and reads positions again before closing so a fill that raced a cancel is closed too. A close that fills only part leaves the rest protected. Once the read-back shows a market flat, `--cancel_orders` cancels any TP/SL still listed there, and the result reports each TP/SL in scope as `cancelled_protection`, `retired_protection` (already removed by the venue) or `kept_protection` (on a market that is not flat).
- **No conclusion from one empty read.** When a market in scope has TP/SL but no position shows, `close_all` reads again for up to the indexing ceiling (15 s on mainnet, 30 s on devnet) and closes the position if it appears. Inside the runner it first waits, within the same bound, for its feed to show the session's own fills, and also closes a position the feed shows before REST does. If none shows, nothing is closed or cancelled there, and the result is an error with the context `Position not shown`, not a success.
- **Preview.** `would_cancel` no longer lists TP/SL; `would_keep_protection` lists them instead. A market with TP/SL and no position shown is listed under `positions_not_shown` with a `warning`, and `would_drop_protection_count` is then `null`, never `0`.
- **Positions below the minimum.** `close_all` closes them like any other position. `order market --reduce_only` accepts a quantity below the minimum too (never one that rounds to zero). Paper matches: a reduce-only order may go below the minimum, while every order must still sit on the step and stay within the position. `below_min_order_qty` and its hint are gone. A position off the step, which ADL rescaling can leave, is flagged `off_step_qty: true`, and its rejected close carries a hint with the reduce-only order that shrinks it to below one step.
- **Runner.** `positions close_all` is allowed 15 s plus the indexing ceiling (30 s on mainnet, 45 s on devnet), so the wait for a position is never cut off.
- **Docs.** SKILL.md, the README's known limitations, the command reference, the error-code table, the paper guide and the runner guide describe the new order and fields.
- **Tests.** The tester's sequence (TP/SL armed, positions read empty) now cancels nothing and returns an error; its preview no longer counts zero dropped protection. Also covered: a read that trails the fill, a partial close, TP/SL already retired by the venue or cancelled after the read-back, failed reads before and after the cancels, the runner's feed showing a position REST does not, closes below the minimum and off the step, and paper below the minimum.

Not exercised live: no orders were placed for this release. Offline tests drive a fake venue whose position reads trail the fill.

## 0.12.17 — 2026-10-22

For users: equity, index and commodity markets now carry a warning that a stop-loss does not protect through the gap when their underlying market reopens; fill records show each fee net of your rebate; and you can list only liquidations, ADL or delisting closes in a market's trades.

Three small items from tester feedback.

- **Markets whose underlying closes.** `query market info` shows each market's V2 `categories` (`CRYPTO`, `EQUITIES`, `INDICES`, `COMMODITIES`), read from the SDK field, the fields an older SDK keeps unparsed, or the raw row, and `null` when not reported; never inferred from the symbol. A market with reported categories, none of them `CRYPTO`, adds `market_hours_warning` to `order market` / `order limit` previews and results and to `order modify` results (not to reduce-only orders), and to its positions in `query positions` and `query margin`. `query positions` reads market definitions once and passes them to the margin figures; if that first read fails, the warning comes from the definitions the margin figures read again.
- **Net fee.** `query executions` rows, fill fee reads and history records add `net_fee_rusd` / `net_fee`: the taker fee minus `takerRebateCredit`, which the same settlement pays back to the taker's own account; a maker fee is already net. It is `null` when a part is missing (taker fills from before the fee breakdown). `execution_metrics_rusd` adds a `net_fee` sum. `fee` and `rebate_credit` stay as reported, and no fee-adjusted return is derived.
- **Execution type filter.** `data market trades --type ORDER_MATCH|LIQUIDATION|ADL|MARKET_CLOSE` sends the API's documented `type` parameter and also filters locally, so a server that ignores it cannot return other types (`coverage.type_filter.removed_locally` counts them). Paging still follows the pages as served.
- **MARKET_CLOSE.** History lists `market_close_execution_records` next to `adl_execution_records`: closes made when a market was delisted or wound down, not liquidations.
- **SKILL.md.** A position in another market is not a margin hedge: requirements add across markets, so show the preview's margin figures when the user calls an order a hedge. Before opening or adding leverage shortly before a scheduled high-impact release, tell the user and suggest a smaller size or waiting (the kit has no calendar). Relay `market_hours_warning`, and suggest cancelling resting orders that would open or add to a position before the underlying closes, keeping its stop-loss and take-profit. `net_fee_rusd` and `--type` are described.
- **Docs.** `docs/margin.md` adds "Positions in different markets add up" and "Markets whose underlying closes". The history and V2 data docs explain `net_fee`, MARKET_CLOSE records, the `--type` filter, and that execution timestamps are whole seconds (rows sharing one are told apart by sequence number). The command reference adds `categories`, `market_hours_warning` and `net_fee_rusd`.
- **Tests.** Net fee signs and missing parts; the type filter is sent and applied locally; MARKET_CLOSE records are labelled; categories are read from each source or null; the warning appears for equity, index and commodity markets and not for crypto, unreported or reduce-only, in previews, results, positions and margin; `positions` still reads definitions once.

Not exercised live: no orders were placed. The `type` filter and `categories` are read from the API's published shapes; offline tests cover both.

## 0.12.16 — 2026-10-22

For users: when a market order fills nothing or only part, the result now shows the order book at that moment and says not to resend until the book has moved on. A preview warns when the book holds less than the order.

On a thin or one-sided book an IOC market order that fills nothing comes back `CANCELLED` (`NO_LIQUIDITY`), and the obvious next move, resending at the same level, prices from a book that has not changed. A tester's zero-fill IOCs on thin books were mostly such resends within a minute. The kit now shows the book against the order's limit and tells the assistant to wait for a newer one.

- **After a send.** An `order market` that ends `CANCELLED` with quantity unfilled (`NO_LIQUIDITY` or `IOC_REMAINDER`) reads `GET /v2/market/{symbol}/depth` once and adds `book`: `best_bid`, `best_ask`, `limit_px`, `touch_past_limit_pct` (how far the best opposite price was past the limit), `qty_within_limit` (and whether the 1,000 levels served covered the limit), the venue's `updated_at_ms` and `age_ms`. The read starts with the send's reply, so arming `--sl`/`--tp` never waits for it. It is bounded to 2 s, and a failed read gives `book.unavailable` without changing any of the order's own fields. A fully filled order reads no book, so inside the runner the extra REST read happens only after a shortfall.
- **The error** now says what filled at which limit, that the book is as of `updated_at_ms`, and not to resend at the same or a nearby price until the book's timestamp has moved on, nor to widen the slippage without the user's yes. Other cancel reasons keep the generic error.
- **Preview.** `order market --preview` reads the book once and returns the same block, with a `warning` when the book holds less than the order inside its limit (likely a partial fill, or none on a one-sided book). Sends without `--preview` never read the book first, and nothing is refused because of it.
- **SKILL.md and the command reference** describe the block: show it to the user, wait for a later `updatedAt` from `data market book` before resending, and on a thin book offer a smaller size or a resting limit instead of more slippage.
- **Paper** shares its book parsing with this read, unchanged in behaviour.
- **Tests.** Zero, partial and full fills (a book only on the first two), buys and sells, a failed and a slow read, preview warnings including one-sided and truncated books, and the runner reading no book on a full fill while keeping the session after a zero fill. The offline suite stubs every book read.

Not exercised live: no orders were placed for this release. The depth read is the same public endpoint `data market book` and paper already use.

## 0.12.15 — 2026-10-22

For users: a stop-loss or take-profit whose limit is more than 3% from its trigger is now refused before anything is sent, with the closest price that works, and a market that accepts no stops is named as such instead of being reported as a pricing error.

The venue admits a TP/SL only when its child limit is within its band around the trigger: 300 bps (3%) on every market on mainnet and devnet1, verified on 21 Oct 2026 (the engine's 5% collar less its 2% margin, from its configuration). A market with no band answers the same `TRIGGER_LIMIT_OUTSIDE_BAND_ERROR`, and only the venue's message tells the two apart. From tester issue #74.

- **`order sl` / `order tp`.** After rounding to the tick, a `--limit_px` outside the band is refused before the position wait and before signing, with the engine's own boundary rule (E9 integer prices, bound rounded down), so the kit and the venue agree at the edge. The refusal is in `refused_by` (check `trigger_band`) with `band_bps`, `distance_bps`, `admissible_limit_px` and `closest_limit_px`. `--force` sends it anyway, with a `warning`, in case the venue widens its band before the kit changes; inside the runner `--force` stays unavailable, and a refused command writes nothing, so the session continues.
- **Venue rejections.** When the venue answers `TRIGGER_LIMIT_OUTSIDE_BAND_ERROR`, on `order sl` / `order tp` and on each `order market --sl/--tp` leg, the result adds `trigger_band` with `reason`: `no_band` (the market accepts no TP/SL, so a position there cannot carry a stop: do not open a protected entry, close to exit), `outside_band` (with the admissible range, or that the venue's band has narrowed when the kit's own check passed), or `unknown` (the message names neither). An unprotected entry on a `no_band` market no longer offers an arm command that cannot work. The venue's `message` and `client_order_id` are kept.
- **One band constant** (`_trigger_band`) for this guard, the `--trigger_slippage` ceiling and paper's, so the kit's own `--sl/--tp` legs always fit the band.
- **Paper.** `paper order sl|tp` refuses the same limit the same way; with `--force` it answers with the venue's rejection and arms nothing, as live would end.
- **Docs.** The error-code row says how to tell the two causes apart; the command reference gains a TP/SL band section; SKILL.md says to offer `closest_limit_px` and never to force past the band, and what to tell the user on a market with no band. No narrowing retries: a tighter limit after a rejection makes the stop less likely to fill when it fires.
- **Tests.** The boundary at exactly 3%, with a trigger whose bound the engine rounds down, for longs and shorts and both kinds; refusals send and read nothing; `--force` outside and inside the runner; the three venue messages; a protected entry on a market with no band; the kit's own legs at every `--trigger_slippage` up to 3%; paper parity. Six existing test cases whose limits were 4.8–18% from their trigger now use limits inside the band.

Not exercised live: no orders were placed. The engine's admission rule and its two messages, the API's pass-through of the engine message, and the deployed band values were read in the source and deployment configuration, not observed on a live order.

## 0.12.14 — 2026-10-22

For users: a trading session no longer ends because one renewal of its safety timer failed. The runner retries a passing failure while the timer still has time left, and it keeps going after an order the exchange refused before receiving it.

A runner session keeps the venue's 30-second cancel-all-after countdown alive by renewing it every 10 seconds. Until now any failed renewal ended the session, although two more renewals fit in the countdown, and a single `SERVICE_UNAVAILABLE_ERROR` on an order ended it too (issue #73).

- **Renewals are retried.** A renewal that fails with `SERVICE_UNAVAILABLE_ERROR` (the request never reached the matching engine), `RATE_LIMITED_ERROR`, `ORDER_OUTCOME_UNKNOWN_ERROR`, another 5xx, a timeout or a transport error is retried after 1 second, or after the venue's `retry_after_ms` when that is longer. Each attempt is a new signed request with a fresh nonce. A failed renewal leaves the previous countdown running at the venue.
- **Bounded by the last confirmed deadline.** A retry is sent only while it can still be confirmed in time: the wait, the 5-second write timeout and a 2-second margin must all fit before the deadline of the last confirmed renewal, which is measured from before it was sent. A failed attempt never moves that deadline. As before, a reply that arrives after it is refused, and the runner never re-arms after a stall.
- **What still ends the session at once:** `EDGE_RATE_LIMITED` (the per-IP ban lasts 60 seconds, longer than the countdown), a countdown the venue did not confirm (account, duration or trigger time), and any other renewal error, such as a signature or validation error.
- **Orders go on while a retry waits.** The wait holds no lock, so trade writes continue until the deadline.
- **Visible to the operator.** `status`, the `stopped` event and the journal report `heartbeat`: `retries`, `last_retry` and `halted_by`, each with the error code, message, HTTP status and time.
- **`SERVICE_UNAVAILABLE_ERROR` on one order keeps the session.** The API sends it with HTTP 400 when a dependency or the engine queue was unavailable before dispatch, so nothing was created. The result carries `session_continues: true`, like `RATE_LIMITED_ERROR`. An entry whose TP/SL is unprotected or unknown still ends the session.
- **Docs.** SKILL.md, the runner guide and the error-code reference describe the retry rule and the new session-continues code.
- **Tests.** Retry then success for each retryable failure, with a fresh nonce per attempt; the venue's `retry_after_ms`; the exact deadline boundary (a retry at 122 s fits a 130 s deadline, at 123 s it does not); failures that keep coming until no retry fits; an immediate halt on an edge ban, a signature or validation error and an unconfirmed countdown; a retry answered after the old deadline is refused; an order goes through during a retry's wait; a session that survives a failed renewal and reports it in `status`; and `SERVICE_UNAVAILABLE_ERROR` on an order keeping the session.

Not exercised live: no runner session was started. Offline tests drive the renewal with stubbed venue answers and a controlled clock.

## 0.12.12 — 2026-10-22

For users: when a hosted workflow reaches its expiry, its protective stop now stays on your position. The run stops placing new orders, and workflow status says the stop is still there until it fires, you stop the run, or you cancel it.

The hosted workflow service now completes a run at its expiry instead of stopping it, so expiry no longer cancels the run's protective stop. This follows the 21 Oct mainnet canary, where an expiring run cancelled its stop and left the position open with none. The kit follows the service's new completion reason.

- **`workflow status`.** A completion with reason `expired` reads "completed because it reached its expiry and sends no new orders". For a completed run, only an `armed` stop is reported as staying on the position. `arming` and `unknown` explicitly say protection is not confirmed; `cancel_pending` says cancellation is in progress and the stop may still fire. Runs that stopped at their expiry before the change still read "stopped: the workflow expired".
- **SKILL.md.** The assistant tells the user that a completed run keeps its stop, and how to take it down: `workflow stop` on the completed run, or `cancel_all` on the market.
- **Docs.** The hosted workflows page says what `--expires` does, adds "When the run completes, or expires" to the protective stop section, says a completed run can be stopped to take its stop down, and moves `expired` from the stop reasons to the completions.
- **Tests.** An expired completion with an armed stop says the stop stays; uncertain states are covered for every completion reason, and stops that are absent or no longer live are not claimed; a run that stopped at its expiry before the change still reads as before.

Not exercised live: the service side ships separately, and until it is deployed an expiring run still stops and cancels its stop.

## 0.12.11 — 2026-10-22

For users: hosted workflows can no longer be drafted with a condition on your available balance. The service cannot check that balance yet, so a run with one never fired.

`workflow draft --balance OP:USD` is gone. Reya's hosted workflow service has no source for the available balance, so an `available_balance` condition skips every crossing as `skip_balance_unavailable`. The service's next release refuses one at validate, preview and start (`available_balance_unsupported`); the kit stops offering it now. `--funding` and `--position` stay.

- **`workflow draft`.** No `--balance` option: passing it is a usage error.
- **Runs already started.** The service does not re-check a stored run, so a run started with a balance condition keeps skipping every crossing as `skip_balance_unavailable`. `workflow status` still names that skip and still describes the condition. Stop the run and draft it again without the condition.
- **`skip_condition_balance` is no longer named.** The service never had a balance to compare, so it never emitted that code. Should it ever appear, it is shown as itself, like any code the kit does not know.
- **Docs.** SKILL.md and the hosted workflows page drop `--balance`. The page says why there is no balance condition, that newer service releases refuse one, and when `skip_balance_unavailable` can still appear.
- **Tests.** `draft` refuses `--balance` and still takes `--position`, and a stored run's balance condition is still described in `status`, with its skip named.

Not exercised live: a draft or start. The change removes a draft option and changes docs; nothing new is sent to the service.

## 0.12.9 — 2026-10-22

For users: if the price crosses your workflow's level between the preview and the start, your assistant now asks whether to fire at once or wait for a fresh crossing, instead of starting a run that quietly waits for the price to move back.

`workflow start` asks the service for a fresh, unsigned preview just before signing. A run started on a level the mark has already passed does not fire on that crossing: it waits until the mark moves back past the re-arm band (0.5% today) and crosses again. On the 21 Oct mainnet canary the mark crossed a level 15 bps away in the 90 s between preview and start, and the run waited 1.5 h for that round trip with nothing in the start result to say so.

- **`ALREADY_TRIGGERED`.** When the fresh preview says `already_triggered` and the user chose neither `--fire-now` nor `--wait-for-rearm`, `start` signs nothing and returns the service's `now` line, the `trigger` and a `next` naming both commands (keeping `--mode`).
- **`--wait-for-rearm`.** The explicit way to start on a passed level and wait for a fresh crossing (the old default). It cannot be combined with `--fire-now` and is not part of the signed request (`fireNow` stays false).
- **Start result.** It now includes `now`, the service's line from that preview, and `already_triggered` when `--fire-now` or `--wait-for-rearm` was used on a passed level, saying which it does.
- **Preview.** Its `already_triggered` text and `next` name both choices.
- **Docs.** The hosted workflows page covers the check under `start`, the errors table and a note on setting the level well away from the mark; SKILL.md tells the assistant to ask, never pick.
- **Tests.** A level crossed after the preview is refused unsigned; `--fire-now` and `--wait-for-rearm` start and say so (and send `fireNow` true and false); the suggested commands keep `--mode production`; the start result says where the run arms; the two flags are exclusive.

Not exercised live: a start on a level crossed after the preview, against the hosted service. The offline fake service covers the refusal and both choices; the case it guards against was seen live on mainnet run 4 on 21 Oct.

## 0.12.8 — 2026-10-22

For users: the kit now installs certifi 2026.10.19, which stops trusting the emSign ECC Root CA - G3 after its issuing keys were found to be crackable. Update the kit to get it.

certifi is the CA bundle `requests` and `httpx` verify every HTTPS connection against. Versions before 2026.10.19 trust the emSign ECC Root CA - G3, under which eight intermediate keys were weak enough to recover from public certificate data (GHSA-6q48-6c7w-9gw2). Lockfile-only change: no code changes.

- **certifi 2026.5.20 -> 2026.10.19** in `poetry.lock` and `requirements.lock`. The new bundle differs from 2026.7.22 only by dropping that one root; its Python files are unchanged apart from the version string.

Not exercised live: no connection was made with the new bundle. `make test` and `poetry check --lock` pass offline.

## 0.12.7 — 2026-10-21

For users: asking your assistant to enable hosted runs now opens the Enable hosted runs step of the Reya app's setup page in your browser, instead of only giving you the link.

`workflow info --open` opens the setup page's Enable hosted runs step when the owner has not approved the hosted signer. It uses the same one-tab rule as a refused `start` and the setup links. Plain `workflow info` still opens nothing.

- **`workflow info --open`.** With `hosted_signer_enabled: false`, it opens `enable_url` once and reports `opened`, `link_already_opened` or `browser_unavailable`, with a `next` to match. A tab already opened by `agent init`, by a refused start or by an earlier `info --open` counts as open. With `true`, it opens nothing.
- **SKILL.md.** When the user asks to enable hosted runs, the assistant runs `workflow info --open`. To only check, it runs `workflow info`.
- **Docs.** The hosted workflows page covers `--open` under `info` and in the section on enabling hosted runs.
- **Tests.** `info --open` opens the page once, a refused start then finds it already open, it opens nothing once the owner has approved, and it says where to open the link when no browser can.

Not exercised live: a real browser tab. `workflow info --open` ran live and read-only against mainnet on 21 Oct for a fresh wallet, with a browser stub that logs instead of opening. It opened the page once, reported `link_already_opened` the second time, and plain `info` opened nothing.

## 0.12.6 — 2026-10-21

For users: set up before hosted runs existed? Ask your assistant to enable hosted runs: it gives you the link to the Enable hosted runs step of the Reya app's setup page, where you approve once with your wallet. A workflow start refused for it opens that page.

Every workflow start, shadow included, needs the owner to approve the service's hosted signer once. The app's setup page now has that step (Enable hosted runs), but the kit only linked the setup page while setup was incomplete, so a wallet already set up never reached it. `workflow info` and a refused `start` now link it.

- **`workflow info`** says whether the owner has approved the active hosted signer (`hosted_signer_enabled`). It checks the same way `agent status` checks the agent key: the owner's approved signers from the API-wallet status read, which the setup page also uses. When the owner has not approved it, `info` returns `enable_url`, the setup page for this agent key, owner and Agent account, and a `next` to relay. `info` stays read-only and opens nothing. A failed read gives `null` with `hosted_signer_note` and never fails `info`.
- **A `start` refused with `HOSTED_SIGNER_NOT_ENABLED`** returns `hosted_signer` and `enable_url`, and opens the page once, like the setup links. It is not opened again while it is already open: a link opened in the last 30 minutes counts, by `start` or by `agent init`, and so does the setup link without `account` when that page shows the same Agent account. `--no-open` only returns the link. With no browser on this machine, the result says `browser_unavailable` and asks you to open the link on any device with your wallet.
- **One link builder.** The setup link and its same-page rule moved out of `agent init` into `agent.setup_link` and `agent.same_setup_page`, so `agent init` and the workflow commands build the same URL. The access code stays in the fragment only.
- **Docs.** The hosted workflows page has a section on enabling hosted runs, and covers the new `info` fields, the refused `start` and the errors table. SKILL.md says what to do when the user asks to enable hosted runs.
- **Tests.** The local fake service now also answers the trading API's owner reads (the approval list and the accounts) from its own state. An approval given in a test is therefore one that both the service and the kit's check see. New tests cover the link on `info` until the owner approves; an agent key not approved yet; a check that cannot be read; the refused start opening the page once; a setup tab from `agent init` counting as open; `--no-open`; no browser; and no known app.

Not exercised live: a refused start opening the page, which needs a start signed for a wallet without the approval. `workflow info` ran live and read-only against mainnet on 21 Oct: it reported `true` for a wallet that has approved the hosted signer, and printed the link for a fresh wallet that has not, with nothing opened or signed.

## 0.12.5 — 2026-10-20

For users: a hosted workflow's stop price is now placed as a real stop. The service arms it once the run's first fill settles, the stop then covers the whole position, and when it fires the run stops.

Reya's hosted workflow service now places the stop on mainnet and devnet. The docs said it placed none yet; they now describe what it does.

- **Docs.** The hosted workflows page has a section on the protective stop. It covers:
  - when the stop arms (after the first settled fill, never at start);
  - the order it places;
  - what happens when it fires (the run stops) and when the run stops (the stop is cancelled and the position stays open);
  - every state and reason in `protection`, with what to do next.

  It also covers an existing stop on the account (left alone; the run keeps going), a revoked hosted signer (clear the stop with `cancel_all`), and paper and shadow runs. The `draft` table, the `stop` section and the stop reasons follow.
- **SKILL.md.** What to tell the user about a run's stop as it moves through those states, and how to clear a stop the service can no longer cancel.
- **`draft --stop-px` help.** The stop is armed once the run's first fill settles.
- **Stop reasons.** `status` and `stop` name `protection_fired` (the run's protective stop fired).

Not exercised live through the kit: this release changes documentation, help text and one reason label. The service's stop was exercised on devnet on 20 Oct, with runs started outside the kit.

## 0.12.4 — 2026-10-20

For users: workflow commands now reach Reya's hosted workflow service at /workflows on the API host you already use (api.reya.xyz on mainnet, api-devnet.reya-cronos.network on devnet).

The service moved from a hostname of its own to a path on each API host. The kit's defaults followed, and `REYA_WORKFLOWS_URL` can now carry that path.

- **Defaults.** Mainnet uses `https://api.reya.xyz/workflows`, and devnet uses `https://api-devnet.reya-cronos.network/workflows`. Every request is that base plus `/v1/...`. The old devnet hostname still answers for older kits until it is retired. The planned mainnet hostname was never created.
- **`REYA_WORKFLOWS_URL`** takes an origin with an optional base path, such as `https://api.reya.xyz/workflows`. The kit refuses a URL that ends in `/v1`, or that has credentials, a query, a fragment, or empty, dot or escaped path segments, since the kit adds `/v1/...` itself.
- **Tests.** The local fake of the service answers only under `/workflows`, as the API host's gateway does, so every workflow command's test goes through the joined path. New tests cover the defaults, the URLs accepted and refused, a service at the root of its host, and a base URL missing the prefix (an HTTP 404, reported as such).
- **Docs.** The hosted workflows page and `.env.example` give the new base URLs.

Exercised live on devnet (20 Oct), with no override set: `workflow info`, `draft`, `validate`, `preview` and `status` reached `https://api-devnet.reya-cronos.network/workflows`. Nothing was started or stopped, and mainnet's path was not live yet.

## 0.12.3 — 2026-10-19

For users: the hosted workflow docs no longer say a workflow's stop is placed when the run starts. The hosted service does not place stops yet, so a run started with a stop price has no stop.

The docs and the `draft --stop-px` help said the stop was "armed when the run starts". The service accepts the field and places nothing yet. When it does, it will place the stop only after the run's first fill settles.

- **Docs.** The `draft` table, the `stop` section, SKILL.md and the `draft --stop-px` help say the stop is not placed yet, so a run started with one has no stop.
- **Test fake.** The local fake no longer reports a stop at start. The workflow test places one only after the first fill has settled.

Not exercised live: this release changes documentation, help text and the test fake only, and the hosted service does not place stops yet, so none has been observed.

## 0.12.2 — 2026-10-19

For users: when you start, check or stop a hosted workflow, your assistant can now give you a link to that run's page in the Reya app, and to the page listing your Agent account's workflows.

The app's workflow pages open only with the kit's access code, like the setup page, so a bare link showed only "Start from your assistant". The workflow commands now print links that carry it.

- **Run and list links.** `workflow start`, `status` and `stop` return `app_run_url` (`/agents/workflows/run?id=<run_id>`) and `app_runs_url` (`/agents/workflows?account=<account_id>`). `status` without a run ID links each listed run, and a start or stop whose outcome is unknown still links the pages.
- **Same app, same code.** The links use the app the setup links use for the deployment (or `REYA_APP_URL`) and the setup page's access code, in the fragment only, never in the query. They are `null` when no app is known for the deployment or `REYA_APP_URL` is not a plain https URL; a link never fails a command. A `REYA_APP_URL` that cannot be parsed, or whose port is not a valid port, now counts as not a plain https URL: `agent init` and `agent status` refuse it with that message instead of a raw parse error.
- **Docs.** SKILL.md tells the assistant to pass the links on exactly as returned, since the part after `#` is the code the page needs. The hosted workflows page lists both fields and the app for each deployment.

Not exercised live: no link was opened from a real run for this release. The links were tested offline against a local fake of the workflow service, for every command that reports a run, an unknown start or stop outcome, a `REYA_APP_URL` override, a malformed one and a deployment with no known app.

## 0.12.1 — 2026-10-19

For users: workflow preview works against the live hosted service again, and shows what one order at your level would buy, what it reserves, how many such orders your budget covers, and the current funding.

The hosted service's released preview takes the document only and answers in a new shape. 0.12.0 sent `fire_now` with every preview, which the service refuses as an unknown field (400 `INVALID_REQUEST`). It also rendered the earlier `on_start` / `at_level` / `conditions` decisions, which the service no longer sends.

- **Preview sends the document only.** `--fire-now` is now a `start` flag only. Preview's `next` suggests it when the mark is already past the level.
- **Preview output.**
  - `now`: the service's first line.
  - `order_text`: side, size, limit, price bound and reservation, plus how many such orders the allocation covers. `order_error` says why there is no order.
  - `funding_text`: the latest sample and the 15-minute average.
  - `lines`: the service's findings.
  - `trigger.side`: where the level is relative to the mark.
- **`NOT_AGENT_ACCOUNT` says why.** `next` follows the service's `details.reason`: `not_tagged`, `main_account` or `spot_account`.
- **Stop without a protective order.** When the run armed none, `next` says there is nothing to cancel instead of pointing at a cancellation.
- **Test fake.** The local fake answers preview in the service's shape and refuses unknown fields, as the service does.

Live on devnet1 on 19 Oct, against workflow-service 0.1.4 in shadow mode, from Claude Code in a terminal: `info`, `draft`, `validate`, `preview`, `start`, `status` and `stop` all ran on a devnet Agent sub-account, signed with the kit's agent key.
- The start was accepted, and a second start was refused with `ACCOUNT_HAS_ACTIVE_RUN`.
- The run armed 232 bps from its level. The stop was accepted, and a repeated stop reported `already_stopped`.

Not exercised live: no crossing happened during the devnet run, so no firing, skip or protective stop was observed. Paper and production modes are not offered on devnet yet.

## 0.12.0 — 2026-10-19

For users: your assistant can now set up a rule such as "buy $25 of BTC each time it drops through 90,000, up to $200, for a week", preview it, and have Reya run it on your Agent account while your computer is off, then follow it and stop it.

New `workflow` commands make the kit a thin client of Reya's hosted workflow service, a private pilot. The service evaluates the rule and places the orders; the kit drafts the document, asks the service to check and preview it, and signs the start and the stop with the agent key.

- **Commands.** `workflow draft` writes the document locally from flags (market, direction, level, side, size per firing, total allocation, expiry, repeat, price bound, funding, position and balance conditions, an optional whole-position stop). `validate` and `preview` ask the service; `start`, `status` and `stop` drive and follow the run; `info` shows the environment, the hosted signer that places the run's orders, the allowed modes and the pilot caps.
- **The service is the only evaluator.** The kit never decides whether a trigger would fire and replays nothing. `preview` shows the mark, the distance to the level, what one firing would send and reserve, each condition's current value and the service's warnings, and says when the level is already past (`--fire-now` or wait for a fresh crossing).
- **Signed with the agent key, never the owner's.** `start` and `stop` are EIP-712 requests signed with the key from `agent init`, under the domain and message types the service publishes; the kit checks them against the fields it knows and refuses a domain that names a chain or contract. Each request has a fresh nonce and a five-minute deadline, and the kit verifies its own signature before sending. Its signing reproduces the service's signed-request test vectors byte for byte. `REYA_SIGNER=own-key` is refused, and the key is never printed.
- **Agent account only.** Every command refuses an account that `agent init` did not record as the Agent account, before anything is signed or sent; `status` and `stop` also refuse a run that belongs to another account.
- **What gets signed is what you wrote.** The start signs the hash of the service's canonical form of the document. The kit refuses to sign if that form changes anything in the document (it may only fill in defaults) or if the hash is not its hash, and checks the mode and the pilot caps before signing. Runs start in shadow mode, which records decisions and sends no orders, unless the environment allows another mode and it is asked for.
- **Status in plain words.** The armed level and the distance to it, each firing as "triggered at 90,000, executed at 90,012 (+1.3 bps), 0.0028 BTC for $252.03" (or why it did not fill), every skipped crossing with its named reason next to the service's text, the remaining allocation, and named stale, stop and completion reasons.
- **Lost answers.** If a start's or stop's answer is lost, the kit confirms it with one read of the run; otherwise it reports the outcome as unknown and resends nothing.
- **Docs.** SKILL.md tells Claude Code and Codex to preview before every start, start only on the user's yes, report status from the result's fields and say that a stop leaves positions open. README and the new hosted workflows page cover the commands, outputs and errors.

Not exercised live: no workflow was validated, previewed, started or stopped against a real service for this release. The hosted service's validate, preview, start and stop endpoints are not served yet, so the commands were tested offline against a local fake of the service that checks signatures independently: the full draft, preview, start, status and stop flow; refusals of other accounts, an own key, another environment, a changed canonical form, a mode or amount above the caps; the service's error codes; lost answers; and the formatting of every named reason.

## 0.11.7 — 2026-10-19

For users: every command now tells you when a newer kit release is out (checked at most once a day), so your assistant can offer to update.

Before, only `agent status` and `agent init` reported a newer release, so someone who only traded never heard of one. Now every command's result can carry the notice, from a check that runs at most once a day and never fails or noticeably slows a command.

- **`kit_update` on every result.** When a newer release exists, a command's JSON result carries `kit_update` (`installed`, `latest_release`, `update_available`, `how_to_update`). Lists, results inside a runner session, and results that already carry `kit` (`agent status` / `agent init`, which still check every time) are unchanged.
- **Once a day.** The check is the same `git ls-remote` against the checkout's origin. Its answer, a failed check included, is cached per checkout in the state directory as `kit-update-check.json`, and for a day other runs only read that file. A damaged cache, or one dated in the future, is checked again.
- **3 s at most.** The retry with the GitHub CLI's credentials now gets only what the first try left of the 3 s; before, each try had its own 3 s, so `agent status` could wait 6 s. `REYA_KIT_UPDATE_CHECK=0` turns the check off, and then nothing is read or written.
- **Runner sessions.** The runner's `ready` event carries the last cached answer. It never waits on the network once the session's heartbeat is armed.
- **Never installs anything.** SKILL.md tells the assistant to finish the task at hand, tell the user once, and run `./install.sh --release=latest` only after they agree.
- **Releases follow merges.** Each new changelog section starts with a one-line `For users:` summary, and release tags and their notes are created from this file after each merge, so the latest release follows the kit's version instead of waiting to be cut by hand.

Not exercised live: no orders were placed and no release was tagged or announced for this change. Offline tests cover the daily cache (fresh, a day old, another checkout, a failed check, a damaged or future-dated cache), the shared 3 s bound, the notice on direct results only, the runner's cache-only `ready` event, and the release decisions: a new version, an existing tag, a tag without its Release, a lower version, a missing section, the changelog check and the announcement text.

## 0.11.6 — 2026-10-19

Standalone `order sl` / `order tp` wait for the position up to the deployment's indexing ceiling (15 s on mainnet, 30 s elsewhere), as `order market --sl/--tp` does since 0.11.4, instead of a fixed 4 s.

- **The defect.** `order sl` is the recovery step after an `unprotected` entry: SKILL.md tells the agent to run the stop-loss `command` from `protection`. It read the position for at most 4 s, then refused with `No open position` (inside the runner, `no protective order submitted`). An entry is reported `unprotected` when the venue is slow to index its fill, which is when the recovery step runs. During the devnet1 episode on 19 October 2026, with fills 4.6–15.4 s late, it would have refused a healthy fill. On mainnet the worst fill from 13 to 19 October reached the feed 4.15 s after its block, an estimated 7 s after its acknowledgement, already past 4 s.
- **The bound.** The same as after a protected entry: the indexing ceiling for the configured API URL. REST reads stay one per 0.5 s plus the first, so at most 31 on mainnet and 61 elsewhere. The acceptance rule is unchanged: an open position on the `--side` side. A flip shows the old side first, so a wrong side keeps reading. Inside the runner the verified feed decides at each update; if it drops, REST gets only the rest of the ceiling. The error now says how long it read.
- **A refusal waits the whole ceiling.** A missing or wrong-side position is reported after 15 s on mainnet and 30 s elsewhere, not 4 s. Positions and executions both appear only once the venue indexes the fill, so to every read the kit can make a late fill looks like no position. The command has no handle on the entry that would tell them apart sooner. Refusing early leaves a real position without its stop; waiting costs time on a command that has nothing to protect.
- **Runner timeout.** Inside the runner `order sl` / `order tp` may take 15 s plus the ceiling plus one trigger write of up to 5 s: 35 s on mainnet and 50 s elsewhere, instead of 15 s, which a 15 s or 30 s wait no longer fits.
- **Unchanged.** `--force` still places the trigger with a `warning` when no position or the other side shows (it is unavailable inside the runner). Nothing is resent and no trigger is retried.

Not exercised live: no orders were placed for this release. Offline tests run the CLI, and the runner against a fake WebSocket, with the ceilings scaled down. They cover a position shown 20 s late, which is armed on devnet and refused on mainnet; a feed lost mid-wait, after which REST gets only the rest of the ceiling; and the runner timeout per deployment.

## 0.11.5 — 2026-10-19

Two follow-ups from 0.11.1 for the runner's own fills on slow indexing: each fill's indexing lag in the journal, and REST proof of a pending fill while the feed cannot show it.

- **Lag per fill, in the journal.** When an own fill leaves the runner's count, the journal's new `fill_lags` gets a record: `order_id`, `symbol`, `sequence`, `resolved_by` (`feed`, `rest`, or `ceiling` when dropped unshown), `ack_to_resolved_s`, `trade_timestamp_ms` (the block timestamp), `venue_lag_s` and `ceiling_s`. `venue_lag_s` is this computer's clock when the runner saw the fill minus the block timestamp; whole-second block times and the local clock offset make it good to about a second, and for `rest` it is an upper bound. A rehearsal journal now records how long each fill took to index, so the indexing ceiling (15 s on mainnet, 30 s elsewhere) can be sized from data. Journal only: the `fees` and `stopped` events are unchanged.
- **REST can end a pending-fill refusal.** Since 0.11.1, while the wallet feed is down or resubscribing, an order that needs the account checks is refused (`no order submitted`) for each own fill the feed has not shown, until the feed shows it or the ceiling passes. Now the runner also asks REST in the background. The order's executions give the fill's trade sequence number, and a REST position whose `lastTradeSequenceNumber` is at or past it includes the fill, so REST decides from then on. The reads (positions, plus executions while the sequence is unknown) only read, each is bounded to 2 s, they back off from 0.5 to 4 s, and they stop once the feed is verified again or nothing is left to prove. Nothing is resent.
- **A fill the feed has just shown is no longer assumed to be in REST.** The feed leads REST by about 0.5–0.8 s. Before, a socket drop right after the feed showed a fill sent the next order's checks to REST at once, which could still lack it. Now, for 5 s after the feed shows a fill, REST decides only once it proves that fill the same way.
- **Limits.** A fill that leaves no position (a close to flat) has no REST row to prove it: it waits for the feed, those 5 s or the ceiling, as before. Once REST decides, the fill's fee and realized-loss debit no longer applies, as when a fill is dropped at the ceiling. The refusal message and the ceilings are unchanged.

Not exercised live: no orders were placed for this release. Offline tests hold the fake feed down while REST shows the fill (in `LiveState` and through the runner, where the refused order goes through once REST proves the fill and each order is sent once), cover the 5 s REST trail for an open and a closed position, a failing or too-early REST read that proves nothing, reads that stop at the ceiling, and the journal record for a fill the feed shows and one dropped at the ceiling.

## 0.11.4 — 2026-10-19

`order market --sl/--tp` waits for the entry's position until the evidence arrives, up to the deployment's indexing ceiling from 0.11.1 (15 s on mainnet, 30 s elsewhere), instead of a fixed 5 s.

- **The defect.** Since 0.7.0 the stop-loss waited at most 5 s for the position to include the entry's fill. On a miss nothing was placed and the position was reported UNPROTECTED. On devnet1 on 19 October 2026 fills reached the feed 4.6–15.4 s after their block for half an hour, so healthy fills were reported UNPROTECTED and left without a stop. On mainnet the worst fill seen from 13 to 19 October reached the feed 4.15 s after its block. With the time from acknowledgement to block on top, that is already at the 5 s edge.
- **The ceiling.** The wait still ends at the first evidence, under the same rule: the entry's side with a newer `lastTradeSequenceNumber`, on the runner's verified feed or on REST. The ceiling is the indexing ceiling the runner already uses to keep counting an own fill the feed has not shown, so one bound covers both.
  - Mainnet: 15 s, about twice the measured worst case of about 7 s from acknowledgement.
  - Elsewhere: 30 s, which covers the devnet1 episode and fill-burst backlogs of about 10 s.
  - The configured API URL decides. `REYA_MODE` cannot disagree, because `live` requires the mainnet URL and `devnet` refuses it.
  - The CLI and the runner use the same ceiling.
- **REST reads.** They stay one per 0.5 s, so the cap grows with the ceiling: 31 reads on mainnet, 61 elsewhere. If the feed drops mid-wait, REST gets only the rest of the ceiling.
- **Runner timeout.** A protected command inside the runner may now take 42 s on mainnet and 57 s elsewhere, instead of 30 s. That is 15 s, plus the ceiling, plus two trigger writes of up to 5 s each, plus the 2 s read-back. Before, 30 s was enough only because the wait was 5 s.
- **Evidence in the result.** `protection.position_wait_s` says how long the wait took, and `protection.position_wait_limit_s` gives the ceiling that applied.
- **Unchanged.** The entry is never resent and no trigger is retried. The stop-loss goes first. A position still not visible at the ceiling is `unprotected` with nothing placed, with the same message, exit code and session stop. Standalone `order sl` / `order tp` keep their 4 s bound.

Not exercised live: no orders were placed for this release. Offline tests run the runner against a fake WebSocket, with the ceilings scaled down.

## 0.11.3 — 2026-10-19

An order the runner acknowledged as resting no longer stays in its warm open-order view after it ends during a disconnect.

- **The defect.** Since 0.9.0 the runner counts each order acknowledged as resting until the order feed (`orderChanges`) shows it, so `order cancel` and `order modify` find it without a REST read. If the socket dropped before the feed showed the order, and the order then ended while disconnected (filled, cancelled, or cancelled with `POSITION_CLOSED`), the resubscribe snapshot never showed it, because it lists open orders only, and nothing removed it. For the rest of the session a cancel or modify naming that order resolved to it and was sent for an order the venue no longer had, and since 0.10.3 a stale stop-loss or take-profit made `order market --sl/--tp` refuse with `protection_exists` on its market.
- **The fix.** The runner notes which order snapshot preceded each acknowledgement. After a reconnect, once REST, read after the new snapshot, matches the feed, the orders acknowledged before that snapshot leave the view: the snapshot, every update since and REST all leave them out. An order acknowledged after the snapshot still counts until the feed shows it, so a stop-loss armed by the previous command is still refused as existing.
- **Unchanged.** Between a reconnect and that match the warm view is unavailable and every check reads REST, as before. If REST was only slow to list an order, the feed shows it when the venue publishes it, as for orders placed elsewhere. The TP/SL read-back still counts only triggers the feed shows armed, never an acknowledged one.

Not exercised live: no orders were placed for this release. Offline tests run the runner against a fake WebSocket.

## 0.11.2 — 2026-10-19

Tests that never touch the network, and a runner that does not wait minutes on fee tiers.

- **The offline suite was not offline.** 78 tests, 74 of them runner tests, sent real REST requests: 77 read fee tiers (`GET /v2/feeTiers`) and one read balances, because their fixtures stubbed every other read. The guard refused only the connection, after a real DNS lookup of `api.reya.xyz`, so code that treats the read as optional (`except Exception`) passed. With name resolution stalled, such a test waits: `test_definitive_rejections_keep_the_session_and_ambiguous_outcomes_halt[failure1-command1-True]` then took 331 s and failed. The fixtures now stub those reads, the guard also refuses name resolution, and a test that attempts the network fails, even when the code swallows the refusal.
- **Fee-tier reads give up after 3 s.** The runner read fee tiers at start-up, before its `ready` event, with no bound but the SDK's 5 minutes per request. Every order preview reads them for the after-fill estimate too (inside the runner, only when its 60 s cache is empty). Fee tiers only refine estimates and never gate an order, so after 3 s the runner starts without them and the order goes ahead with `taker_fee_rate_assumed: null`, as when the read fails.
- **A stuck test prints its stack.** pytest's `faulthandler_timeout` is 60 s: a test still running then dumps every thread's stack to the log, well before CI's 15-minute job timeout. No new dependency.

Not exercised live: no orders were placed for this release.

## 0.11.1 — 2026-10-19

The runner on a venue that indexes fills slowly. On devnet1 on 19 October a fill settled on chain within about a second of its acknowledgement, but the feed and REST showed it 12–13 s later. That was an episode: over six days to 19 October, mainnet's indexer consumed fills a median of about 1 s after their block (p99.9 2.1 s, worst minute 4.2 s). So the runner now ends every wait on evidence and bounds it by a per-deployment indexing ceiling: 15 s on mainnet, 30 s elsewhere.

- **An unshown fill keeps counting, or the order is refused.** The runner's own acknowledged fill counted in the guards for only 5 s. After that it was dropped, the wallet resubscribed and the next order read REST, which lacked the fill too, so an order sent 5–13 s after a fill was checked without it. Now the fill counts until the feed shows it, up to the indexing ceiling. When the runner cannot place it in the position, an order that needs the account checks is refused before sending with `no order submitted` instead of being checked against REST. That covers a position that changed in a way that may or may not include the fill, a socket that dropped or resubscribed (its new snapshot may or may not include the fill), and a missing fee bound or mark. The session continues and there is nothing to reconcile. A reduce-only `order market` close is still sent, as when the account read fails, and the engine decides. A fill not shown within the ceiling (busted, or missed by the feed) is dropped and REST decides, as before. `order market --sl/--tp` refuses the same way rather than run its checks before the entry on REST.
- **The fee and realized-loss debit lasts as long as the fill.** It expired 10 s after the acknowledgement, before devnet showed the fill, which overstated the balance until then. It now stays until 10 s after the feed shows the fill.
- **A stop right after a fill can reconcile.** The settlement reads at stop made their three observations within about 1.4 s and gave up before the fill was indexed, so the session stayed unreconciled and the next one needed `--ack-previous-session`. Within the indexing ceiling the runner now waits for its warm feed to show its own fills, which costs no request, then reads settlement with backoff (0.5, 1, 2, then 4 s apart) for what remains, at least 2 s. The first definitive answer ends the reads. A `stop` straight after a fill therefore takes about as long as the venue takes to index it. The fee reads still pending at stop also run after that wait, so they find the fill.
- **The `stopped` event names fills settlement has not seen.** One session reported `final_state.positions: []` and `protection: []` while the account held an unsettled fill; only the settlement reason pointed at it. The event and the journal now carry `unconfirmed_fills`: each acknowledged fill (`symbol`, `order_id`, `side`, `filled_qty`) settlement has not confirmed. The error names them and says the final position reads can leave them out. When the settlement reads themselves fail, every acknowledged fill is listed.

Not exercised live: no orders were placed for this release. Offline tests cover an order 13 s after a fill that is still checked with it, the ceiling per deployment, a resubscribe while a fill is pending, the refusal and the close that still goes out, a stop that waits for the feed and then reconciles, the settlement backoff, and the event's list.

## 0.11.0 — 2026-10-19

`paper watch`, for the tester rehearsing leveraged orders in paper: since 0.10.0 paper fires TP/SL and liquidates only when a command runs, so a dip that recovers between commands was missed. That was paper's main lenient difference from live, where the venue fires at the first mark through the trigger.

- **`paper watch [--interval S] [--duration S]`** makes the observation of `paper state` every 2 s by default (at least 1 s), in the foreground, until `--duration`, Ctrl-C or SIGTERM. It also stops by itself when the ledger has no position or TP/SL left, and after a liquidation. Each tick reads only public GETs: the market summary and definitions together, plus the book of each market whose TP/SL the mark has reached. Two reads a tick is 60 a minute at 2 s, against the edge's 1,500 a minute per IP for reads. V2 marks move every 500 ms, so the 1 s floor sees every other one.
- **JSON lines.** `watch_started`; each `stop_loss`, `take_profit` or `liquidation` as recorded in `history`; `clock_warning` / `clock_ok` when the skew crosses 2 s / falls under 1 s; one `watch_warning` when 3 ticks in a row made no observation; and a final `watch_summary` with `stopped_by`, `ticks`, `observations`, `skipped` by reason, `max_gap_ms`, `events`, `peak_margin_usage`, `account` and `clock`.
- **Other paper commands keep working.** Reads hold no lock; the ledger is locked only for the milliseconds it takes to apply an observation. A tick that finds the ledger locked is skipped, not waited for. An observation is applied only to the exact ledger its reads were planned on: if another command changed it meanwhile, the reads are discarded and the tick is planned again once. No TP/SL fill or liquidation is applied twice, none fires on a mark read before its trigger changed, and events are printed only after they are saved.
- **Saves only on change.** A quiet tick writes nothing; a fill, a liquidation, a dropped trigger or a funding anchor is saved. `last_safe_observation_ms` on a liquidation is still the previous tick. No `refresh` snapshots are recorded: at 2 s the 10,000 bound would fill in about 5.5 hours.
- **Failures.** A failed read, stale data or data outside the clock band skips the tick; nothing is retried with the same data. HTTP 429 (the edge's per-IP ban) stops watch at once, exit 1, without retrying. A ledger reset under it stops it too.
- SKILL.md: after a paper entry with a stop-loss, offer `watch`; always pass `--duration`, run it in the background with output to a file when it outlasts the command timeout, report fills and liquidations first, and `max_gap_ms` as the longest stretch paper did not see the mark. `docs/paper-trading.md` has a section on watching between commands; the faithfulness notes now say observations, not commands.
- V2 reads that answer with a status other than 200 raise `V2StatusError` (still a `ValueError`, same message), so callers can tell a 429.

Watch narrows the gap to about the interval; it does not close it. A move that reverses within `max_gap_ms` is still missed.

Not exercised live: no orders were placed. Against devnet1's public data with a scratch ledger: an 8 s watch made 4 observations (largest gap 2.04 s, clock offset −0.1 s); a take-profit armed with `--force` below the mark fired on the first tick against the devnet book and watch stopped `flat`; 12 back-to-back `paper state` runs and an order during a 10 s watch all succeeded while watch skipped 1 tick for the lock; SIGINT and SIGTERM each ended a running watch with its summary and exit 0.

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
