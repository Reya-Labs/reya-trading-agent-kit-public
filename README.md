# Reya Agent Kit

Trade perpetual futures on [Reya](https://reya.xyz/) by asking your own AI assistant.

## Start here

**You need Claude Code in a terminal, or Codex.** Open Terminal and run `claude` ([install Claude Code](https://code.claude.com/docs/en/setup) if the command isn't found), or run `codex`. Regular Claude or ChatGPT chat can't run the setup. As of October 2026 the Claude desktop app (including its Code tab) declines to place live trades even when asked, so use Claude Code in a terminal or Codex for trading.

In the terminal session, paste this with your wallet address:

```text
Set up the Reya agent kit for my wallet 0x<your wallet address>. Clone Reya-Labs/reya-trading-agent-kit with the GitHub CLI, follow its README, and walk me through each step.
```

While the kit is in private testing you also need access to this repository on GitHub: accept the invite, then sign in once when your assistant asks. If you were not invited, ask your Reya contact.

### On Windows

The kit runs in Ubuntu on Windows (WSL), not in Windows itself.

1. Open PowerShell as administrator, run `wsl --install`, and restart when asked. Then open **Ubuntu** from the Start menu and choose a user name and password.
2. In the Ubuntu window, install Git and the GitHub CLI with `sudo apt update && sudo apt install -y git gh`, and [install Claude Code](https://code.claude.com/docs/en/setup) (or Codex) there too.
3. [Sign in to GitHub](#signing-in-to-github) once, in the Ubuntu window.
4. Run `claude` (or `codex`) in the Ubuntu window and paste the prompt above.

The setup page opens in your Windows browser. Keep the kit where your assistant puts it, in your Ubuntu home folder (`~/reya-trading-agent-kit`), not on a Windows drive (`/mnt/c/...`): Windows drives ignore the file permissions that keep the kit's key private to you.

### Signing in to GitHub

While the repository is private, run `gh auth login` yourself in a terminal window (on Windows, the Ubuntu window) rather than asking your assistant to: your assistant would see its one-time code only after the command finishes, and gh asks the Git question below only in a terminal. Answer **GitHub.com**, **HTTPS**, **Yes** to "Authenticate Git with your GitHub credentials?", and **Login with a web browser**. It shows a one-time code: open <https://github.com/login/device> on any device, your phone included, and enter it. If you signed in some other way before, run `gh auth setup-git` once.

### No browser on this computer

The setup page needs a browser where your wallet is. When the kit cannot open one here (an SSH session, a server, or WSL that cannot start Windows programs), your assistant shows you the link instead. Open it on any device where your wallet is, such as your phone wallet's built-in browser or another computer, and do the three steps there. The kit notices when you've finished.

### If your assistant's safety mode stops a step

Claude Code's auto mode can refuse to run the installer (it is code from an external repository), `agent init` or a live order. That is its own safety check, not a kit error. Approve the command if it asks, switch to a mode that asks before running commands, or run the command it shows you in your own terminal and tell it when it has finished.

### What happens next

Your assistant installs the kit, which takes a minute or two, and asks whether you'd like to practise with virtual money first. For real trading it opens one page in the Reya app that walks you through three steps:

1. **Approve the agent**: one signature in your wallet. This lets the kit place and cancel orders. It cannot withdraw, transfer or change permissions.
2. **Create your Agent account**: one click. The kit only ever trades the account it records. If the app already created an Agent account for this wallet (an earlier setup or a reinstall), the kit uses it. A wallet can have several Agent accounts (open another from the app's setup page or its "New Agentic Account"); with more than one, your assistant asks which one the kit trades. It never picks another account by itself, even one you named "Agent account". You can still ask for another sub-account during setup, and one an earlier kit version already trades stays in use; either way the kit warns that hosted strategies only run on the Agent account.
3. **Fund it** with the USDC you're willing to let it trade: one signature. The page shows the current smallest order and a suggested first amount; for example, when the smallest ETH order is 0.01 ETH, about $50 leaves room for a few small orders.

Your assistant waits while you do this and tells you when it's ready. You never type or paste a private key. If an assistant asks you for one, stop.

Want to try it without real money first? Say "practise with paper trading first": the kit has a paper account with virtual funds and live prices, and it needs no wallet or approval.

### Then try asking

- "Show my Agent account balance and open positions on Reya."
- "What is the ETH funding rate on Reya right now?"
- "Place a small post-only buy for ETH 5% below the market, then cancel it."
- "Show my open orders on Reya."

### What it can and cannot do

- **Can:** read Reya market data and your accounts, and place, change and cancel orders, take-profits and stop-losses on your Agent account.
- **Hosted strategies (pilot):** ask for a rule such as "buy $25 of BTC each time it drops through 90,000, up to $200, for a week", and Reya runs it on your Agent account while your computer is off. Your assistant previews it first and starts it only when you say yes.
- **Cannot:** withdraw, transfer funds, change permissions, or trade any account other than your Agent account. The approval you sign is trading-only on Reya's side, and the kit refuses every other account on top of that.
- **Its key** is generated on your computer and stored in the kit folder's `.env` file, readable only by you. It is never printed, uploaded or shared. Revoke it at any time on the app's API page (More → API).
- **What it contacts:** Reya's API, Reya's strategy service when you use hosted strategies, the Reya app pages it opens for you, and GitHub when installing or checking for a newer release. No analytics and no conversation logging.
- **Orders are real** and irreversible once submitted. Read [`DISCLAIMER.md`](DISCLAIMER.md).
- **Guards against typos:** your assistant previews every order and tells you its size in USDC, the leverage and the distance from the market price first. The kit refuses, until you confirm, a limit price more than 5% through the market price, an order larger than the account's value, or one that takes leverage above 5x. Change the defaults with `REYA_MAX_ORDER_NOTIONAL` and `REYA_MAX_LEVERAGE` in `.env`.
- **Margin before you trade:** `query margin` and `query positions` show equity including unrealized PnL, margin usage (100% is liquidation) and each position's liquidation price. Every order preview says whether Reya's matching engine would accept the order and how close it would leave you to liquidation, and the kit refuses one the engine would reject. `--max_size` and `--usage 0.5` size an order by risk instead of by guessing leverage. See [`docs/margin.md`](docs/margin.md).

### Update or remove

- Say "Update the Reya agent kit." Your assistant runs `./install.sh --release=latest` in the kit folder, and your setup is kept.
- Say "Remove the Reya agent kit." Your assistant runs `./uninstall.sh`, which unlinks the skill and keeps your `.env` and records. Revoke the agent on the API page too.

---

## For AI assistants: setting this kit up for a user

Follow these steps when a user asks you to set up or install the Reya agent kit.

1. **You must be able to run shell commands on the user's computer.** If you cannot, because you are a chat-only app, tell the user that this needs Claude Code in a terminal (the `claude` command) or Codex, and to open a new session there and paste the same prompt. Then stop. If you are not able to place trades for the user, say so before starting: setup and read-only use work, but live orders will need Claude Code in a terminal or Codex. Offer to continue with setup, or stop so the user can switch first.
2. **Check prerequisites:** macOS, Linux, or Ubuntu on Windows (WSL); `git`, and the GitHub CLI (`gh`). On Windows the kit must run inside WSL: if your shell is Windows itself (PowerShell, cmd or Git Bash), stop and point the user to [On Windows](#on-windows). Offer to install anything missing with the platform's package manager, for example `brew install gh` or `sudo apt install -y git gh`, and ask before installing. The installer needs no Python setup from you: it uses a system Python 3.12+ that can create a venv, and otherwise downloads a pinned uv into the kit folder (no admin rights, nothing outside the folder) and uses that.
3. **While the repository is private:** run `gh auth status`. If the user is not signed in, ask them to run `gh auth login` themselves in a terminal, as described in [Signing in to GitHub](#signing-in-to-github), and to tell you when it's done. Don't run it yourself: you would see its one-time code only after it finishes, and without a terminal gh skips the step that lets git use the sign-in. If the clone reports that the repository was not found, the user needs an invite: tell them to ask their Reya contact. Do not try other URLs and never ask for a token.
4. **Install the latest release and start setup** with the user's wallet address. The address is public; no key is needed. If your safety mode refuses to run the installer, `agent init` or a live order, tell the user and show them the exact command to run in their own terminal.
   ```bash
   gh repo clone Reya-Labs/reya-trading-agent-kit ~/reya-trading-agent-kit   # skip if the folder already exists
   cd ~/reya-trading-agent-kit && ./install.sh --release=latest --owner=0x<wallet>
   ```
5. **Ask once whether to practise first.** The first `agent init` result carries `practice_first`; nothing has opened in the browser yet. Ask its question in plain words. For practice, run its `practice_command` (paper trading, virtual funds, no wallet needed) and stop the live setup there until the user asks to continue.
6. **Real trading: one page, one wait.** Run `~/reya-trading-agent-kit/.venv/bin/python -m agent_kit.scripts.agent init --until-ready --timeout 100`. It opens one setup page in the Reya app (one browser tab) where the user approves the agent, creates the Agent account and funds it, and it returns once setup is `ready`. Before it starts, tell the user in plain words what `next` says and show `agent_address` so they can compare it with the page. The page only runs the steps for the wallet the user gave you: if another wallet is connected, it asks them to switch first. If they say the connected wallet is the one they meant, remove the `PERP_WALLET_ADDRESS_1` line from `.env` and run `agent init --owner <that wallet>` (the agent key is kept). If it returns `timed_out: true`, tell the user where they are (`progress`, e.g. step 2 of 3) and run it again; never ask them to report back after each step. If it returns `opened: false` with `browser_unavailable` (an SSH session, a server, or WSL without access to Windows), it returned at once without waiting: show the user `url` and ask them to open it on any device where their wallet is (a phone wallet's built-in browser works), then run the same command again, which now waits. Use `init`, not `status`: `init` records the Agent account; `status` only reports. If the setup page does not load, give the user `step_url`, the app's regular page for the current step. At the funding step, relay `funding_guidance` (the smallest order and a suggested amount); the user enters the amount on the page.
7. **Never** ask for, accept, print, copy or move a private key, and never click or sign in the app on the user's behalf.
8. **At `ready`,** read [`SKILL.md`](SKILL.md) for the operating rules (new sessions load it automatically as a skill). If the result's `hosted_runs` carries an `offer`, ask its question once, with its permission text: on "now" run `agent init --hosted-runs now --until-ready --timeout 100` (the same setup page moves on to its optional Enable hosted runs step, one more signature); on "later" run `agent init --hosted-runs later`. Then offer the `try_asking` examples from the result.

To update later: `cd ~/reya-trading-agent-kit && ./install.sh --release=latest`. When `agent status` reports `kit.update_available`, or any command's result carries `kit_update`, tell the user and offer to update. Never update without asking.

---

## For developers and operators

> **Live execution and isolated local paper trading.** Paper results are simulations, not production acceptance or historical backtests. Read the [simulation contract](docs/paper-trading.md). Validate on devnet1 before pointing at mainnet. Devnet1 and Cronos are distinct deployments despite sharing a chain ID. See [`DISCLAIMER.md`](DISCLAIMER.md).

### What this is

Python commands wrapping the [Reya SDK](https://github.com/Reya-Labs/reya-python-sdk), each emitting JSON to stdout:

- [`agent_kit/scripts/doctor.py`](agent_kit/scripts/doctor.py) — offline configuration and skill-discovery diagnostics
- [`agent_kit/scripts/data.py`](agent_kit/scripts/data.py) — exact V2 market data, bounded history and account reconciliation
- [`agent_kit/scripts/stream.py`](agent_kit/scripts/stream.py) — resilient V2 observations as NDJSON
- [`agent_kit/scripts/watch.py`](agent_kit/scripts/watch.py) — [wait on the WebSocket for a price or funding level](docs/watch.md), then run at most two agreed commands once
- [`agent_kit/scripts/paper.py`](agent_kit/scripts/paper.py) — isolated durable virtual account using public live V2 data
- [`agent_kit/scripts/history.py`](agent_kit/scripts/history.py) — [account-period activity and performance](docs/history-and-performance.md) with coverage and cashflow separation
- [`agent_kit/scripts/health.py`](agent_kit/scripts/health.py) — connectivity check
- [`agent_kit/scripts/query.py`](agent_kit/scripts/query.py) — read commands (prices, positions, orders, balances, margin)
- [`agent_kit/scripts/trade.py`](agent_kit/scripts/trade.py) — write commands, including GTC limit modification and cancellation by client ID
- [`agent_kit/scripts/account.py`](agent_kit/scripts/account.py) — previewable API-relayed account creation and signed rUSD transfers
- [`agent_kit/scripts/runner.py`](agent_kit/scripts/runner.py) — a persistent session with submission budgets, durable nonces and a cancellation heartbeat

The full agent-facing manifest — every command, argument, and response shape — is in [`SKILL.md`](SKILL.md) at the repo root, an [agentskills.io](https://agentskills.io)-format skill. Running [`./install.sh`](install.sh) sets everything up and links the kit into `~/.claude/skills/` (Claude Code), `~/.agents/skills/` (other agentskills.io runtimes) and, when Codex is installed, `~/.codex/skills/`; the scripts then work from any directory via `<kit-dir>/.venv/bin/python -m agent_kit.scripts.<script>`.

The kit depends on `reya-python-sdk`, pinned as a git dependency in [`pyproject.toml`](pyproject.toml). Every `from sdk...` import resolves against that package — there is no vendored copy to keep in sync.

### Install from source

Requires **Python 3.12+** (when no system Python can create a venv, the installer uses a pinned uv kept in `.uv/` inside the kit) and, while the repository is private, GitHub access through the [GitHub CLI](https://cli.github.com/) (`gh auth login`) or your normal Git credentials. The installer creates a `.venv` inside the kit, installs the pinned dependencies from [`requirements.lock`](requirements.lock), links the kit into your agent's skills directories, and runs a health check. See [installation, update, uninstall and client setup](docs/private-first-use.md).

```bash
gh repo clone Reya-Labs/reya-trading-agent-kit
cd reya-trading-agent-kit
./install.sh --release=latest   # switch to the latest release first; omit to install the current checkout (e.g. main)
```

`--owner=0x<wallet>` continues straight into the guided setup. `--agent=claude-code|codex|agentskills|both|all` chooses which skills directories to link (default: Claude Code and agentskills, plus Codex if installed), `--no-link` skips linking and `--skip-health` skips the connectivity check. Try the [credential-free paper demo](docs/paper-trading.md#first-paper-session). Run `doctor` for offline, secret-free configuration checks; `REYA_MODE=read-only` disables live writes.

Prefer a package manager? `poetry install` (Poetry 2.x) or `uv pip install -e .` work too — [`poetry.toml`](poetry.toml) pins the venv to `.venv` in-project, so the invocation path is identical regardless of install method. (Poetry 1.x cannot parse the PEP-621 `[project]` metadata and fails with `'name' is a required property` — upgrade if you see that.)

Illustrative mainnet response (five perp definitions were returned on 2026-10-06; counts change by deployment and time):

```json
{
  "status": "ok",
  "api_url": "https://api.reya.xyz/v2",
  "sdk_version": "3.6.3.0",
  "markets_available": 5,
  "latency_ms": 209
}
```

`markets_available` counts perp market definitions (the example is illustrative); `sdk_version` mirrors the tag pinned in [`pyproject.toml`](pyproject.toml).

### Environment

Public reads (`health`, `prices`, `market info`, `market funding`) need no wallet. Account reads require the owner address and account ID (`accounts` only needs the owner). Writes sign with the **agent key**: a key the kit generates for itself and that you approve once in the Reya app. **You never hand the kit your wallet's private key.** Mainnet is the default and needs no `.env` beforehand — the guided setup writes it (copy [`.env.example`](.env.example) to `.env` only to switch to devnet1 or another deployment):

```bash
.venv/bin/python -m agent_kit.scripts.agent init --owner 0x<your wallet address>
```

`agent init` is idempotent: it reads the current state and tells you the one thing to do next, then you re-run it (or pass `--wait` to let it poll). The steps, in order:

1. **Onboarding check.** A wallet with no Reya accounts is sent to the app to connect and accept the terms first (the app creates your main account; the kit never creates a wallet's first account).
2. **Approve the agent.** The kit generates a keypair, writes the private key to `.env` (file mode 0600; it is never printed) and prints the agent address plus a link to the app's API page (`/api-page`, header → More → API) with that address pre-filled. Compare the address, approve it with one wallet signature. The permission is the same one one-click trading uses: it lets the agent place and cancel orders on your accounts and nothing else — no withdrawals, no transfers, no permission changes. Revoke it on the same page at any time.
3. **Create the agent account.** A link to create a dedicated sub-account named "Agent account" (one click, no signature today). Reya's API reports it with the account type `AGENTPERP`, and the kit finds it by that type alone: renaming it changes nothing, and a sub-account of another type is never taken for it, whatever its name. The kit records its ID in `PERP_AGENT_ACCOUNT_IDS` and sets `PERP_ACCOUNT_ID_1` to it. To switch the kit to another of your Agent accounts later, run `agent init --account-id <id>`: it rewrites both lines itself and keeps the agent key.
4. **Fund it.** A link to the transfer flow from your main account, with `--amount` pre-filled if given; one wallet signature.
5. **Ready.** `agent status` shows owner, agent address, approval, agent account and balance.
6. **Optional: enable hosted runs.** Where Reya runs a strategy service, `ready` offers one more approval on the same setup page: Reya's hosted runner, which places a strategy's orders while your computer is off. Answer with `--hosted-runs now` (the kit waits on the page's Enable hosted runs step) or `--hosted-runs later` (remembered; not asked again). The approval lets the runner place and cancel orders on every account your wallet owns, not only the Agent account (Reya's service only trades the Agent account); it cannot withdraw, transfer or change permissions, and you can remove it any time on the API page.

The agent key only ever trades accounts recorded in `PERP_AGENT_ACCOUNT_IDS`; the kit refuses anything else because the on-chain permission is wallet-wide. Live/read commands load the same `.env` even when invoked from another directory; shell variables take precedence. The app links default to `https://app.reya.xyz` on mainnet; `https://app-devnet-cf.reya.xyz` on devnet1; set `REYA_APP_URL` for other deployments.

**Bringing your own key instead.** If you deliberately want the kit to hold a wallet key — for example a fresh wallet created only for it, so transfers and withdrawals need no trip to the app — set `REYA_SIGNER=own-key` and exactly one of `PERP_PRIVATE_KEY_FILE` (an absolute path to a regular, non-symlink file owned by you with mode `0600`/`0400`, containing only the hexadecimal key) or `PERP_PRIVATE_KEY_1`. Without `REYA_SIGNER=own-key` those variables are an error, so an old `.env` fails loudly instead of silently signing with a wallet key. Agents must never print or inspect key material in either mode. Unsigned account reads do not open key files or validate signing secrets.

Devnet1 currently uses exchange ID `1`; confirm the exchange for your deployment. Non-mainnet account commands require an explicit `REYA_DEX_ID`. Mainnet defaults to `2`. Known API URLs select their own gateway and reject mismatched chain IDs or gateway overrides. Custom endpoints require explicit chain, gateway, and exchange settings. With no environment configured, public reads default to mainnet.

Account-specific commands scope to `PERP_ACCOUNT_ID_1`. Other sub-accounts on the same wallet are not included. **Never commit private keys.** `.env` is gitignored.

---

### Production trading

Mainnet is supported by the same commands. Start with [`.env.mainnet.example`](.env.mainnet.example) in a separate checkout and follow the [production guide](docs/mainnet.md). Account inspection works without a private key, and reads/writes reject an account ID that does not belong to the configured owner. The signer may be the owner or an authorized delegate.

Production configuration and signing are tested, and public mainnet reads have been verified. A small mainnet canary (order placement, cancellation, TP/SL arming and a reduce-only close) passed on the production-support revision. Trigger firing on mainnet was not exercised.

---

### Operating boundaries

Read the [V2 endpoint, freshness, accounting and recovery contract](docs/v2-data-and-reconciliation.md).
New data access is V2-only. The two retained account-setup legacy routes run under
a documented exception (12 October 2026) until dedicated V2
endpoints ship. Derived account views are not an authoritative matching-engine
admission/risk response.


Cleanup commands require exactly one of `--symbol X` or `--all` on every deployment, including previews. A symbol filter still affects **all strategies using that symbol on the account**. Preview, inspect, then explicitly authorize the write. See [isolation and nonce guidance](docs/mainnet.md#account-isolation-and-concurrent-writers).

#### Known limitations

- **History retention.** The V2 history endpoints stop serving older rows and do not say where served history starts. On mainnet, one account's order history returned nothing before 28 September 2026 although its fills went back to June, and its earliest fee transfers were missing. `history` marks a feed `incomplete` only when other feeds prove rows are missing (`orders_missing_for_fills`, `fills_without_fee_transfers`); `bounded` never means complete. Prefer recent intervals and treat older totals as observed records only.
- **Lost-reply account operations.** If the relay's reply is lost, `account status` confirms a create or transfer only from a unique matching observation after the request, labelled `unique_observation_after_request`. An identical operation by another client after the request cannot be told apart, and ambiguous cases stay unresolved. See [confirmation by unique observation](docs/account-setup-and-runner.md#confirmation-by-unique-observation).
- **Positions off the quantity step.** ADL rescaling can leave a position that is not a multiple of the market's `qty_step_size`. The API rejects off-step quantities on every order, reduce-only included, so `close_all` cannot close it exactly; a reduce-only `order market` for the position's quantity (rounded down to the step) leaves less than one step open. A position below `min_order_qty` that is on the step closes normally: reduce-only orders are exempt from the minimum.

Order responses include client/fill IDs and a bounded, best-effort settled-fee lookup. Fees remain `null` until the reported fills reconcile; `query executions --order_id ID` reads them later without resubmitting the order. See the [execution reference](docs/command-reference.md#settled-executions-and-fees) for fee units and pagination limits.

### Account setup and a trading runner

Follow [the setup and runner guide](docs/account-setup-and-runner.md) to create/fund a dedicated account and feed commands to one persistent trading session. Both flows preview by default. Creation is unsigned; transfers use a separate owner key to sign an authorization. Reya's executor submits the transaction and pays gas. Reuse a stable `--request-id` for each setup intent; uncertain outcomes block further submission. The runner requires a flat, empty account and explicit acknowledgement of account-wide cancellation.

All kit writers now cooperate through host-local locks and durable nonce state. This coordinates updated kit processes sharing a state directory; existing bots and other hosts must be stopped or integrated separately. The runner enforces allowed markets, order/session submission budgets and slippage limits, on top of the kit's fat-finger guards. Neither the runner's exit nor the venue's cancellation timer closes positions, and neither removes TP/SL: the timer keeps them when it fires, and on exit the runner cancels its other orders one by one while a position is open, because an account-wide cancel would clear its TP/SL too. Uncertain writes require reconciliation, with no automatic replay.

### Common operations

Commands below assume the kit's venv is active — run `source .venv/bin/activate` first. Otherwise prefix each command with `.venv/bin/` (or invoke the venv interpreter directly: `.venv/bin/python -m agent_kit.scripts.query prices`).

```bash
# Reads
python -m agent_kit.scripts.query prices
python -m agent_kit.scripts.query accounts      # owner only; discover account IDs
python -m agent_kit.scripts.query fees          # fee tiers; your tier with an owner set
python -m agent_kit.scripts.query positions     # includes estimated uPnL at the mark
python -m agent_kit.scripts.query orders
python -m agent_kit.scripts.query executions --symbol eth   # bare tickers expand to ETHRUSDPERP
python -m agent_kit.scripts.query balances
python -m agent_kit.scripts.query margin       # equity, margin usage, liquidation prices
python -m agent_kit.scripts.query market info    --symbol ETHRUSDPERP
python -m agent_kit.scripts.query market funding --symbol ETHRUSDPERP

# Orders
# Maker-only GTC with a client ID (nonzero uint64):
python -m agent_kit.scripts.trade order limit ETHRUSDPERP --side long --price 1900 --qty 0.01 --post_only --client_order_id 123
# Entry with its stop-loss and take-profit, armed once the fill shows in the position (preview first)
python -m agent_kit.scripts.trade order market SPYRUSDPERP --side long --qty 1.55 --sl 0.35% --tp 0.5% --preview
python -m agent_kit.scripts.trade order market SPYRUSDPERP --side long --qty 1.55 --sl 0.35% --tp 0.5%
# Without --sl/--tp the result's `protection` says whether any stop-loss on Reya covers the position
python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --notional 100
python -m agent_kit.scripts.trade order limit  ETHRUSDPERP --side long --price 2000 --notional 100
# Size by margin risk: the largest size the engine admits, or margin usage 50% after the fill
python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --max_size --preview
python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --usage 0.5 --preview
python -m agent_kit.scripts.trade order cancel --order_id <id>

# Trigger orders (close the whole position when fired)
python -m agent_kit.scripts.trade order tp ETHRUSDPERP --side long --trigger_px 2300 --limit_px 2295 --time_in_force IOC
python -m agent_kit.scripts.trade order sl ETHRUSDPERP --side long --trigger_px 2000 --limit_px 1995 --time_in_force IOC

# Risk primitives — preview first, then execute
python -m agent_kit.scripts.trade order cancel_all --all --preview
python -m agent_kit.scripts.trade order cancel_all --all
python -m agent_kit.scripts.trade positions close_all --all --cancel_orders --preview
python -m agent_kit.scripts.trade positions close_all --all --cancel_orders

# Hosted strategies: Reya runs the rules on the Agent account (preview first; Paper, the default, sends no orders)
python -m agent_kit.scripts.strategy draft --trigger price:eth:up:3000 --order eth:sell:1000:3300 --order btc:buy:500:80000 --allocation 2000 --expires 7d
python -m agent_kit.scripts.strategy preview --file <file from draft>
python -m agent_kit.scripts.strategy start --file <file from draft> --label "ETH breakout"
python -m agent_kit.scripts.strategy status
python -m agent_kit.scripts.strategy stop <run_id>

# Only for what Reya can't run (a funding trigger, an entry when a hosted strategy can't be used): wait on the WebSocket, on this computer
python -m agent_kit.scripts.watch trigger --symbol eth --mark lte:2492 --then "order market eth --side short --notional 30 --sl 2510" --detach
python -m agent_kit.scripts.watch list
python -m agent_kit.scripts.watch stop <id>
```

Hosted strategies (a private pilot) run on Reya's strategy service, not on this computer: "when the ETH mark crosses up through 3,000, sell $1,000 of ETH with a stop at 3,300 and buy $500 of BTC, up to $2,000, for a week". A strategy has a trigger (a price crossing, a schedule or the start), immediate and resting orders on one or more markets, and optionally conditions, a stop per market, exits and a repeat. `draft` fills the service's template from flags and keeps the document the service expands it to; `validate` and `preview` ask the service, which is the only evaluator; `start` and `stop` are signed with the agent key and refused for any account but the Agent account. An Agent account runs a few strategies at once, one per market, sharing its margin. Status shows the strategy's state, a row per market (its own share, entry, profit and stop), its resting orders, each step's orders ("Sent 3 orders: 2 filled, SOL rejected for margin"), named skip reasons and the remaining allocation. `start`, `status` and `stop` also link the strategy's page in the Reya app (`app_run_url`). By default a stop cancels its resting orders and closes what it opened in each market (its own fills, never more than the live position, never what you held before or traded by hand), then cancels that market's stop; `stop --keep-position` keeps the positions open with no stop order from the strategy, and `stop --all` stops every live one. See [hosted strategies](docs/hosted-workflows.md). For what a strategy cannot express, with your own program, see [from strategies to the API](docs/from-workflows-to-the-api.md).

`watch trigger` is the local fallback for rules Reya can't run, and for a single-market entry at a price when a hosted strategy can't be used (for example no hosted runs approval, or a take-profit wanted at entry): it waits on the market summary WebSocket (every push; never REST polling) for a mark or funding level, then runs at most two agreed `trade` commands once, in order, or only reports. It refuses entries without a stop-loss and positions without one on Reya, never fires on a move it saw only through a stale feed, and runs only while this computer is awake. See [watching a price or funding](docs/watch.md).

`--sl` / `--tp` take a percent of the entry's average fill price or a price. After the fill the command waits until position reads include it (at most 15 s on mainnet, 30 s on devnet), arms the stop-loss, then the take-profit, reads both back and reports `protection`; if it cannot arm the stop-loss it exits 1 and says the position is UNPROTECTED, with the command to arm it. It never resends the entry. Standalone `order tp` / `order sl` read the position until it shows the `--side` position, within the same ceiling, so they also work straight after an entry. See [entry protection](docs/command-reference.md#entry-protection).

The current API uses `TAKE_PROFIT` / `STOP_LOSS`. Triggers require a chosen child limit price and time in force; an IOC may partially fill or fill nothing. A returned order ID alone is not a fill. The CLI retains fill/cancellation details and exits 1 for incomplete IOC execution, unknown outcomes, or failed batches. `close_all` only counts a position as closed after a flat readback; `--cancel_orders` cancels resting orders in the same scope first. Every create carries a client order ID (generated when omitted) that is returned even on errors.

See [`SKILL.md`](SKILL.md) for the operating rules, the [command reference](docs/command-reference.md) for response shapes, and [error codes](docs/error-codes.md) for what each error means and whether a retry is safe.

---

### Example strategies

End-to-end strategies live in [`examples/agent_kit/`](examples/agent_kit/) and are written the way an AI agent would actually use the kit — by shelling out to the CLI scripts and parsing JSON output, not by importing the SDK.

- [`btc_eth_strategy.py`](examples/agent_kit/btc_eth_strategy.py) — long $100 ETH if BTC > $70k, else short, with its stop-loss and take-profit on Reya.
- [`eth_limit_order.py`](examples/agent_kit/eth_limit_order.py) — GTC limit buy $100 ETH at $50 below the mark price.
- [`bracketed_long.py`](examples/agent_kit/bracketed_long.py) — defensive bracketed long: preview, then one `order market --sl --tp`; if the entry filled but no stop-loss is armed, flatten with `positions close_all`, and say loudly when even that cannot be confirmed.

Run them as modules: `python -m examples.agent_kit.bracketed_long`.

---

### Testing

Offline tests live in [`agent_kit/tests/`](agent_kit/tests/). Fixtures use the installed SDK models and method signatures; signing-boundary tests exercise the real SDK payload builders. Network access and local credentials are disabled in the suite.

```bash
make test
# or: poetry run pytest agent_kit/tests -v
```

Live validation is separate and opt-in: follow [`docs/devnet-smoke.md`](docs/devnet-smoke.md) on an isolated devnet account. CI never trades.

---

### Development

Development tooling uses **Poetry 2.x** (`poetry install` pulls the dev group: pytest, black, isort, flake8, mypy).

```bash
make test      # run the unit suite
make lint      # isort + black + flake8 + mypy (check only)
make format    # apply isort + black
make lock      # re-lock after a dependency change (poetry.lock + requirements.lock)
```

The lock/export workflow requires `poetry-plugin-export` (CI uses Poetry 2.4.1 and plugin 1.8.0).

To upgrade the SDK: edit the git tag in [`pyproject.toml`](pyproject.toml), then `make lock` — CI validates that both lockfiles stay in sync with `pyproject.toml`.

External PRs are welcome — run `make lint && make test` before opening one, and keep the JSON output contract in [`SKILL.md`](SKILL.md) in sync with any script change.

---

### Safety

Read [`DISCLAIMER.md`](DISCLAIMER.md) before pointing this at mainnet. Live orders, cancellations, and position changes on Reya are irreversible once submitted.