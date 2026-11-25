# Devnet validation for the SDK migration

This is an operator-run, opt-in test. It sends live **devnet1** orders; CI runs only offline tests. Use an isolated account with no existing orders or positions and enough collateral for the minimum order plus fees. Never run against a shared market-maker account. Devnet1 success does not establish mainnet readiness.

## 1. Confirm the environment and baseline

Set the kit-root `.env` using the devnet1 section of `.env.example`: chain `89346162`, API `https://api-devnet.reya-cronos.network/v2`, gateway `0x7Ec89E555c771D2B5939aBE5C4E4291852633D4D`, exchange ID `1`. Verify the deployment's exchange ID before running. Cronos has a different API and gateway. Do not print credentials.

From the repository root, with its venv active:

```bash
python -m agent_kit.scripts.health
python -m agent_kit.scripts.query prices --symbol ETHRUSDPERP
python -m agent_kit.scripts.query market info --symbol ETHRUSDPERP
python -m agent_kit.scripts.query market funding --symbol ETHRUSDPERP
python -m agent_kit.scripts.query balances
python -m agent_kit.scripts.query margin
python -m agent_kit.scripts.query positions
python -m agent_kit.scripts.query orders
```

Require `health.api_url` to match devnet1, all reads to succeed, and positions/orders to be empty. Verify collateral covers the test with headroom; `margin` is only a collateral estimate, not authoritative execution admission. Choose `QTY` equal to the reported `min_order_qty`, and reject the test if its notional exceeds $100. The examples below use `0.01` ETH; verify the current minimum first.

## 2. Place, read back, and cancel a resting limit

Choose `LIMIT` below the current market so the small buy should rest (for example 2% below the mark price), then substitute its numeric price:

```bash
python -m agent_kit.scripts.trade order limit ETHRUSDPERP --side long --qty 0.01 --price <LIMIT>
python -m agent_kit.scripts.query orders --symbol ETHRUSDPERP
python -m agent_kit.scripts.trade order cancel --order_id <RETURNED_NUMERIC_ORDER_ID>
python -m agent_kit.scripts.query orders --symbol ETHRUSDPERP
python -m agent_kit.scripts.query positions --symbol ETHRUSDPERP
```

Require `OPEN`, zero filled quantity, matching open-order readback, `CANCELLED`, and then no orders or positions. If it fills unexpectedly, stop and perform the cleanup below instead of opening another position.

## 3. Small fill and protective orders

```bash
python -m agent_kit.scripts.trade order market ETHRUSDPERP --side long --qty 0.01
python -m agent_kit.scripts.query positions --symbol ETHRUSDPERP
python -m agent_kit.scripts.query prices --symbol ETHRUSDPERP
```

Require `FILLED` and a `0.01` long position in readback. A nonzero exit may still have partial execution: inspect `filled_qty` and positions, then clean up; never blindly repeat the entry. On a timeout or unknown response, reconcile account state before another write.

Choose a TP trigger about 3% above the current **mark**, and an SL about 3% below it. Choose each child limit explicitly and within the venue's permitted band (using the trigger price itself is a conservative smoke-test choice, but does not guarantee a fill). Substitute the numeric prices below:

```bash
python -m agent_kit.scripts.trade order tp ETHRUSDPERP --side long --trigger_px <TP> --limit_px <TP> --time_in_force IOC
python -m agent_kit.scripts.trade order sl ETHRUSDPERP --side long --trigger_px <SL> --limit_px <SL> --time_in_force IOC
python -m agent_kit.scripts.query orders --symbol ETHRUSDPERP
```

Require two `OPEN` orders, with `TAKE_PROFIT` and `STOP_LOSS`, the requested limits/time in force, and `triggered: false`. This validates **arming and cancellation**, not trigger firing, OCO execution, or liquidity under stress. If either submission fails, immediately reconcile and clean up the test position.

## 4. Cancel, close reduce-only, and verify

The test plan explicitly includes cleanup of its own orders and position. Preview must match only those artifacts before execution:

```bash
python -m agent_kit.scripts.trade order cancel_all --symbol ETHRUSDPERP --preview
python -m agent_kit.scripts.trade order cancel_all --symbol ETHRUSDPERP
python -m agent_kit.scripts.query orders --symbol ETHRUSDPERP
python -m agent_kit.scripts.trade positions close_all --symbol ETHRUSDPERP --preview
python -m agent_kit.scripts.trade positions close_all --symbol ETHRUSDPERP
python -m agent_kit.scripts.query positions
python -m agent_kit.scripts.query orders
python -m agent_kit.scripts.query balances
python -m agent_kit.scripts.query margin
```

Require cancelled protective orders, a reduce-only close execution, and **empty positions and orders on final readback**. `close_all` can exit 1 if its bounded readback still shows a position: poll reads briefly before deciding whether another close is needed. Partial/no-fill outcomes need explicit reconciliation; an order ID or successful submission is insufficient.

Record the SDK tag/commit, kit commit, environment, UTC time, symbol/size, sanitized CLI responses and final state. Keep credentials out of evidence and source control. Record failed steps separately from successful ones.
