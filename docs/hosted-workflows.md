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
- `status` reads the run back from the service.

Workflows are a private pilot. Each environment says which modes it allows (`workflow
info`); a run in `shadow` mode records every decision and sends no orders.

## Commands

All print JSON and exit 0 on success or 1 with an `error` field, like every kit command.

```bash
python -m agent_kit.scripts.workflow info
python -m agent_kit.scripts.workflow draft --symbol btc --direction down --level 90000 --notional 25 --allocation 200 --expires 7d [options]
python -m agent_kit.scripts.workflow validate --file FILE
python -m agent_kit.scripts.workflow preview --file FILE
python -m agent_kit.scripts.workflow start --file FILE [--fire-now] [--mode shadow]
python -m agent_kit.scripts.workflow status [RUN_ID] [--all] [--account-id ID]
python -m agent_kit.scripts.workflow stop RUN_ID [--account-id ID]
```

### `info`

The service's `environment`, `chain_id`, EIP-712 `eip712_domain`, the active
`hosted_signer` (the address that places the run's orders), the `modes` this environment
allows and the pilot `limits` (`max_allocation_usd`, `max_order_usd`). The kit reads all
of these from the service on every signed request instead of building them in.

### `draft`

| Option | Meaning |
| -- | -- |
| `--symbol` | The market, `btc` or `BTCRUSDPERP`. |
| `--direction down\|up` | `down`: fires when the mark moves from above the level to at or below it. `up`: the reverse. |
| `--level` | The trigger level in USD. |
| `--side buy\|sell` | The order when it fires; `buy` by default. |
| `--notional` | USD per firing. |
| `--allocation` | USD in total over every firing. The run completes when it is used. |
| `--expires` | `7d`, `12h`, `90m`, Unix seconds, or an ISO time with a zone. The service stops the run then. |
| `--repeat each_crossing\|once` | Fire on every crossing (re-arming after each), or once. |
| `--bound-bps` | How far from the mark an order may fill, 10–100 bps; 25 by default. |
| `--funding OP:PCT_PER_HOUR` | Condition on the 15-minute average funding, e.g. `lt:0.001`. Repeatable. |
| `--position OP:QTY` | Condition on the position in base units (long positive), e.g. `lt:0.5`. |
| `--balance OP:USD` | Condition on the available balance, e.g. `gte:50`. |
| `--stop-px` | Optional whole-position stop price. **The hosted service does not place stops yet:** it accepts the field, but a run started with it has no stop. When stops arrive, the service will place one only after the run's first fill settles, never at start. |
| `--account-id` | The Agent account, when more than one is recorded. |
| `--out FILE` | Where to write it; by default the kit's state directory. `--force` replaces a file. |

`OP` is `lt`, `lte`, `gt` or `gte`. The result has `file`, the `document` and `workflow`, the
rule in one sentence. The document format is the service's `reya.workflow/1`; amounts and
prices are decimal strings.

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
- `funding_text`: the latest funding sample and the 15-minute average the conditions use,
  with the number of samples in the window.
- `warnings`, such as a level so far from the mark that it may be a typo, and `lines`: the
  service's own findings as plain text, one per line.

When the mark is already past the level, `already_triggered` says so: start with
`workflow start --fire-now` to fire on the first fresh mark, or without it to wait until
the mark moves back past the re-arm band and crosses again. Preview itself takes no
`--fire-now`: firing on the first mark is the start's choice.

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
  caps.

The result has `run_id`, `state`, `mode`, `signer` (the agent key's address),
`hosted_signer`, the allocation and expiry, `status_url`, and the run's pages in the Reya
app, `app_run_url` and `app_runs_url` ([below](#in-the-reya-app)). The run arms on the next
fresh mark. One active run per Agent account; to change a workflow, stop it and start a new
one.

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
order the run armed (`protection.state` goes to `cancel_pending`, then `cancelled`). The
service places no stops yet, so today `protection` is `null` and there is nothing to cancel.
**Positions stay open**: close them separately if you want to be flat. Stopping a stopped
run changes nothing (`already_stopped: true`). The result links the run's pages in the app,
like `start`.

## In the Reya app

`start`, `status` and `stop` link the run's pages in the Reya app, for the deployment the kit
is configured for (the same app as the `agent init` setup page):

| Field | Page |
| -- | -- |
| `app_run_url` | `/agents/workflows/run?id=<run_id>#access=<code>`: one run, with its trigger and distance, rules, firings, skips and history, and Stop for the connected owner. |
| `app_runs_url` | `/agents/workflows?account=<account_id>#access=<code>`: the Agent account's runs, newest first. |

Like the setup page, these pages open only with the kit's access code in the part after
`#`, which the browser never sends to the server; the code is never in the query. Give the
user the whole link: opened without it, in a tab that has not opened a kit link before, the
page only says to start from the assistant. A start or stop whose outcome is unknown still
links the pages, which show what the service recorded.

Both are `null` when no app is known for the deployment (set `REYA_APP_URL`, as for the
setup links) or when `REYA_APP_URL` is not a plain `https` URL; that never fails the
command.

## Named reasons

Skips (a crossing that did not fire):

| Code | Reason |
| -- | -- |
| `skip_condition_funding` | funding condition not met |
| `skip_funding_unavailable` | funding average unavailable |
| `skip_condition_position` | position condition not met |
| `skip_condition_balance` | balance condition not met |
| `skip_balance_unavailable` | available balance unavailable |
| `skip_firing_held` | firing held by an operator |
| `skip_below_minimum` | order below the market minimum |
| `skip_collar_too_tight` | market price band narrower than the bound |
| `skip_price_out_of_range` | price outside the venue's range |
| `crossed_while_stale` | crossed while data was stale, not fired |

Stale inputs: `mark_stale`, `mark_from_future`, `venue_halted`, `account_stale`,
`funding_stale`, `permissions_stale`, `restarted`. A missed move is reported, never fired
late.

Stops: `user` (a signed stop), `permission_revoked` (the starter's trading permission was
revoked), `hosted_signer_revoked`, `outcome_unknown`, `expired`, `operator`. Completions:
`fired_once`, `allocation_used`. A code the kit does not know yet is shown as itself, next
to the service's text.

Firing outcomes: `filled` and `partial` ("executed at"), `shadow` (recorded, nothing
sent), `past_bound` and `zero_fill` (not filled within the bound), `rejected`, and
`unknown`: the run is blocked, keeping its reservation and protection, until the order's
outcome is known.

## Errors and lost answers

A refusal from the service carries `error_code`, `retryable`, `http_status`,
`request_id` (quote it when reporting a problem), `retry_after_s` when the service asks for
a pause, and `next` for the common codes:

| Code | What to do |
| -- | -- |
| `HOSTED_SIGNER_NOT_ENABLED` | Approve the hosted signer (`workflow info`) in the Reya app, the same way as the agent key. |
| `NOT_AGENT_ACCOUNT`, `NOT_PERMITTED` | Run `agent status`. |
| `ACCOUNT_HAS_ACTIVE_RUN` | `workflow status`; stop the active run first. |
| `NONCE_TOO_LOW`, `REQUEST_EXPIRED` | Nothing happened; run the same command again (check the clock for `REQUEST_EXPIRED`). |
| `MODE_NOT_ALLOWED`, `ABOVE_PILOT_LIMIT` | Use a mode and amounts `workflow info` allows. |
| `RATE_LIMITED`, `BUSY`, `NOT_READY`, `ACCOUNT_STATE_UNAVAILABLE`, `MARKET_DATA_UNAVAILABLE` | Retryable: wait `retry_after_s`, then try again. |

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
