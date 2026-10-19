# Private first use

The repository stays private during the pilot and becomes public at the public
release. An invited tester needs GitHub access to
`Reya-Labs/reya-trading-agent-kit`. Authentication errors require access from a
maintainer; never paste a GitHub token into a URL, prompt or command transcript.

## Install the latest release

The simplest path is the one prompt at the top of the [README](../README.md#start-here),
pasted into Claude Code or Codex: the assistant clones the repository, installs
the latest release and starts the guided setup. By hand, with GitHub CLI
authentication or your normal private Git credentials:

```bash
gh repo clone Reya-Labs/reya-trading-agent-kit
cd reya-trading-agent-kit
./install.sh --release=latest --owner=0x<your wallet address>
.venv/bin/python -m agent_kit.scripts.doctor
.venv/bin/python -m agent_kit.scripts.data market summary --symbol ETHRUSDPERP
```

`--release=latest` switches the checkout to the newest published release (the one
GitHub marks Latest, or the highest `vX.Y.Z` tag) before installing; it refuses
when tracked files have local changes and never touches `.env` or `.venv`.
`--release=v0.3.0` installs a specific release. `--owner` continues into
`agent init`; leave it out to install only.

If the clone reports `Repository not found` although you have access, Git is
probably using SSH with a key that is not authorized for the organization: check
`gh auth status` (Git operations protocol) and `git config --global --get-regexp '^url\.'`
for an `insteadOf` rewrite of `https://github.com/`. Clone over HTTPS with your
GitHub CLI login instead, without changing global configuration. The username
prefix in the URL keeps a global rewrite from applying; use the same `-c`
options for later fetches:

```bash
GH_USER=your-github-username   # replace with your GitHub username
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
  clone "https://${GH_USER}@github.com/Reya-Labs/reya-trading-agent-kit.git"
```

By default the installer links Claude Code (`~/.claude/skills`) and the generic
`~/.agents/skills`, plus Codex (`${CODEX_HOME:-~/.codex}/skills`) when Codex is
installed. Choose `--agent=claude-code`, `--agent=agentskills` or `--agent=codex`
for one location, `--agent=both` for the first two, `--agent=all` for all three,
or `--no-link` for direct CLI use. For any other assistant, give it the absolute
path to `SKILL.md` and let it run the commands it describes.
The installer refuses to replace another checkout's skill link. Use that
checkout's `uninstall.sh` before switching the link. Restart/reload the assistant
according to its own skill-discovery controls, then ask:

> Use the Reya skill to show the selected environment and ETH market data. Do not trade.

The assistant should run `doctor`, then the credential-free `data market` command
using the absolute `<kit>/.venv/bin/python` path. If discovery fails, provide the
absolute path to `SKILL.md`; installation is not proof a particular client loaded
it. `doctor` reports link targets, not an authenticated client acceptance result.

## First run: approve an agent, never paste a key

Trading needs a signer. The kit's default is an **agent key** it generates for
itself; the owner approves it once in the app, and it can only place and cancel
orders. Nobody types a private key anywhere:

```bash
.venv/bin/python -m agent_kit.scripts.agent init --owner 0x<your wallet address>
```

Follow the `next` instruction, do the step in the app, and re-run (or add
`--wait`). The sequence is: app onboarding if the wallet has no Reya accounts →
approve the agent address on the API page (one signature) → create the "Agent
account" (one click) → fund it (one signature) → `ready`. `agent status` shows
the same state at any time. If the assistant ever asks you for a private key,
stop: that is not how this kit is set up.

Supported baseline: POSIX macOS/Linux and Python 3.12+. CI exercises Python
3.12/3.13 and the pip installer on Linux; local checks are recorded in the PR.
Native Windows is not supported (file locks and private-file permissions are
POSIX); use WSL with Ubuntu, as in the README's [On Windows](../README.md#on-windows),
with the kit in the Ubuntu home folder rather than on a Windows drive. Claude Code (walkthrough
verified), Codex and the generic agentskills directory are supported discovery
paths. Actual moderator/client
walkthroughs remain a private-pilot acceptance step, not an automated claim.

### Regional availability

Reya's API is not available in some regions. From there, commands that contact
Reya's API fail with `error_code: "REGION_UNAVAILABLE"` (HTTP 403) and nothing
reaches the venue; local commands such as `doctor` and paper replays still work. This is
a compliance block, not a kit fault: do not route around it with a VPN or proxy.

## Choose environment and account

With no `.env`, public reads use mainnet and require no credentials. `doctor`
always reports the resolved URL and whether it is mainnet. Copy `.env.example`
for devnet1 or `.env.mainnet.example` for mainnet. Shell settings take precedence
and the file is loaded from this checkout regardless of the current directory.

- `REYA_MODE=read-only`: public/account reads only; disables trading credential
  loading and account execution.
- `REYA_MODE=devnet`: requires a non-mainnet deployment; use devnet1 settings.
- `REYA_MODE=live`: requires the mainnet API/chain configuration; real funds.
- `REYA_MODE=paper`: blocks live/read commands and directs you to the isolated
  `python -m agent_kit.scripts.paper` command. That command ignores live credentials
  and uses the deployment saved at paper initialization; no paper-to-live fallback.

Existing configurations without REYA_MODE still infer the deployment for
compatibility. Set it explicitly for the private pilot. `doctor` never reads or
prints key contents; it diagnoses source selection, not credential validity.

Set the wallet owner, run `data accounts`, and then select `PERP_ACCOUNT_ID_1`.
Prefer a small dedicated sub-account for experimentation. Existing/main-account
selection remains available. A selected sub-account is a command scope, **not a
restriction on a wallet-wide key**. The operator supplies any credentials in a
private file outside the checkout; do not put secrets in assistant conversations.

Account creation and transfers use two isolated legacy routes, kept under the
non-V2 exception agreed on 12 October 2026. Read the [API inventory](v2-data-and-reconciliation.md)
before running setup.

## Update and uninstall

`agent status` reports the installed version and the latest release under `kit`
(a best-effort `git ls-remote` against this checkout's origin, at most a few
seconds, no prompts; `REYA_KIT_UPDATE_CHECK=0` turns it off). To update, stop
running kit processes, preserve the shared operation journal, read the release
notes, then run `./install.sh --release=latest`. Do not use a blind
`git reset --hard`: retain local configuration and investigate local changes.
Run `doctor`, `health` and the same public read again. Do not migrate state by
starting a fresh state directory while operations are unresolved.

`./uninstall.sh` removes only skill symlinks pointing at this checkout. It leaves
`.env`, `.venv`, external key files and operation journals intact. You may remove
the checkout separately after stopping processes; reconcile every outstanding
operation before deleting any journal. To roll back, switch to the prior
release (`./install.sh --release=vX.Y.Z`) only if its journal schema is compatible.

## Small private demo

1. Diagnose installation and selected environment.
2. Search markets and inspect a bounded book/candle/trade response.
3. Observe `stream market --feed depth --symbol ETHRUSDPERP --seconds 20`.
4. With owner/account only, inspect `data state` and `data reconcile-orders`.
5. Explain freshness, history coverage and unresolved outcomes using the
   [accounting and stream contract](v2-data-and-reconciliation.md).
6. Complete the [credential-free paper demo](paper-trading.md#first-paper-session):
   initialize, simulate an order, replay its request ID, refresh and inspect history.
   Funded trading acceptance remains a separate operator exercise.
7. Explain [account-period history](history-and-performance.md), including missing
   metrics and why deposits/internal transfers are not trading profit.
