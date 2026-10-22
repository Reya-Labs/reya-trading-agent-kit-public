# Disclaimer

This software is **experimental**. The Reya Agent Kit interacts with a live decentralized exchange and can execute real financial transactions. Orders, cancellations, and position changes are irreversible once submitted.

By using this software, you accept full responsibility for any financial losses, errors, or unintended actions that result from its use, including but not limited to:

- Incorrect order parameters (price, quantity, side, symbol, slippage)
- Unintended position sizes, fills against slipped IOC limits, or partial fills
- Wrong-account targeting (the kit scopes to `PERP_ACCOUNT_ID_1` — verify it points to the account you intend)
- Cascading effects of agent-driven decisions, including loops that submit many orders in a short window
- Losses resulting from market movements, funding payments, or liquidations during automated trading

The authors and contributors of this software accept no liability for losses of any kind.

## v1 has no safety net

This release supports live execution and an isolated **local paper simulator** using current public V2 data. Read [the simulation contract](docs/paper-trading.md): funding, ADL, liquidation, protective orders, latency and market impact are not simulated. Paper results do not establish production readiness, and historical backtesting is not supported. To test without risking real funds, use the **Reya devnet1**: use the URL, chain, gateway, exchange and test credentials described in `.env.example`. Devnet1 and Cronos are separate deployments despite sharing a chain ID. Verify behavior there before pointing at mainnet (`CHAIN_ID=1729`).

## Key management

- Private keys configured in `.env` are used to sign every order this kit submits.
- Never commit `.env` or any file containing a private key to version control.
- Use a dedicated trading wallet funded only with the capital you are willing to put at risk.
- Anyone with access to the private key has full authority over the configured account.

## Risk primitives are best-effort

`order cancel_all` and `positions close_all` are batch operations that cancel or flatten *every* matching order/position on the configured account. They do not coordinate with other processes that may be modifying the same account simultaneously. Individual failures inside the batch are reported in the response but do not halt the run.

## Regulatory

The regulatory status of cryptocurrency derivatives trading varies by jurisdiction. You are responsible for ensuring your use of this software, the Reya protocol, and any resulting transactions complies with the laws applicable to you.
