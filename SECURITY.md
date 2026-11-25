# Security

## Scope

By default the kit signs with an **agent key** it generated itself
(`PERP_AGENT_PRIVATE_KEY` in `.env`, written by `agent init`). The owner approves
that key's address once in the Reya app; the on-chain permission lets it place and
cancel orders on the owner's accounts and nothing else — it cannot withdraw,
transfer or change permissions, and it can be revoked on the app's API page at any
time. The kit additionally refuses to trade any account not recorded in
`PERP_AGENT_ACCOUNT_IDS`, because the on-chain permission is wallet-wide. The kit
never asks for, stores or reads the owner's wallet key.

In **own-key mode** (`REYA_SIGNER=own-key` with `PERP_PRIVATE_KEY_FILE` or
`PERP_PRIVATE_KEY_1`) the kit holds a wallet key you chose to give it. Anyone with
that key — or with shell access to a machine holding it — has the authority of that
wallet. The host running this kit is part of the security perimeter in both modes.

## Key handling

- Prefer the default agent key. Fund the dedicated agent account only with the
  capital you are willing to automate; that balance bounds the loss.
- In own-key mode, use a **dedicated wallet** funded only with what you are
  willing to put at risk. Do not reuse a wallet that holds other assets.
- All account commands scope to `PERP_ACCOUNT_ID_1` — verify it points at the
  account you intend before going live.
- Never paste private keys into agent prompts, chat logs, shell history, issues or
  commit messages. `.env` is gitignored; keep it that way and keep it `chmod 600`
  (`doctor` warns when it is not). The agent key is written there by the kit and is
  never printed; treat the file as write-only.
- Develop and test against devnet1 (`CHAIN_ID=89346162`,
  `REYA_API_URL=https://api-devnet.reya-cronos.network/v2`; use the matching
  gateway and exchange in `.env.example`) before pointing at mainnet.

## If a key is compromised

**Agent key** (the default): revoke it on the app's API page with one signature;
it stops working immediately. Then cancel resting orders and flatten exposure from
the app or from a freshly approved key, and run `agent init` to generate a new one
(the old key cannot be reused once revoked).

**Own key:** act in this order, while you still control the account:

1. Cancel resting orders and flatten exposure in one command (preview first with
   `--preview`): `python -m agent_kit.scripts.trade positions close_all --all --cancel_orders`.
   Spot orders are not covered; cancel them in the app.
2. Withdraw collateral to a safe wallet via [app.reya.xyz](https://app.reya.xyz).
3. Rotate: create a fresh wallet/account, update `.env`, and stop using the
   compromised key everywhere.
4. Audit for activity you did not initiate: open orders via
   `python -m agent_kit.scripts.query orders`, fills via
   `python -m agent_kit.scripts.data history perpExecutions`, order history via
   `python -m agent_kit.scripts.data history orderHistory`, and transfers via
   `python -m agent_kit.scripts.data history transfers`.

## Reporting a vulnerability

Report vulnerabilities in this kit privately via GitHub's
[private vulnerability reporting](https://github.com/Reya-Labs/reya-trading-agent-kit/security/advisories/new)
rather than a public issue. Issues in the Reya protocol or API itself should go
through Reya's official channels.
