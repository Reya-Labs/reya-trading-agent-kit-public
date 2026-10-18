# Production trading

The existing perp commands support Reya mainnet through the pinned SDK. Mainnet uses the same command interface as devnet1; configure the production API, chain, gateway, exchange and account together. Supported commands include IOC market orders, GTC limits, cancellations, TP/SL and reduce-only closing.

## Configure and inspect

Use a separate kit checkout for mainnet so switching deployments does not reuse a devnet `.env`. Install the kit, then copy `.env.mainnet.example` to `.env` in that checkout and populate the mainnet owner address and account ID:

| Setting | Mainnet value |
| --- | --- |
| `CHAIN_ID` | `1729` |
| `REYA_API_URL` | `https://api.reya.xyz/v2` |
| `REYA_ORDERS_GATEWAY` | `0xfc8c96be87da63cecddbf54abfa7b13ee8044739` |
| `REYA_DEX_ID` | `2` |

`PERP_WALLET_ADDRESS_1` is the **account owner**, even when a delegated wallet signs orders. `PERP_ACCOUNT_ID_1` must belong to that owner on the selected deployment. The kit checks this before account reads and writes, so a wrong owner/account pair produces an error rather than an apparently empty account. A signer is needed only for writes: by default the agent key from `agent init`; with `REYA_SIGNER=own-key`, `PERP_PRIVATE_KEY_FILE` or `PERP_PRIVATE_KEY_1` (not both). Account inspection uses unsigned data endpoints. Trading permission for a delegated signer still needs to be established on the venue.

Shell environment variables override `.env`. Inspect the selected deployment through health before proceeding:

```bash
python -m agent_kit.scripts.health
python -m agent_kit.scripts.query market info --symbol ETHRUSDPERP
python -m agent_kit.scripts.query prices --symbol ETHRUSDPERP
python -m agent_kit.scripts.query market funding --symbol ETHRUSDPERP
python -m agent_kit.scripts.query positions
python -m agent_kit.scripts.query orders
python -m agent_kit.scripts.query balances
python -m agent_kit.scripts.query margin
```

Require `health.api_url` to be the production URL and resolve any account-read errors before trading. The health response proves connectivity and reports the locally installed SDK version; it does not attest the server version, trading permission, market admission or available liquidity. `margin` estimates collateral with haircuts and excludes unrealized PnL; it is not authoritative account equity or execution admission.

## Account isolation and concurrent writers

Use `query accounts` with the owner address to discover accounts, then set `PERP_ACCOUNT_ID_1` to the intended perp account. Inspect its balances, positions and open orders before trading. Account discovery is unsigned and needs no account ID or key.

Use a separately funded subaccount for a strategy so cleanup and exposure do not affect other strategies. The [account setup commands](account-setup-and-runner.md) use the supported `executeReya` API relay with deployment/ownership checks and API outcome verification. Creation needs no signature. Transfers use a separate owner key to sign `transferFromMAToMA`; Reya's executor submits the transaction and pays gas. No RPC endpoint or user gas budget is required. This flow does not automatically grant trading permissions.

Stop competing bots/CLI writers during a canary. The base SDK serializes nonces within one Python process only. Updated kit commands add cooperative host-local locks and durable counters; another process sharing the owner/signer context can race and receive `INVALID_NONCE`. Even a different subaccount does not by itself establish a separate nonce context. For agent commands on one dedicated account, use [the runner](account-setup-and-runner.md). For other concurrent strategies, use a coordinated writer service or independently provisioned and verified nonce contexts. After an ambiguous submission, inspect orders/executions before deciding whether to write again; never blindly retry.

Both cleanup commands require exactly one of `--symbol X` and `--all`, on every deployment and for previews too. `--symbol` is not strategy isolation: all orders/positions for that symbol on the account may be affected. `--all` deliberately affects the whole configured account. Preview the exact scope, inspect it, then authorize the real operation; preview does not reserve/freeze the account between calls.

## Live operation and acceptance

The [command reference](../SKILL.md) applies to mainnet. TP/SL require an explicit child limit and `IOC` or `GTC` time in force. They use the mark price and attempt to reduce the full position. A trigger firing or an order receiving an ID does not guarantee execution.

An account owner/operator should perform production acceptance with an isolated account and an agreed symbol, quantity/notional, slippage ceiling and loss budget. Use the market's actual minimum quantity and step, then record these outcomes:

1. Public and account reads succeed for the intended mainnet account, with no pre-existing exposure or orders in the test scope.
2. A small resting limit appears in open orders, is cancelled and disappears. If it fills unexpectedly, reconcile that exposure before continuing.
3. One small IOC entry fills and its position appears in readback. Partial, cancelled, unknown or timed-out outcomes are reconciled before another write.
4. TP and SL are armed with the selected child limits/TIF, read back as the expected types and `triggered: false`, then cancelled. This proves arming/cancellation only; exercising trigger firing/OCO is a separate validation.
5. Preview the test position's reduce-only close, execute it, and confirm no residual position or orders. `close_all` waits briefly for readback; on a remaining/unknown result, read account state before deciding on another operation.

Keep evidence free of private keys. Record the kit commit, SDK pin, UTC time, chosen size/limits, responses and final positions/orders. Only the operator's completed canary establishes live production acceptance for that account and deployment. An offline signature test or a successful devnet run does not establish that fact.

## Verification status

Current boundary: the author's checks covered mainnet public reads, offline mainnet signing and unsigned account reads on devnet1. Separately, the operator reported a successful plain-CLI mainnet canary. Account creation passed on mainnet; its transfer and dedicated-account runner remain untested there. TP/SL firing was reported on devnet only. The stabilization revision requires its own operator rehearsal.
