# Hosted strategies

A strategy is a set of rules that Reya's hosted strategy service runs for you on your Agent
account, with your computer off: "buy 0.1 ETH every day for ten days, with a stop-loss 10%
below the entry and a $300 loss limit". The same service runs the strategies the Reya app and
its chat create, so a strategy started by the kit shows in the app, and one started in the app
shows in `strategy status`. The kit is a thin client of that service:

- Strategies are `reya.workflow/3` documents: states, rules that fire on a trigger, the
  actions a rule takes, and where it goes next. `draft` writes one from flags: a **basket**
  of immediate orders entered at the start, when a mark crosses a level, or every period,
  with a stop-loss and a take-profit from the entry, a time exit and a loss limit. It is the
  same shape the in-app chat drafts, and the kit keeps it only once the service has checked
  it. The service takes whole documents and offers no template, so this is the one document
  the kit writes; `validate`, `preview` and `start` also take one you wrote ([your own
  document](#your-own-document)).
- `validate` and `preview` ask the service. The service is the only evaluator: the kit
  never decides on its own whether a rule would fire, and there is no replay of past prices.
- `start` and `stop` are signed requests (EIP-712, `StrategyStart` and `StrategyStop`) signed
  with the agent key that `agent init` generated, never with your wallet's key. The key is
  never printed.
- `status` reads a strategy back from the service, with a read grant the agent key signs
  ([below](#private-reads)).

A strategy runs in one of two modes. **Paper** (`paper`, the default) simulates its fills on the
strategy's own book and sends no orders; **Live** (`live`) sends real orders on the Agent
account. `strategy info` lists the modes the environment starts now. Every strategy, Paper ones
included, also needs hosted runs enabled for the wallet: a one-time approval in the Reya app
([below](#enabling-hosted-runs)).

An Agent account runs **one Live strategy at a time**, and Paper ones beside it (the preview's
`paper.max_active_runs` says how many). Strategies of the previous version (`reya.workflow/2`)
no longer run: a document of that version is refused (`PREVIOUS_VERSION`), and an old run id
(below 3,000,000) reads as `RUN_ON_PREVIOUS_VERSION`. Draft it again.

## Commands

```bash
python -m agent_kit.scripts.strategy info [--open]
python -m agent_kit.scripts.strategy draft --order SYMBOL:buy|sell:USD|QTY<ASSET> [--order ...] \
    [--trigger start|price:SYMBOL:up|down:LEVEL|every:PERIOD] [--rounds N] [--first-now] \
    [--stop-loss PCT] [--take-profit PCT] [--exit-after DURATION] [--max-loss USD] \
    [--max-slippage-bps 25] [--out FILE] [--force]
python -m agent_kit.scripts.strategy validate --file FILE [--mode draft|paper|live] [TERMS]
python -m agent_kit.scripts.strategy preview --file FILE [--mode paper|live] [TERMS]
python -m agent_kit.scripts.strategy start --file FILE [--mode paper|live] [TERMS] [--no-open]
python -m agent_kit.scripts.strategy status [RUN_ID] [--all [--limit N] [--before RUN_ID]]
python -m agent_kit.scripts.strategy stop RUN_ID [--keep-position] | --all
```

`TERMS` are the run's terms, which are not in the document: the service records them with a
preview, and a Start signs that record.

| Term | Meaning |
| -- | -- |
| `--expires 7d` | When the run ends: `7d`, `12h`, `90m`, Unix seconds or an ISO time with a zone; at least an hour and at most 365 days less 5 minutes ahead (the service judges it on its own clock; `364d` always passes; default 7 days). At its end a strategy keeps what it holds and sends nothing new. |
| `--fire-now` | A price crossing already met at the start fires at once. Without it, the strategy waits for a fresh crossing. |
| `--label NAME` | The strategy's name, up to 64 characters, shown in status and in the app. |
| `--paper-balance USD` | Paper: the balance its margin starts from (default: the account's balance, read once at the preview). |
| `--paper-start empty\|copy` | Paper: start flat, or copy the account's settled positions in its markets (default: copy when the account holds one, else empty). |
| `--paper-slippage-bps N` | Paper: the slippage every simulated fill assumes, 0 to 100 (default 1). |
| `--paper-fee-tier TIER` | Paper: a named fee tier the preview lists in `paper.fee_tiers` (default: the account's own rate). |
| `--account-id ID` | The recorded Agent account, when several are recorded. |

### `info`

The service's environment, release (`service_version`), hosted signer (`hosted_signer`), the
modes it starts now (`modes`, with `mode_names`), whether it checks against accounts
(`account_checks`), the trading whitelist rule (`trading_whitelist`), what it runs
(`accepted`: triggers, actions, stop levels, states) and the shapes it refuses in this release
(`refused_shapes`). It also says whether the owner has approved the hosted signer
(`hosted_signer_enabled`, [below](#enabling-hosted-runs)). Read-only.

### `draft`

Writes a basket from flags, then asks the service to check it in draft mode (against the
Agent account when one is recorded; without setup, the document alone). The file is written
only when the check does not refuse it, to `--out` or the kit's state directory
(`workflows/`), in its canonical form: the version hash a Start signs is over those bytes.

| Flag | Meaning |
| -- | -- |
| `--order SYMBOL:buy\|sell:SIZE` | An immediate order, sent at once when the entry fires. SIZE is USD as a plain number (`500`) or a base quantity with the market's base asset after it (`1SOL`, `0.05BTC`). One order per market; up to 16. |
| `--trigger start` | The entry fires right after the start (the default). |
| `--trigger price:SYMBOL:up\|down:LEVEL` | The entry fires when SYMBOL's mark price crosses LEVEL. |
| `--trigger every:PERIOD --rounds N` | The entry fires every PERIOD (`90m`, `4h`, `1d`, `1w`), counted from the start, N times in all (1 to 100); then the strategy keeps what it bought, its stops still watched. `--first-now` makes the first round at the start. |
| `--stop-loss PCT`, `--take-profit PCT` | On every market, from the entry price. When one is hit, the strategy closes what it holds and ends. |
| `--exit-after DURATION` | After the entry, close everything and end once this long has passed (not with `every`). |
| `--max-loss USD` | The strategy's loss limit: past it, everything is closed and it ends. |
| `--max-slippage-bps N` | How far from the mark each immediate order may fill (default 25). |

`draft` also writes the two settings this release runs (`on_liquidation: close_and_end`,
`on_margin_skip: keep_running`). It refuses, before asking anything, a market given twice, an
`every` trigger without `--rounds`, and more than six exits and entries on one step.

The result has `file`, `rules` (the strategy as sentences, step by step), `markets`, the
service's check (`verdict`, `refusals`, `warnings`, `notes`, `figures`, `readouts`, `what_if`,
`takes_over`; see [preview](#preview)), `version_hash`, `document` and `next`. A refused draft
returns `ok: false` and writes nothing.

Example: `strategy draft --order eth:buy:0.1ETH --trigger every:1d --rounds 10 --stop-loss 10
--max-loss 300` writes the format's own example E1 byte for byte.

### `validate`

Checks a document with the service. `--mode draft` (the default) checks the document alone, or
against the Agent account when one is recorded; `--mode paper` and `--mode live` check a run of
it on the account, with the terms. Unsigned; the service writes nothing and keeps no record.

### `preview`

What a Paper (the default) or Live run of the strategy would do now, against the Agent account,
with its terms. Unsigned. The result:

| Field | Meaning |
| -- | -- |
| `verdict` | `pass`, `warning` or `refusal`. A refusal means a start in this mode is refused now. |
| `refusals`, `warnings`, `notes` | The service's issues: `code`, `message`, and `path` or `block_id` where in the document. A Paper issue that would refuse a Live start carries `live_would_refuse: true`. |
| `figures` | The main readouts in plain text: `margin_needed`, `additional_deposit_needed`, `worst_deployed`, each "at most" or "at least" as the service bounds it, or `unknown (reason)`. |
| `readouts` | The service's figures as it sends them: margin needed by part (margin, fees, funding), liquidation per market, loss at each stop, each loop's rounds and cost. |
| `what_if` | One row per thing that can happen (a rule firing, liquidation, a margin skip, the loss limit, expiry, a refused order): what the strategy does then. |
| `takes_over` | What a Live start would take over from the account: its positions, resting orders and armed stops. A strategy leaves the trader's own stops as they are. |
| `paper` | Paper: the run's settings as resolved (slippage, fee, margin source and amount, start), what it copies and does not, and the account's active Paper runs. |
| `preview_id`, `strategy_id`, `valid_until` | The preview record, kept ten minutes. |
| `mode_allowed: false` | The environment does not start this mode now. |

`next` gives the `start` command with the same terms.

### `start`

Previews the document again with its terms and signs that preview: `StrategyStart` carries the
account, the version hash, the expiry, the mode, `fireNow`, the preview's `previewId`, an empty
`handoverStops` (the kit moves no exchange stop into a strategy), the agent key as signer, a
fresh nonce and a five-minute deadline. So what starts is what the service checked a moment
before. While that preview refuses, or gives no record, nothing is signed: `START_BLOCKED`,
with the refusals and a `next`. Before signing, the kit also checks that the environment starts
this mode, that a hosted signer is active and not the agent key, that the account is an Agent
account, and that the service's version hash is the hash of the document it holds.

The result: `started`, `run_id`, `strategy_id`, `mode` and `mode_text`, `label`, `markets`,
`rules`, `version_hash`, `preview_id`, `signer`, `hosted_signer`, `expires_at`, `fire_now`,
`warnings`, `figures`, `status_url`, `app_run_url`, `app_runs_url`.

### `status`

`strategy status RUN_ID` reads one strategy: `text` ("#3000093 DCA ETH: Paper, running, ETH"),
`state` (`active`, `held`: it keeps its positions after a final step, its stops still watched,
`stopped` or `completed`), `status` (`running`, `holding`, `closing`, `ended`), `end_reason`,
`markets`, `holds_markets`, `since`, `expires`, `needs_you_count`, and, under the agent key's read
grant ([below](#private-reads)):

- `result`: the strategy's result since its start (`total`, `settled`, `unrealised`, and
  `parts`: realised, funding, fees, deleveraging, penalty), or at its end.
- `positions`: each market's quantity, entry, mark and unrealised result.
- `protection`: each stop-loss and take-profit level with its price (`priced_from` the entry
  or a fixed price), whether it `fired`, the loss limit, and `stops_paused`.
- `needs_you`: open items the service needs the user for.
- `paper`: a Paper run's settings, balance and resting orders.
- `rules`: the strategy as sentences; `activity`: its newest events (code, rule, time).

Without a verified grant the service shows none of these: `detail: null` with
`detail_reason: read_grant_required`, and a `note` on what to do.

`strategy status` lists the account's running strategies (Live and Paper), with the account's
Live strategy (`live_strategy`) and, when exactly one runs, its full status. `--all` lists ended
ones too, newest first, `--limit` at a time: when `next_before` is set, older ones follow
(`--before RUN_ID`).

### `stop`

Signs `StrategyStop` for the run's account and strategy. By default it **closes the strategy's
positions, then ends** (`status` `closing`, then `ended`). `--keep-position` (Live only) ends
it at once and leaves its positions open as yours, with no stop-loss from the strategy; a stop
you placed on the exchange yourself stays. A Paper strategy stops only by closing. Stop does not
cancel other orders on the account. A strategy that has already ended is not stopped again:
nothing is signed (`already_ended`). `stop --all` stops every running one among the account's newest 1,000
strategies, one signed stop each. It exits with an error when any of them was not stopped (`STOP_INCOMPLETE`,
each one's own error in `stopped`), or when the account has more strategies than that (`LISTING_TRUNCATED`): then an
older one may still be running, and it never says none is.

## What a strategy does

- A rule fires on its trigger: the start, a crossing of the mark price, a timer, or a
  stop-loss or take-profit level. A crossing fires on the first fresh mark past the level
  after one on the other side; with `--fire-now`, one already met at the start fires at once.
- An immediate order is sent at once, within `max_slippage_bps` of the mark: it fills there or
  not at all on a thin book. A basket's orders go out one by one, not atomically: one can be
  refused while the others fill, and the strategy keeps what filled.
- A stop-loss or take-profit is a rule the service watches on the mark, not an order resting
  on the exchange: `protection.stops_paused` (and `live_strategy.stops_paused`) says when the
  service is not watching them (for example while it restarts).
- A final `done` step closes what the strategy holds and ends it; a final `hold` step keeps
  the positions, opens nothing new and still watches its stops.
- The loss limit (`max_loss_rusd`) closes everything and ends the strategy once its loss passes
  it; liquidation closes and ends it; an order the margin cannot take is skipped and the strategy
  keeps running.
- This release sends immediate orders only: a strategy with resting limit orders
  (`limit_orders`) is refused at the start (`resting_limits_unavailable`), though it can be
  previewed.

## Your own document

`validate`, `preview` and `start` take any `reya.workflow/3` document, spelled any way JSON
allows; the kit sends its canonical form (keys sorted, no whitespace). The kit reads it as
strictly as the service does: no repeated key, no fractional JSON number (decimals are
strings), no integer beyond ±(2^53 − 1), at most 12,288 bytes. The format's examples are in
`agent_kit/tests/fixtures/workflow_v3_examples` (copied unchanged from the service), and
`strategy info` lists what this release runs (`accepted`) and refuses (`refused_shapes`). The
service checks everything else, and its refusals name the block (`block_id`) and the `path`.

## Enabling hosted runs

The hosted signer places a strategy's orders, so every start, Paper ones included, needs the
owner to have approved it once, the same kind of approval as the agent key. In the Reya app this
is the last step of the setup page that `agent init` opens, **Enable hosted runs**: one
signature from the owner wallet, with the hosted signer's address shown to compare with
`hosted_signer`.

`agent init` offers this step once setup is `ready` (`hosted_runs.offer`); see the
[command reference](command-reference.md#agent-setup-agentpy). `enable_url` is that page's
link: `/agents/setup?agent=<agent key>&owner=<owner>&account=<Agent account>#access=<code>`.
The access code is only in the part after `#`.

- `strategy info` checks the approval the way `agent status` checks the agent key's (the
  owner's approved signers from the trading API's API-wallet status). With
  `hosted_signer_enabled: false` it returns `enable_url` and opens nothing; `strategy info
  --open` also opens it, as a refused `start` does.
- A start refused with `HOSTED_SIGNER_NOT_ENABLED` returns `enable_url` and opens it, once,
  and not again while it is open (`link_already_opened: true`). When no browser can open on
  this machine it says `opened: false` and `browser_unavailable`, and prints the link.

Once the owner has approved, run the same `start` again.

## In the Reya app

`start`, `status` and `stop` link the strategy's pages in the Reya app for the deployment (the
same app as the `agent init` setup page):

| Field | Page |
| -- | -- |
| `app_run_url` | `/agents/strategies?id=<run_id>#access=<code>`: one strategy, with Stop for the connected owner. |
| `app_runs_url` | `/agents/accounts?account=<account_id>#access=<code>`: the Agent account's page, with its strategies, funds and positions. |

Like the setup page, these open only with the kit's access code in the part after `#`, which
the browser never sends to the server. Both are `null` when no app is known for the deployment
(set `REYA_APP_URL`). The app has no page yet that reviews a kit draft of this version, so
`draft` and `preview` link none: to start a strategy in the app, use the app's chat.

## Private reads

A run shows the whole strategy, so the service makes run reads private: under a read grant it
verifies, `GET /v1/runs/{id}` carries the run's detail. The kit sends one on the run reads
(`GET /v1/runs/{id}`, its activity and `GET /v1/accounts/{id}/runs`) and on the checks that
name the Agent account (`POST /v1/validate`, `POST /v1/preview`), and on nothing else:

- **What it is.** An EIP-712 `ReadGrant` under the same domain as start and stop
  (`Reya Workflows`, version `1`): `verifyingChainId`, `environment`, `owner`, `accountIds`
  (the recorded Agent accounts), `expiresAt` (one hour) and a random 16-byte `salt`. It is
  signed with the agent key, an approved delegate of the owner; never with the owner's key, so
  with `REYA_SIGNER=own-key` no grant is sent.
- **How it travels.** `Authorization: Bearer <token>`, where the token is base64url without
  padding of the JSON `{"grant": {...}, "signature": "0x..."}`.
- **Reuse.** The grant is kept in the state directory (`workflow-read-grant.json`,
  owner-only) and reused until 80% of its hour has passed, then replaced.
- **Refusals.** If something in front of the service (a gateway) refuses a read that carried a
  grant, without the service's own error, the kit asks once more without it and drops the kept
  grant. The service's own refusals stand and are never asked again without the grant:
  `INVALID_READ_GRANT` (the kept grant is dropped), `READ_GRANT_REQUIRED` and `RUN_NOT_FOUND`
  (also for a run the grant does not cover). The grant never appears in a result or an error.

## Errors and lost answers

A refusal from the service carries `error_code`, `retryable`, `http_status`, `request_id`
(quote it when reporting a problem), `retry_after_s` when the service asks for a pause, and
`next` for the common codes:

| Code | What to do |
| -- | -- |
| `START_BLOCKED` (the kit, before signing) | The fresh preview refuses this mode; nothing was signed. Clear what each refusal names, then start again. |
| `HOSTED_SIGNER_NOT_ENABLED` | The owner approves the hosted signer on the Enable hosted runs step (`enable_url`), then starts again. |
| `NOT_AGENT_ACCOUNT` | Nothing was signed or started. Follow `next`: `agent init --account-id <id>` switches the kit to the wallet's Agent account (`agent_account_fix`). |
| `NOT_PERMITTED` | Run `agent status`. |
| `NOT_WHITELISTED` | The owner is not whitelisted for trading on this deployment; no strategy can start, Paper included. |
| `ACCOUNT_STRATEGY_LIMIT` | The account already runs its one Live strategy (`next` names it): stop it first, or start this one in Paper. |
| `START_REFUSED` | By `details.reason`: `card_expired` (the preview is gone: start again, it previews afresh), `card_mismatch` or `start_recheck_worse` (the account changed: preview again), `start_checks_refused` (`details.issues`), `preview_used`, `trades_any_market`, `funding_crossing` (only mark crossings fire in this release). |
| `INVALID_REQUEST` | By `details.reason`: `resting_limits_unavailable` (immediate orders only), `paper_balance_needed` (pass `--paper-balance`), `paper_keep_refused`, `expires_at_too_soon`, `odds_source_off`. |
| `STOP_REFUSED` | By `details.reason`: `run_ended`, `already_keeping`, `keep_blocked_by_close`. |
| `NONCE_TOO_LOW`, `REQUEST_EXPIRED`, `DEADLINE_TOO_FAR` | Nothing happened; run the same command again (check the clock). |
| `MODE_NOT_ALLOWED` | Use a mode `strategy info` lists. |
| `RUN_ON_PREVIOUS_VERSION`, `PREVIOUS_VERSION` | A strategy of the previous version: draft it again. |
| `RATE_LIMITED`, `BUSY`, `NOT_READY`, `ACCOUNT_STATE_UNAVAILABLE`, `STORE_UNAVAILABLE` | Retryable: wait `retry_after_s`, then try again. |
| `INVALID_READ_GRANT`, `READ_GRANT_REQUIRED` | Run `agent status`, re-approve the agent key if it asks, and check this computer's clock. |

If a start's answer is lost (a timeout, or an answer the kit cannot read), the kit reads the
account's newest strategies once: when one carries the preview's `strategy_id`, the result says
`confirmed_by_read: true` (a preview record starts at most one run). Otherwise it returns
`outcome: "unknown"` with that `strategy_id` and resends nothing; run `strategy status --all`
with the same `--account-id` as the start (the result's `next` gives it) before starting again. A stop whose answer is lost is confirmed by one read of the strategy
(it is closing or has ended); otherwise, and also when that read fails, `outcome: "unknown"`, and nothing is
resent. A second
`stop` of an ended strategy changes nothing.

## Common shapes

| Idea | `draft` flags |
| -- | -- |
| Recurring buy (DCA) | `--order eth:buy:100 --trigger every:1d --rounds 10 [--first-now] [--stop-loss 10] [--max-loss 300]` |
| Dip buy | `--order sol:buy:200 --trigger price:sol:down:120 --stop-loss 5 --take-profit 8` |
| Breakout | `--order btc:buy:0.01BTC --trigger price:btc:up:100000 --stop-loss 3` |
| Pair | `--order eth:buy:500 --order btc:sell:500 --trigger price:eth:down:2800 --stop-loss 5 --exit-after 5d` |
| Time-boxed trade | `--order eth:buy:250 --take-profit 4 --exit-after 6h` |
| TWAP | `--order eth:buy:50 --trigger every:5m --rounds 20 --first-now` (the total is each buy × rounds) |

Every order must be at least its market's minimum (`query market info --symbol X`).

## Beyond what a strategy does

A basket covers one entry on one or more markets with its exits. The format does more (loops
through earlier steps, conditions on funding, rules that read outside odds): write the document
yourself ([your own document](#your-own-document)) and let the service check it. For rules the
format cannot express (a grid that places the opposite order after each fill, trailing stops,
indicators, sizes that follow a hedge, signals from sources the service does not read), write the rule yourself
against the API: [from strategies to the API](from-workflows-to-the-api.md) maps each part of a
strategy to API orders and points to runnable examples.

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
