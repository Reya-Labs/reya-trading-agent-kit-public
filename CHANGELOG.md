# Changelog

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
