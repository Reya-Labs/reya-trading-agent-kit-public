# Watching a price or funding

`python -m agent_kit.scripts.watch` waits on Reya's WebSocket for a mark or funding level, then runs
at most two agreed kit `trade` commands once, or only reports that the level was reached.

It is for what Reya cannot run itself: a funding trigger, or an exit on one market's price that
closes positions a strategy did not open. Everything else belongs on Reya: a stop-loss or
take-profit (`--sl`/`--tp`, `order sl`/`order tp`), a resting limit, or a
[hosted strategy](hosted-workflows.md), which also trades several markets on one trigger and
closes them together with an exit. Those keep working when this computer is off, and this
does not.

It is also the fallback for an entry that waits for a price when a hosted strategy cannot be
used: hosted runs are not enabled and the user does not want to approve the hosted runner, the
account is not the Agent account, the account already runs as many strategies as it may or
another of its strategies holds that market, `production` is not offered, the service refuses
the market, or the user wants a take-profit order resting on the venue from the fill (a
strategy's target exit is acted on by the service). Then one command does it:
`--then "order market <symbol> --side S --qty N --sl L [--tp L]"`, with the stop-loss (required)
and take-profit armed as soon as the fill shows. When a strategy can be used, it comes first.

## Event-driven, not polling

It subscribes to the public market summary channel, `/v2/perpMarket/{symbol}/summary`. The server
pushes it about every 500 ms with the mark (`markPrice`), the hourly funding rate (`fundingRate`, in
per cent per hour) and the time of the last price update (`pricesUpdatedAt`). Reya computes the mark
once a second and funding every 5 s, so the watch sees every mark that Reya's own stop-losses and
hosted strategies act on. It never reads prices over REST in a loop. To observe a market without a
rule, use `python -m agent_kit.scripts.stream market --feed summary --symbol X`, which reads the same
channel.

## Commands

```bash
python -m agent_kit.scripts.watch trigger --symbol S [--mark lte|gte:PRICE] [--funding OP:PCT_PER_HOUR]
    [--funding-window MIN] [--then "<trade command>"]... [--unprotected-ok] [--max-hours H] [--detach]
# at least one of --mark and --funding
python -m agent_kit.scripts.watch list
python -m agent_kit.scripts.watch status ID
python -m agent_kit.scripts.watch stop ID
```

| Argument | Meaning |
| -- | -- |
| `--symbol` | The market whose summary it watches; a bare ticker works (`eth`). |
| `--mark lte:PRICE` / `gte:PRICE` | Fires when the mark crosses down to PRICE or below, or up to PRICE or above. |
| `--funding OP:PCT_PER_HOUR` | `lt`, `lte`, `gt` or `gte` on the average funding over `--funding-window` minutes (1..60, default 15), in per cent per hour, the unit `strategy draft --funding` uses. With `--mark` it is a condition at the crossing; alone it is the trigger. |
| `--then "…"` | A kit `trade` command to run once it fires, at most twice; they run in order. Without any, it only reports. |
| `--unprotected-ok` | Lifts the two stop-loss rules below. Only after the user agreed to that in the conversation. |
| `--max-hours` | Ends the watch (`expired`); above 0, at most 24, default 4. |
| `--detach` | Runs it in the background (below). |

## Before it watches

It refuses (`refused`, with `reason`), and watches nothing, when:

- a `--then` is not `order market`, `order limit` or `positions close_all --symbol X`
  (`invalid_action`), uses `--force`, `--allow_large`, `--skip_margin_check`, `--preview` or `--all`,
  gives an option twice, or there are more than two (`too_many_actions`);
- an `order market` opens or adds to a position without `--sl`, or an `order limit` would rest an
  entry, which carries no stop (`unprotected_entry`);
- a market a command touches holds a position with no stop-loss on Reya (`no_backstop`), or its
  orders cannot be read (`backstop_unknown`): that stop is the protection that stays if the watch
  dies;
- a command's own `--preview` would not send it now (`preview_refused`, with the `preview`);
- the arguments are out of bounds (`invalid_arguments`);
- the deployment has no verified WebSocket (`no_websocket`);
- with `--detach`, a running watch already waits for the same rule and commands (`duplicate`), or
  an unexpired one is `unknown` (`liveness_unknown`, below).

An `armed` event then lists the stops it found (`backstops`) and each command's preview.

## How it decides

- **Fresh.** A push is fresh while pushes keep coming (no gap over 3 s, by this computer's clock
  and by `pricesUpdatedAt`), arrive at live pace and on time, and the prices keep updating
  (`pricesUpdatedAt` moved in the last 10 s, the venue's own mark-age limit). Pace and timing use the
  row's `updatedAt`, which the server stamps on every push. *On time*: receipt minus `updatedAt` at
  most 3 s above the quickest delivery over the last 60 live-paced pushes (about 30 s). The window
  is counted, not timed, and only pushes that arrive at live pace (not bunched with the one before)
  join it, so frames the socket held through a pause or sleep, however many, are measured against
  deliveries from before it and never displace them; a steady delay from the start counts as
  normal; a constant offset between the two clocks cancels out,
  and a clock step heals within those 60 pushes. *Live pace*, until 3 pushes in a row have arrived
  at live pace: frames bunched together while, over the last 4 pushes, receipts advanced less than
  half as much as the stamps are a burst (frames held from the start, which cannot set the
  baseline). After that, a short network stall that bunches a few frames within 3 s is not stale. A gap, late or burst frames, or frozen prices give `stale`; the next
  fresh push gives `recovered`. Pushes carry `late_ms`, how much later than that quickest delivery
  they arrived (not on `stale` with reason `no_push`, which has no push).
- **Start.** Nothing fires until three pushes (about 1 s) can be judged. A crossing in that second
  is compared with the first mark once they can, so a move that stays past the level still fires; a
  touch that reverses within that second is missed.
- **Crossing.** It fires on the first fresh mark at or past the level after a fresh mark on the
  other side. That is the hosted strategies' rule, so a level means the same here as on Reya.
- **Never a guess.** A level already reached at the first push ends the watch (`halted`,
  `already_triggered`). A move through the level that happened while the feed was stale, including
  while this computer slept, ends it too (`halted`, `crossed_while_stale`). No fresh push for 60 s,
  or no first push within 60 s of the start, ends it (`halted`, `feed_stale`). A market the server
  refuses to stream twice before any price arrived (an unknown or delisted symbol) ends it
  (`halted`, `subscription_refused`); a single refusal, or one after prices have flowed, is treated
  as a dropped socket. None of these sends anything.
- **Funding.** The average of the pushed funding over the window, counted only across contiguous
  fresh pushes: a stale gap restarts the window. With `--mark`, a crossing before the window is
  full, or with the condition not met, is `skipped`, and the watch waits for the next crossing.
  Alone, it fires when the full-window average moves from not meeting the condition to meeting it;
  met already when the window first fills is `already_triggered`.
- **Reconnects.** A dropped socket reconnects with backoff (0.5 s doubling to 8 s), each attempt
  reported as `reconnecting`. The freshness rules above decide what a reconnect missed, and a push
  older than one already seen (a resubscribe snapshot) is ignored.
- **Stop and expiry win.** A stop or the deadline that arrives while it waits for a push wins over
  that push.

## When it fires

It runs the `--then` commands once, in order, in this process, through the same code and guards as
`python -m agent_kit.scripts.trade` (fat-finger and margin checks, the writer lease and durable
nonces). It stops at the first command with an `error` and sends nothing after it. It never retries
and never repeats a command. Two commands are not atomic: the first can fill while the second fails
or is never sent (`legs_atomic: false`). The second waits for the first to finish, including arming
its stop-loss and take-profit (up to 15 s on mainnet and 30 s on devnet while the position shows the
fill) and its fee read. `watch stop` or SIGTERM during firing waits until every command has its
outcome: `watch stop` then answers `stopped: false` with a message, and `watch status` shows the end.

## Events and exit codes

One JSON object per line, each with `event`, `symbol`, `at_ms` and, for a detached watch, `id`:

| `event` | Meaning |
| -- | -- |
| `arming` | `rule`, `then`, `alert_only`, `expires_at_ms`. |
| `armed` | `backstops` and `previews` (only with `--then`). |
| `watching` | The first fresh mark (`mark`, `prices_updated_at`, `level`); with only `--funding`, again with `reason` `funding_window_full`. |
| `stale` / `recovered` | The feed went stale (`reason` `push_gap`, `no_push`, `late_delivery` or `prices_not_updating`) / is fresh again; `late_ms` as above. |
| `skipped` | A crossing whose funding condition was not met or whose window was not full. |
| `reconnecting` | `error` and the wait `in_s`. |
| `fired` | `trigger` (the mark, level and funding average) and `actions` (each `command` with its own `result`); `error` if a command failed; `legs_atomic: false` with two. |
| `expired` / `stopped` | `--max-hours` passed / `watch stop`, SIGTERM or Ctrl-C. |
| `halted` | `reason` `already_triggered`, `crossed_while_stale`, `feed_stale`, `subscription_refused` or `feed_error`, and `error`. |
| `refused` | Before watching; `reason` and `error`. |

It exits 0 for `fired` with every command done, `expired` and `stopped`, and 1 otherwise.

## In the background

`--detach` starts the watch in its own process group, writes its events to
`$REYA_STATE_DIR/watches/<id>.jsonl` (`REYA_STATE_DIR` from the environment or the kit's `.env`, as
for every command) and a record next to it (`<id>.json`: process, rule, commands,
expiry and output file; owner-only files), and returns once the watch reports (`watching`) or ends,
at most 20 s: `{"event": "detached", "id", "state", "last_event", "output", "pid", ...}`. It exits 1
when the watch already ended `refused` or `halted`, `fired` with an `error`, or `died` in that time.

- `watch list`: every recorded watch with `state`: `running`, its final event, `died` (the process
  is gone without a final event: killed, or the computer restarted; read the account before
  anything else), or `unknown` (this computer cannot tell whether the process runs: it has neither
  `/proc` nor `ps`). While an unexpired watch is `unknown`, `--detach` refuses another
  (`liveness_unknown`), so two cannot both fire.
- `watch status ID`: the same for one, with the number of events and the last five.
- `watch stop ID`: sends SIGTERM, but only to a live process that is this watch (its command line
  carries `--id ID`), and waits up to 10 s; `stopped` says whether it has ended.

## Limits

- It runs only while this computer is awake, online and the process alive. A sleep or outage over
  60 s ends it (`halted`, `feed_stale`), whatever the price did; a shorter one resumes, and a move
  through the level during it is `crossed_while_stale`, never fired. Either way, read the account
  before starting it again.
- Reaction time after the mark changes. Measured on devnet1 (served from Tokyo) from the UK, 23 Oct:
  0.47 s from the price update to the watch firing; 3.42 s from the price update to the venue
  receiving the order, because `order market --sl` makes its account and market reads before it
  sends, each crossing that distance. Close to the API it is shorter: about 1 s is expected from
  Europe for mainnet (served from Frankfurt), not yet measured through a watch. A hosted strategy
  sends about 30 ms after the mark, and a stop-loss on Reya fires on the mark itself.
- It does not tell the user anything by itself; the assistant reads its result.
- On Windows the kit runs inside WSL, where it behaves as on Linux.
