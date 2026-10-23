# Hosted workflows

A workflow is a rule that Reya's hosted workflow service runs for you on your Agent
account, with your computer off: "buy $25 of BTC each time the mark price crosses down
through 90,000, if the 15-minute average funding is below 0.001%/h, up to $200, until
26 October". The kit is a thin client of that service:

- `draft` writes the workflow document on this computer. Nothing is sent.
- `validate` and `preview` ask the service. The service is the only evaluator: the kit
  never decides on its own whether a trigger would fire, and there is no replay of past
  prices.
- `start` and `stop` are signed requests (EIP-712) signed with the agent key that
  `agent init` generated, never with your wallet's key. The key is never printed.
- `status` reads the run back from the service, with a read grant the agent key signs
  ([below](#private-run-reads)).

Workflows are a private pilot. Each environment says which modes it allows (`workflow
info`); a run in `shadow` mode records every decision and sends no orders. Every run,
shadow included, also needs hosted runs enabled for the wallet: a one-time approval in the
Reya app ([below](#enabling-hosted-runs)).

## Commands

All print JSON and exit 0 on success or 1 with an `error` field, like every kit command.

```bash
python -m agent_kit.scripts.workflow info [--open]
python -m agent_kit.scripts.workflow draft --symbol btc --direction down --level 90000 --notional 25 --allocation 200 --expires 7d [options]
python -m agent_kit.scripts.workflow validate --file FILE
python -m agent_kit.scripts.workflow preview --file FILE
python -m agent_kit.scripts.workflow start --file FILE [--fire-now | --wait-for-rearm] [--mode shadow] [--no-open]
python -m agent_kit.scripts.workflow status [RUN_ID] [--all] [--account-id ID]
python -m agent_kit.scripts.workflow stop RUN_ID [--account-id ID]
```

### `info`

The service's `environment`, `chain_id`, EIP-712 `eip712_domain`, the active
`hosted_signer` (the address that places the run's orders), the `modes` this environment
allows and the pilot `limits` (`max_allocation_usd`, `max_order_usd`). The kit reads all
of these from the service on every signed request instead of building them in.

It also says whether hosted runs are enabled for the wallet ([below](#enabling-hosted-runs)):

| Field | Meaning |
| -- | -- |
| `hosted_signer_enabled` | `true` when the owner has approved `hosted_signer`, `false` when not, `null` when the kit could not tell: no Agent account recorded yet (`next` says to run `agent status`), or the approvals could not be read (`hosted_signer_note` says why). Absent when the service names no active hosted signer. |
| `enable_url` | With `false`: the setup page's Enable hosted runs step in the Reya app, for this agent key, owner and Agent account. `null` when no app is known for the deployment. |
| `next` | With `false` or `null`: what to tell the user. |

`info` is read-only and signs nothing. It opens nothing unless you pass `--open`. Then, with `false`, it opens `enable_url` once, like a refused start, and reports `opened`, `link_already_opened` or `browser_unavailable` ([below](#enabling-hosted-runs)).

### `draft`

| Option | Meaning |
| -- | -- |
| `--symbol` | The market, `btc` or `BTCRUSDPERP`. |
| `--direction down\|up` | `down`: fires when the mark moves from above the level to at or below it. `up`: the reverse. |
| `--level` | The trigger level in USD. |
| `--side buy\|sell` | The order when it fires; `buy` by default. |
| `--notional` | USD per firing. |
| `--allocation` | USD in total over every firing. The run completes when it is used. |
| `--expires` | `7d`, `12h`, `90m`, Unix seconds, or an ISO time with a zone. The run completes then: it sends no new orders, and a protective stop it armed stays on the position ([below](#the-protective-stop)). |
| `--repeat each_crossing\|once` | Fire on every crossing (re-arming after each), or once. |
| `--bound-bps` | How far from the mark an order may fill, 10–100 bps; 25 by default. |
| `--funding OP:PCT_PER_HOUR` | Condition on the 15-minute average funding, e.g. `lt:0.001`. Repeatable. |
| `--position OP:QTY` | Condition on the position in base units (long positive), e.g. `lt:0.5`. |
| `--stop-px` | Optional whole-position stop price, below the level and the mark for a buy (above for a sell). Never placed at start: the service arms it once the run's first fill settles ([below](#the-protective-stop)). |
| `--account-id` | The Agent account, when more than one is recorded. |
| `--out FILE` | Where to write it; by default the kit's state directory. `--force` replaces a file. |

`OP` is `lt`, `lte`, `gt` or `gte`. There is no condition on the available balance. The
service has no source for it, so a run with one would skip every crossing as
`skip_balance_unavailable`, and newer service releases refuse one at `validate`, `preview`
and `start` (`available_balance_unsupported`). The result has `file`, the `document`,
`workflow`, the rule in one sentence, and `app_review_url`, the page in the Reya app where
the user reviews the draft and can start it ([below](#in-the-reya-app)). The document format
is the service's `reya.workflow/1`; amounts and prices are decimal strings.

### `validate`

The service checks the document against the live market. On success: `version_hash` and
`canonical` (the document in its canonical form, defaults filled in), plus `warnings`. An
invalid document returns `ok: false`, `error` and every reason in `errors`.

### `preview`

What the document would do right now, from the same evaluator the live runs use:

- `now`: the service's first line: the mark and how far the level is from it ("the mark is
  91,420; this would arm 1.55% below, at 90,000"), or that the level is already past.
- `trigger`: `level`, `direction`, `distance_bps`, `distance`, `side` (where the level is
  relative to the mark) and `already_triggered`.
- `order_text`: one firing at the level as the service sizes it: side, quantity, limit
  price, the price bound, what it reserves of the allocation and how many such orders the
  allocation covers. When no order could be sent at the level, `order_error` says why.
- `sizing`: the per-firing amount against what a firing really sends. The service divides the
  draft's `--notional` by the limit (the level plus or minus the bound) and floors the quantity
  to the market's lot, so $27 at a limit of 2,512.188 sends 0.010 ETH, about $25.12. `sizing`
  gives `requested_notional_usd`, `effective_notional_usd` (quantity × limit, the amount
  reserved), the shortfall in dollars and per cent, and, when rounding took something off,
  `next_step_up`: the next quantity, its notional and `draft_notional`, the least `--notional`
  that reaches it. Its `message` is appended to `order_text`. The market's lot comes from the
  public market definitions; when that read fails, `sizing` still compares the two amounts and
  leaves out the next step.
- `funding_text`: the latest funding sample and the 15-minute average the conditions use,
  with the number of samples in the window.
- `warnings`, such as a level so far from the mark that it may be a typo, and `lines`: the
  service's own findings as plain text, one per line.
- `app_review_url`: the same draft's review page in the Reya app, for when the user starts
  it there rather than from the kit ([below](#in-the-reya-app)).

When the mark is already past the level, `already_triggered` says so: start with
`workflow start --fire-now` to fire on the first fresh mark, or with `--wait-for-rearm` to
wait until the mark moves back past the re-arm band (0.5% today) and crosses again. Preview
itself takes neither: what to do with a crossed level is the start's choice.

A level close to the mark can be crossed in the minute between the preview and the start.
Set it well away from the mark (about 0.5% or more) for a run that should wait for a move, or
choose `--fire-now` knowingly for one that should fire at once.

### `start`

Checks the document again with the service, then signs `WorkflowStart` with the agent key
and sends it. The signed request binds the document's version hash, the account, the
environment, the hosted signer, the allocation, the expiry, the mode, `fireNow`, a nonce
and a deadline five minutes ahead. Before signing, the kit refuses:

- any account that `agent init` did not record as the Agent account
  (`PERP_AGENT_ACCOUNT_IDS`), and `REYA_SIGNER=own-key`;
- a service that serves another environment or chain than the kit is configured for, or
  that publishes a signing domain or message the kit does not know;
- a canonical form from the service that changes anything in your document, or whose
  version hash is not its hash;
- a mode the environment does not allow, and an allocation or order size above the pilot
  caps;
- a level the mark has already passed, unless you chose `--fire-now` or `--wait-for-rearm`.
  The kit asks the service for a fresh preview just before signing; when `already_triggered`
  is true and neither was given, it signs nothing and returns `error_code:
  "ALREADY_TRIGGERED"`, the service's `now` line, the `trigger`, and a `next` naming both
  commands. A run started past its level would only wait for the re-arm band, which is not
  what a preview "this would arm 0.2% below" promised.

The result has `run_id`, `state`, `mode`, `signer` (the agent key's address),
`hosted_signer`, the allocation and expiry, `now` (the service's line from the preview just
before signing), `status_url`, and the run's pages in the Reya app, `app_run_url` and
`app_runs_url` ([below](#in-the-reya-app)). A start with `--fire-now` or `--wait-for-rearm`
on a level already passed adds `already_triggered`, saying which it does. The run arms on the
next fresh mark. One active run per Agent account; to change a workflow, stop it and start a new
one.

A start the service refuses with `HOSTED_SIGNER_NOT_ENABLED` started nothing. Its result
adds `hosted_signer` and `enable_url`, and the kit opens that page in the browser once
([below](#enabling-hosted-runs)); `--no-open` only returns the link.

### `status`

With a run ID, one run:

- `summary`: one line, e.g. "Run 7 (shadow) is active; armed at 90,000 (crossing down):
  mark 91,420 is 1.55% (155 bps) above it; $175.00 of $200.00 remaining ($25.00 spent,
  $0.00 reserved); 1 firing(s), 2 skip(s)."
- `armed`: the level, the latest mark and the distance (`text`).
- `phase` and `phase_text`: `starting`, `armed`, `waiting_for_reset`, `cooling` or `done`.
- `stale`: while inputs are stale nothing fires; `reason` names why (below).
- `firings`: each firing as "triggered at 90,000, executed at 90,012 (+1.3 bps), 0.0028
  BTC for $252.03", or why it did not fill, with its `settlement`.
- `skips`: each skipped crossing with its named `reason` and the service's own `text`;
  `skip_counts` by reason.
- `allocation`: total, reserved, spent and remaining.
- `protection`, `completion` and `stop`, each with a named reason.
- `app_run_url` and `app_runs_url`: the run's page and the Agent account's workflows page in
  the Reya app.

Without a run ID, the Agent account's active runs (`--all` for every run), each with its
`app_run_url`, the account's `app_runs_url`, and the full status of the active one.

### `stop`

Signs `WorkflowStop` with the agent key. When it returns, the stop is durable: no further
firing is sent, an order already sent completes, and the service cancels the protective
order the run armed, and nothing else: `protection.state` reads `cancel_pending` while the
cancel is in progress and `cancelled` (reason `run_stopped`) once it has succeeded.
**Positions stay open.** Treat them as having no stop from the run only once
`protection.state` is `cancelled`; if it reads `cancel_failed`, the stop can still fire:
clear it with `cancel_all --symbol <market>` (preview first). Close positions separately if
you want to be flat. A completed run can be stopped too: that is how the stop it kept (after
its `once` step, its allocation or its expiry) comes down. Stopping a stopped
run changes nothing (`already_stopped: true`). The result links the run's pages in the app,
like `start`.

## The protective stop

A document with `--stop-px` asks the service for one stop-loss on the whole position of
the run's market, signed by the hosted signer:

- **When it arms.** Never at start. Once one of the run's fills settles, the service arms
  the stop (`arming`, then `armed`). It covers the whole position, so buys that later
  firings add are covered too. A run with no settled fill reads `pending`.
- **The order.** A stop-loss on the closing side, triggered at your price on the market's
  tick, rounded away from the mark, with its limit at the edge of the venue's band around
  the trigger (3% today). When the mark touches the trigger, the venue
  closes the position with an IOC order down to that limit. `protection` shows
  `trigger_px`, `limit_px`, `order_id`, `client_order_id` and `armed_at`.
- **When it fires.** `protection.state` becomes `fired` and the run stops
  (`protection_fired`), so it does not reopen the position. `protection.fired` gives the
  quantity the close executed and its average price. The close is IOC, so it can fill
  only part of the position, or nothing when the book has nothing inside the limit; the
  run stops either way. Read the position back after a stop fires.
- **When the run stops.** The service cancels the stop it armed and nothing else
  (`cancel_pending` while in progress, then `cancelled`). The position stays open; it has no
  stop from the run only once the state is `cancelled` (after `cancel_failed` the stop can
  still fire).
- **When the run completes, or expires.** A run that fired its `once` step, used its
  allocation or reached its expiry completes and keeps its stop: expiry stops new orders,
  never protection. The stop stays on the position, watched, until it fires (recorded in
  `protection.fired`), the venue retires it, the run is stopped (`workflow stop RUN_ID`,
  which cancels it), or the owner clears it with `cancel_all --symbol <market>`.
  Status confirms retained protection only for `armed`: `arming` and `unknown` mean
  protection is not confirmed, while `cancel_pending` means cancellation is in progress
  and the stop may still fire until cancellation is confirmed.
- **While it cannot fire.** The venue skips stops while the mark is stale or the venue is
  halted; `protection.suppressed` then reads `mark_stale` or `venue_halted`.

What `protection.reason` means when the stop is not armed or was taken down:

| `state`, `reason` | What happened | Next |
| -- | -- | -- |
| `not_armed`, `account_has_stop` | The account already has a stop-loss on this market, which the service never touches. It covers the whole position, the run's buys included, at its own price. | The run keeps going and tries again after its next settled fill. |
| `not_armed`, `earlier_run_stop` | An earlier run's stop on this market is still armed and covers the position. | As above. |
| `not_armed`, `outside_band` / `account_suspended` / `refused` | The venue refused the stop. | As above. |
| `cancelled`, `position_closed` / `oco_sibling_fired` | The venue took the stop down: nothing left to close, or the account's take-profit fired. | Armed again after the next settled fill. |
| `cancelled`, `risk_rejected` | The stop fired and the venue's risk checks refused the close. **The position is open with no stop.** | Armed again after the next settled fill. |
| `cancelled`, `cleared_by_owner` | The owner's `cancel_all` cleared it. | This run arms no other stop. |
| `cancelled`, `run_stopped` | The run stopped and the service cancelled it. | — |
| `cancel_failed`, `hosted_signer_revoked` | The owner revoked the hosted signer, so the service can no longer cancel the stop. It can still fire, and its fill would not settle. | Clear it with `cancel_all --symbol <market>`. |
| `unknown` | The service lost the answer to an arm twice and never saw the stop on the venue. The run is blocked until it is seen. | `workflow status`; the service keeps reading the venue. |

`paper` runs simulate the stop: it arms at once after the first paper fill, fires when the
mark touches the trigger, closes the run's own paper position at the mark when that is inside
the limit, and stops the run (on the venue the stop would close the whole account position).
`shadow` runs arm nothing: they record the stop they would have armed, `not_armed` with
reason `shadow`.

## Enabling hosted runs

The hosted signer places a run's orders, so every start, shadow included, needs the owner
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

- `workflow info` checks the approval the way `agent status` checks the agent key's: the
  owner's approved signers from the trading API's API-wallet status, which the app's setup
  page reads too. With `hosted_signer_enabled: false` it returns `enable_url`, and opens
  nothing. `workflow info --open` also opens it, as a refused `start` does.
- A refused `start` returns `enable_url` and opens it, like the setup links: once, and not
  again while it is open. A link opened in the last 30 minutes, by `start` or by `agent init`,
  counts as open, and so does the setup link without `account` when the recorded account is
  the owner's one Agent account (`AGENTPERP`, or before the API reports that type, the one
  named "Agent account"): that page shows the same account. The result then says
  `link_already_opened: true`. When no browser can open on this machine (an SSH session, no
  display, WSL without Windows access), it says `opened: false` and `browser_unavailable`,
  and prints the link for the user to open on any device where their wallet is.

Once the owner has approved, run the same `start` again. Revoking the hosted signer on the
app's API page stops its runs (`hosted_signer_revoked`).

## In the Reya app

`draft` and `preview` link the draft's review page, and `start`, `status` and `stop` the
run's pages, in the Reya app for the deployment the kit is configured for (the same app as
the `agent init` setup page):

| Field | Page |
| -- | -- |
| `app_review_url` | `/agents/workflows/run?draft&agent=<agent>&owner=<owner>#access=<code>&doc=<document>`: the draft, shown from the service's own preview (trigger and distance, the order one firing sends, rules, funding, warnings), with the mode, the pilot caps, what its protective stop does and what Stop does. The owner, connected, starts it there: the app signs `WorkflowStart` with the owner's one-click trading wallet, or the owner's wallet, in shadow mode unless they choose another mode the service allows, and the page then becomes the run's page. Any other wallet is asked to switch. |
| `app_run_url` | `/agents/workflows/run?id=<run_id>#access=<code>`: one run, with its trigger and distance, rules, firings, skips and history, and Stop for the connected owner. |
| `app_runs_url` | `/agents/workflows?account=<account_id>#access=<code>`: the Agent account's runs, newest first. |

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

A run started in the app is the same run as one started by the kit, except that the owner's
wallet signed it rather than the agent key: `status` lists it, and `stop` stops it. A draft
reviewed in the app is never stored anywhere: Discard there only clears the page.

All three are `null` when no app is known for the deployment (set `REYA_APP_URL`, as for
the setup links) or when `REYA_APP_URL` is not a plain `https` URL; that never fails the
command.

## Named reasons

Skips (a crossing that did not fire):

| Code | Reason |
| -- | -- |
| `skip_condition_funding` | funding condition not met |
| `skip_funding_unavailable` | funding average unavailable |
| `skip_condition_position` | position condition not met |
| `skip_balance_unavailable` | available balance unavailable |
| `skip_firing_held` | firing held by an operator |
| `skip_below_minimum` | order below the market minimum |
| `skip_collar_too_tight` | market price band narrower than the bound |
| `skip_price_out_of_range` | price outside the venue's range |
| `crossed_while_stale` | crossed while data was stale, not fired |

`skip_balance_unavailable` comes only from a run with a condition on the available balance,
drafted with an older kit's `--balance` or written by hand. The service cannot check that
balance, so such a run skips every crossing this way: stop it, draft the rule again without
the condition and start a new run.

Stale inputs: `mark_stale`, `mark_from_future`, `venue_halted`, `account_stale`,
`funding_stale`, `permissions_stale`, `restarted`. A missed move is reported, never fired
late.

Stops: `user` (a signed stop), `permission_revoked` (the starter's trading permission was
revoked), `hosted_signer_revoked`, `outcome_unknown`, `operator`,
`protection_fired` (the run's protective stop fired), and `expired` on runs stopped at expiry
before the service changed expiry to a completion reason. Completions: `fired_once`, `allocation_used`, `expired` (the
run reached its expiry: no new orders, and its protective stop stays). A code the kit does not know yet is shown as itself, next
to the service's text.

Firing outcomes: `filled` and `partial` ("executed at"), `shadow` (recorded, nothing
sent), `past_bound` and `zero_fill` (not filled within the bound), `rejected`, and
`unknown`: the run is blocked, keeping its reservation and protection, until the order's
outcome is known.

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
| `NOT_AGENT_ACCOUNT`, `NOT_PERMITTED` | Run `agent status`. |
| `ACCOUNT_HAS_ACTIVE_RUN` | `workflow status`; stop the active run first. |
| `NONCE_TOO_LOW`, `REQUEST_EXPIRED` | Nothing happened; run the same command again (check the clock for `REQUEST_EXPIRED`). |
| `MODE_NOT_ALLOWED`, `ABOVE_PILOT_LIMIT` | Use a mode and amounts `workflow info` allows. |
| `ALREADY_TRIGGERED` (the kit, before signing) | The mark passed the level since the preview; nothing was signed. Ask the user: `start --fire-now` or `start --wait-for-rearm`. |
| `RATE_LIMITED`, `BUSY`, `NOT_READY`, `ACCOUNT_STATE_UNAVAILABLE`, `MARKET_DATA_UNAVAILABLE` | Retryable: wait `retry_after_s`, then try again. |
| `INVALID_READ_GRANT`, `READ_GRANT_REQUIRED` | A run read without a grant the service takes: run `agent status` and re-approve the agent key if it asks, and check this computer's clock ([private run reads](#private-run-reads)). |

If a start's answer is lost (a timeout, or an answer the kit cannot read), the kit reads
the account's active run once: when it is this document, started by this agent key, the
result says `confirmed_by_read: true`. Otherwise it returns `outcome: "unknown"` and
resends nothing; run `workflow status` before starting again. A stop whose answer is lost
is confirmed the same way, and running `stop` again is always safe.

## Environments

The service for each deployment:

| `REYA_API_URL` | Workflow service | App pages |
| -- | -- | -- |
| `https://api.reya.xyz/v2` | `https://api.reya.xyz/workflows` | `https://app.reya.xyz` |
| `https://api-devnet.reya-cronos.network/v2` | `https://api-devnet.reya-cronos.network/workflows` | `https://app-devnet-cf.reya.xyz` |

The service is a path on the same API host: the kit sends `/workflows/v1/...` there, and the
host's gateway passes `/v1/...` on to the service.

Set `REYA_WORKFLOWS_URL` to use another service: its base URL, an origin with an optional base
path (`https://api.reya.xyz/workflows`), without `/v1`, credentials, a query or a fragment. Set
`REYA_WORKFLOWS_ENVIRONMENT` (`mainnet`, `testnet` or `devnet1`) for a custom `REYA_API_URL`.
`REYA_APP_URL` sets the app for the page links.
