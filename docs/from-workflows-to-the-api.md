# From hosted strategies to the API

A hosted strategy is a set of rules that Reya runs for you on your Agent account: "sell $30 of ETH when the
mark crosses down through 2,492, with a stop-loss at 2,560" ([hosted strategies](hosted-workflows.md)). A
strategy can trade several markets on one trigger, rest orders on the book and exit on a target, a profit or a
time. When you need more than a strategy can express, write the rule yourself against Reya's API. Use the API
for:
- grids, ladders and trailing stops;
- pairs by ratio, or sizes that follow a hedge;
- conditions and signals of your own, including ones from outside Reya;
- changing a rule while it runs.

You trade the same venue, the same mark price and the same order types. The difference is who runs the rule.
With a strategy, Reya runs it. With the API, your program runs it, and the responsibilities that go with that
become yours.

The API itself is documented in the [developer docs](https://docs.reya.xyz/developers). Runnable Python
examples for this guide are in the SDK's
[`examples/strategies`](https://github.com/Reya-Labs/reya-python-sdk/tree/main/examples/strategies).

## How fast each one reacts

Reya's mark price is recalculated once a second ([mark price](https://docs.reya.xyz/developers/pricing-and-funding/mark-price)).
Reya's own stop-loss and take-profit orders fire on that mark, and so does a hosted strategy, a few tens of
milliseconds later. A program that uses the API sees the same mark on the WebSocket market summary.

None of them can act on a price that is touched and reversed within one second. Your program also adds its
own network time to the API. For a move that lasts less than a second, use a resting limit order on the book.

## What maps to what

| In a strategy | With the API, you build | Developer docs |
| -- | -- | -- |
| **Trigger:** the mark crosses a level (`down` or `up`). Touching the level counts. | Subscribe to `/v2/perpMarket/{symbol}/summary` and read `markPrice`. Fire when the previous fresh mark was on the other side and the new one is at or past the level. | [WebSocket Info API](https://docs.reya.xyz/developers/api-reference/websocket-api-reference) |
| **Freshness:** a run never fires on a mark older than 10 s. A crossing during a feed gap is reported, never fired late. | Check each mark's age from `pricesUpdatedAt`. After a gap or a reconnect, wait for a fresh mark instead of firing on the jump. | [WebSocket Info API](https://docs.reya.xyz/developers/api-reference/websocket-api-reference) |
| **The order:** one IOC at the mark ± the bound (10–100 bps, 25 by default). It is sized from the dollar amount at that limit and rounded down to the market's quantity step. | `createOrder` with `orderType: LIMIT`, `timeInForce: IOC`, `reduceOnly: false`. `limitPx` = mark × (1 ± bound), rounded toward the mark to `tickSize`. `qty` = notional ÷ `limitPx`, rounded down to `qtyStepSize`, and at least `minOrderQty`. | [Order types](https://docs.reya.xyz/developers/order-entry/order-types-and-tif), [markets](https://docs.reya.xyz/developers/reference/markets-and-assets) |
| **Allocation:** total dollars across every firing. | Your own running total of filled quantity × price. Stop when it is used. | [Executions](https://docs.reya.xyz/developers/executions-and-settlement/executions-and-trade-history) |
| **Repeat:** `once` or `each`. After a firing, a strategy re-arms only once the mark moves back 0.5% past the level, after a 15-minute cooldown. | Your own state machine: armed, fired, waiting to move back, armed again. Pick your own band and cooldown. | |
| **Expiry.** | Your own deadline. For resting orders, `GTT` with `expiresAfter`. | [Order types](https://docs.reya.xyz/developers/order-entry/order-types-and-tif) |
| **Conditions:** the 15-minute average funding; position size. | Average `fundingRate` from the market summary yourself. Read your position at the crossing (the WebSocket `positions` channel, or REST). | [WebSocket Info API](https://docs.reya.xyz/developers/api-reference/websocket-api-reference) |
| **Protective stop:** one stop-loss on the whole position, armed after the first fill. | Once the fill shows in your position, a `STOP_LOSS` with `triggerPx` and `limitPx` and no `qty`. It protects the whole live position when it fires. Each account has at most one stop-loss and one take-profit per market, and the two form an OCO pair. | [Trigger orders](https://docs.reya.xyz/developers/order-entry/trigger-orders) |
| **Stopping a run** ([what a stop does](hosted-workflows.md#stop)). | Stop your program. Cancel your stop with `cancelOrder` (or `cancelAll`) if you want it gone. Close a position with a reduce-only IOC. | [Order lifecycle](https://docs.reya.xyz/developers/order-entry/order-lifecycle) |
| **Paper mode:** simulated fills; nothing is sent. | A dry run: log the order you would send (the example does this unless you pass `--live`), or devnet1 with test funds. | [Environments](https://docs.reya.xyz/developers/getting-started/environments) |
| **A lost answer:** the outcome is read from the order stream, and the run is blocked until it is known. | Never resend after a timeout or `ORDER_OUTCOME_UNKNOWN_ERROR`. Reconcile through `openOrders`, `orderChanges` and executions first. | [Ambiguous outcomes](https://docs.reya.xyz/developers/connectivity/handling-ambiguous-outcomes) |
| **Order tags.** | Set your own non-zero `clientOrderId` on every order, so you can find it again. | [Order lifecycle](https://docs.reya.xyz/developers/order-entry/order-lifecycle) |
| **Reya's hosted signer** places the run's orders. | Your own signer wallet, approved with your owner wallet on the API Keys page. The approval covers every account your wallet owns. It lets the signer place and cancel orders; it cannot withdraw or transfer. | [Signer authorization](https://docs.reya.xyz/developers/getting-started/signer-authorization) |

## What you take on

- **Keeping it running.** A strategy runs with your computer off; your program runs only where you host it.
  Stop-loss orders you arm rest on Reya either way.
- **Staleness and reconnects.** Treat a mark older than about 10 s as unknown. Reconnect and re-subscribe on a
  `1012` or `1013` close. The SDK's WebSocket client does both.
- **Unclear outcomes.** Reconcile before you resend.
- **Nonces.** Each signer's nonces must increase. Run one program per signer.
- **Rate limits.** See [below](#rate-limits-that-matter-for-a-bot).
- **Protection.** Nothing arms a stop for you. Arm one as soon as the fill shows in your position, and check
  that it armed.

## Setting up

1. **Start on devnet1.** It has its own endpoints, chain ID and signer approvals
   ([environments](https://docs.reya.xyz/developers/getting-started/environments)).
2. **Use a new signer wallet for your program, not the kit's agent key.** The kit keeps its own nonce state for
   the agent key, which another program would not share. Approve the new signer's public address with your owner
   wallet on the API Keys page, and keep its private key with your program
   ([signer authorization](https://docs.reya.xyz/developers/getting-started/signer-authorization)).
3. **Use an account of its own.** Don't use the Agent account a strategy runs on: each account has at most one
   stop-loss per market, and a run leaves an existing stop alone.
4. **Install the Python SDK** as the [quickstart](https://docs.reya.xyz/developers/getting-started/onboarding)
   describes, and fill in its `.env`:
   - `PERP_ACCOUNT_ID_1`: your program's account;
   - `PERP_WALLET_ADDRESS_1`: your owner wallet;
   - `PERP_PRIVATE_KEY_1`: the new signer's key;
   - the environment values from the SDK's `.env.example`, plus `REYA_DEX_ID=1` on devnet1 (the exchange ID
     devnet1 registers, as the kit uses there).

## The examples

The SDK's [`examples/strategies`](https://github.com/Reya-Labs/reya-python-sdk/tree/main/examples/strategies)
has two:

- **`mark_feed.py`:** a mark-price feed for one market that reconnects and treats a mark older than 10 s as
  stale. It hands each new mark to your code, and a slow consumer drops the oldest marks, never the newest.
  Running it alone prints the mark and needs no key:
  `python -m examples.strategies.mark_feed ETHRUSDPERP`.
- **`price_cross_entry.py`:** the API version of a `once` strategy with a stop. It does four things:
  1. waits for a fresh crossing;
  2. sends one IOC at the mark ± the bound, sized from your dollar amount, and says the real size first;
  3. waits until the fill shows in your position;
  4. arms a whole-position `STOP_LOSS` whose IOC child may fill up to 1% beyond the trigger.

  It is a dry run unless you pass `--live`. It never resends an entry whose outcome is unclear. If it cannot
  arm the stop, it says the position is unprotected.

  ```bash
  python -m examples.strategies.price_cross_entry --symbol ETHRUSDPERP --direction down --level 2492 \
      --side sell --notional 30 --stop-px 2560          # add --live to send
  ```

To go further:
- **Repeat:** after a firing, wait until the mark moves back past your band before arming again.
- **Take-profit:** add a `TAKE_PROFIT`; it forms an OCO pair with the stop.
- **Pairs:** give each leg its own entry and its own stop. A stop fires on its own market's mark, so a "close both
  legs when ETH hits X" rule needs your program to send the second leg's close.

## Rate limits that matter for a bot

These are the Standard account tier's published values; the
[rate limits](https://docs.reya.xyz/developers/connectivity/rate-limits) page has the full rules and the Market
Maker tier.

| What | Allowance |
| -- | -- |
| `createOrder` / `modifyOrder` | 60 a minute, burst 5, per account. An entry with a stop and a take-profit uses 3. |
| `cancelOrder` | 120 a minute, burst 10 |
| `cancelAll` | 10 a minute, burst 5 |
| `cancelAllAfter` | 100 a minute, burst 10 |
| Resting orders | 200 per account, 50 per market, $250,000 combined notional. Armed stop-loss and take-profit orders don't count. |
| WebSocket Info, per connection | 20 messages a second (burst 1,200); 10 subscriptions a second (burst 40); 500 active subscriptions |
| Per IP address | 2,000 REST order-management requests a minute; 1,500 other REST requests a minute; 300 new WebSocket connections a minute; 100 Info connections; 10 distinct wallets on Info |
| WebSocket Order Entry | Approved market-maker IPs only. Other programs use REST `createOrder`. |

- **A rate-limited request** returns `RATE_LIMITED_ERROR` with `retryAfterMs`. Wait at least that long.
- **Going over a per-IP limit** blocks that scope for 60 seconds (HTTP 429), cancellations included.
- **Updates the server pushes** don't count against your message allowance. Subscribe once instead of polling
  REST.

## Before going live

- [ ] It ran on devnet1 end to end: a crossing, an entry, a stop that armed.
- [ ] A dry run on mainnet prints the order and size you expect.
- [ ] It refuses to fire on a stale mark, and after a reconnect.
- [ ] On an unclear outcome it stops and reconciles; it never resends.
- [ ] Every position it opens gets a stop on Reya, and it says so loudly when one doesn't.
- [ ] Its account holds only what you are willing to risk on this strategy.
