"""The latency probe acknowledges orders locally: order entry is never called."""

import time
from decimal import Decimal
from unittest.mock import AsyncMock

from eth_account import Account
from sdk.open_api.api.order_entry_api import OrderEntryApi
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api.config import TradingConfig
from sdk.reya_rest_api.models.orders import LimitOrderParameters

from agent_kit.scripts import latency, runner
from agent_kit.tests.conftest import make_market_def, make_price


async def test_probe_orders_never_reach_order_entry(monkeypatch):
    for name in ("create_order", "cancel_order", "cancel_all", "cancel_all_after", "modify_order"):
        monkeypatch.setattr(OrderEntryApi, name, AsyncMock(side_effect=AssertionError("order entry called")))
    key = "0x" + "22" * 32
    config = TradingConfig(
        private_key=key,
        owner_wallet_address=Account.from_key(key).address,
        account_id=8009,
        api_url="https://api.reya.xyz/v2",
        chain_id=1729,
        dex_id_override=2,
    )
    async with latency.DryRunnerClient(config) as client:
        client.configure(runner.Policy(frozenset({"ETHRUSDPERP"}), Decimal(100), Decimal(100), Decimal("0.01"), 10, 10))
        client.lease.state["runner"] = {"requests": [], "acknowledged_orders": [], "submitted_notional": "0"}
        monkeypatch.setattr(
            client.reference, "get_perp_market_definitions", AsyncMock(return_value=[make_market_def()])
        )
        monkeypatch.setattr(client.reference, "get_spot_market_definitions", AsyncMock(return_value=[]))
        await client.start()
        summary = AsyncMock(return_value=make_price(updated_at=int(time.time() * 1000)))
        monkeypatch.setattr(client.markets, "get_perp_market_summary", summary)
        params = LimitOrderParameters(
            symbol="ETHRUSDPERP",
            is_buy=True,
            limit_px="1500",
            qty="0.01",
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        response = await client.create_limit_order(params)
        assert response.status.value == "OPEN" and client.orders.count == 1
        await client.refresh()  # the countdown is acknowledged locally too
        await client.disarm()
