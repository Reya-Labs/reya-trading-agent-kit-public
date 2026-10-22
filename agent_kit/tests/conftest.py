"""
Fixtures and model builders for agent kit CLI tests.

Strategy: unit tests with the SDK mocked. We never hit the network.

- `FakeClient` stands in for the query and trading clients. Tests configure its method
  return values directly.
- `patch_client` swaps the client factory in both `query.py` and
  `trade.py` for a `FakeClient` instance.
- `patch_market_api` / `patch_reference_api` swap the OpenAPI clients used by
  the no-auth commands (`prices`, `market info`, `market funding`).
- Builder helpers (`make_balance`, `make_position`, etc.) construct real
  Pydantic model instances so the cmd functions see exactly the data shape
  they'd see in production.
- `parse_stdout(capsys)` reads the captured stdout and returns parsed JSON —
  every cmd writes JSON to stdout, so this is how we assert outputs.
"""

from typing import Any, Optional

import json
import os
import socket
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from sdk.open_api.api.market_data_api import MarketDataApi
from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.models.account import Account
from sdk.open_api.models.account_balance import AccountBalance
from sdk.open_api.models.asset_definition import AssetDefinition
from sdk.open_api.models.asset_oracle_price import AssetOraclePrice
from sdk.open_api.models.market_definition import MarketDefinition
from sdk.open_api.models.market_summary import MarketSummary
from sdk.open_api.models.order import Order
from sdk.open_api.models.order_status import OrderStatus
from sdk.open_api.models.order_type import OrderType
from sdk.open_api.models.pagination_meta import PaginationMeta
from sdk.open_api.models.perp_execution_list import PerpExecutionList
from sdk.open_api.models.position import Position
from sdk.open_api.models.side import Side
from sdk.open_api.models.time_in_force import TimeInForce
from sdk.reya_rest_api import ReyaTradingClient

DEFAULT_ACCOUNT_ID = 8009


# --------------------------------------------------------------------------
# Fake client + monkeypatch fixtures
# --------------------------------------------------------------------------


class FakeClient:
    """Stand-in for ReyaTradingClient.

    Tests set return values directly on the AsyncMock attributes. Supports
    `async with` semantics — `__aenter__` returns self.
    """

    def __init__(self, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
        self.config = MagicMock()
        self.config.account_id = account_id
        self.config.dex_id_override = 2

        # Sub-resources for direct API access via the client.
        self.markets = create_autospec(MarketDataApi, instance=True)
        self.markets.get_perp_markets_summary.return_value = []
        self.markets.get_perp_market_summary.return_value = make_price()
        self.markets.get_asset_oracle_prices.return_value = []
        self.reference = create_autospec(ReferenceDataApi, instance=True)
        self.reference.get_perp_market_definitions.return_value = [
            make_market_def(),
            make_market_def(symbol="BTCRUSDPERP", market_id=2),
        ]
        self.wallet = AsyncMock()
        self.wallet.get_wallet_perp_executions.return_value = PerpExecutionList(
            data=[], meta=PaginationMeta(limit=100, count=0)
        )

        # Method stubs with default empty returns.
        self.start = AsyncMock(return_value=None)
        self.get_accounts = AsyncMock(
            return_value=[Account(account_id=account_id, name="test", type="SUBPERP")] if account_id is not None else []
        )
        # Enough collateral that ordinary test orders stay inside the kit's size and leverage guards.
        self.get_account_balances = AsyncMock(
            return_value=[make_balance(account_id=account_id, real_balance="100000")] if account_id is not None else []
        )
        self.get_positions = AsyncMock(return_value=[])
        self.get_open_orders = AsyncMock(return_value=[])
        sdk = create_autospec(ReyaTradingClient, instance=True)
        self.create_limit_order = sdk.create_limit_order
        self.create_trigger_order = sdk.create_trigger_order
        self.cancel_order = sdk.cancel_order

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.fixture
def fake_client() -> FakeClient:
    """A FakeClient with the default configured account_id (8009)."""
    return FakeClient()


@pytest.fixture
def patch_client(monkeypatch, fake_client: FakeClient) -> FakeClient:
    """Replace account-data and trading clients with `fake_client`."""
    monkeypatch.setattr("agent_kit.scripts.query.AccountDataClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr("agent_kit.scripts.trade.ReyaTradingClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr("agent_kit.scripts.query.get_trading_config", lambda **kw: None)
    monkeypatch.setattr("agent_kit.scripts.trade.get_trading_config", lambda **kw: None)
    return fake_client


@pytest.fixture(autouse=True)
def neutralize_api_client(monkeypatch) -> None:
    """Prevent unauth'd query commands from creating real ApiClient instances.

    The cmd_prices / cmd_market_info / cmd_market_funding code path constructs
    its own ApiClient (independent of ReyaTradingClient). Stub it out so tests
    don't open real aiohttp sessions.
    """
    fake_api_client = MagicMock()
    fake_api_client.rest_client = None
    monkeypatch.setattr("agent_kit.scripts.query.ApiClient", lambda *a, **kw: fake_api_client)


@pytest.fixture
def patch_market_api(monkeypatch) -> AsyncMock:
    """Replace `MarketDataApi` in query.py with an AsyncMock."""
    api = create_autospec(MarketDataApi, instance=True)
    monkeypatch.setattr("agent_kit.scripts.query.MarketDataApi", lambda *a, **kw: api)
    return api


@pytest.fixture
def patch_reference_api(monkeypatch) -> AsyncMock:
    """Replace `ReferenceDataApi` in query.py with an AsyncMock."""
    api = create_autospec(ReferenceDataApi, instance=True)
    monkeypatch.setattr("agent_kit.scripts.query.ReferenceDataApi", lambda *a, **kw: api)
    return api


# --------------------------------------------------------------------------
# Model builders
# --------------------------------------------------------------------------


def make_balance(
    *,
    account_id: int = DEFAULT_ACCOUNT_ID,
    asset: str = "RUSD",
    real_balance: str = "100",
    balance_deprecated: str = "100",
) -> AccountBalance:
    return AccountBalance(
        account_id=account_id,
        asset=asset,
        real_balance=real_balance,
        balance_deprecated=balance_deprecated,
    )


def make_asset_def(
    *,
    asset: str = "RUSD",
    spot_market_symbol: Optional[str] = None,
    price_haircut: str = "0",
    liquidation_discount: str = "0",
    status: str = "ENABLED",
    decimals: int = 18,
    display_decimals: int = 6,
) -> AssetDefinition:
    return AssetDefinition(
        asset=asset,
        spot_market_symbol=spot_market_symbol,
        price_haircut=price_haircut,
        liquidation_discount=liquidation_discount,
        status=status,
        decimals=decimals,
        display_decimals=display_decimals,
    )


def make_market_def(
    *,
    symbol: str = "ETHRUSDPERP",
    market_id: int = 1,
    min_order_qty: str = "0.01",
    qty_step_size: str = "0.001",
    tick_size: str = "0.001",
    liquidation_margin_parameter: str = "0.03",
    initial_margin_parameter: str = "0.04",
    max_leverage: int = 25,
    oi_cap: str = "4500",
) -> MarketDefinition:
    return MarketDefinition(
        symbol=symbol,
        market_id=market_id,
        min_order_qty=min_order_qty,
        qty_step_size=qty_step_size,
        tick_size=tick_size,
        liquidation_margin_parameter=liquidation_margin_parameter,
        initial_margin_parameter=initial_margin_parameter,
        max_leverage=max_leverage,
        oi_cap=oi_cap,
    )


def make_price(
    *,
    symbol: str = "ETHRUSDPERP",
    oracle_price: str | None = "2000",
    mark_price: str | None = "2000",
    updated_at: int = 1779000000000,
) -> MarketSummary:
    return make_market_summary(
        symbol=symbol, oracle_price=oracle_price, mark_price=mark_price, prices_updated_at=updated_at
    )


def make_asset_price(asset: str = "ETH", price: str = "2000") -> AssetOraclePrice:
    return AssetOraclePrice(asset=asset, oracle_price=price, updated_at=1779000000000)


def make_market_summary(
    *,
    symbol: str = "ETHRUSDPERP",
    updated_at: int = 1779000000000,
    oi_qty: str = "150",
    funding_rate: str = "0.0001",
    # Equal to make_position's entry funding value: no unsettled funding unless a test sets it.
    long_funding_value: str = "1000",
    short_funding_value: str = "1000",
    oracle_price: str | None = "2000",
    mark_price: str | None = "2000",
    prices_updated_at: int | None = 1779000000000,
) -> MarketSummary:
    return MarketSummary(
        symbol=symbol,
        updated_at=updated_at,
        oi_qty=oi_qty,
        funding_rate=funding_rate,
        long_funding_value=long_funding_value,
        short_funding_value=short_funding_value,
        volume24h="0",
        px_change24h="0",
        oracle_price=oracle_price,
        mark_price=mark_price,
        prices_updated_at=prices_updated_at,
    )


def make_position(
    *,
    account_id: int = DEFAULT_ACCOUNT_ID,
    symbol: str = "ETHRUSDPERP",
    side: Side = Side.B,
    qty: str = "0.05",
    avg_entry_price: str = "2000",
    exchange_id: int = 2,
    avg_entry_funding_value: str = "1000",
    last_trade_sequence_number: int = 1,
) -> Position:
    return Position(
        exchange_id=exchange_id,
        symbol=symbol,
        account_id=account_id,
        qty=qty,
        side=side,
        avg_entry_price=avg_entry_price,
        avg_entry_funding_value=avg_entry_funding_value,
        last_trade_sequence_number=last_trade_sequence_number,
    )


def make_order(
    *,
    order_id: str = "test-order-id",
    account_id: int = DEFAULT_ACCOUNT_ID,
    symbol: str = "ETHRUSDPERP",
    side: Side = Side.B,
    order_type: OrderType = OrderType.LIMIT,
    qty: str = "0.05",
    limit_px: str = "2000",
    trigger_px: Optional[str] = None,
    time_in_force: Optional[TimeInForce] = TimeInForce.GTC,
    status: OrderStatus = OrderStatus.OPEN,
    exchange_id: int = 2,
    created_at: int = 1779000000000,
    last_update_at: int = 1779000000000,
) -> Order:
    return Order(
        exchange_id=exchange_id,
        symbol=symbol,
        account_id=account_id,
        order_id=order_id,
        qty=qty,
        side=side,
        limit_px=limit_px,
        order_type=order_type,
        trigger_px=trigger_px,
        time_in_force=time_in_force,
        status=status,
        created_at=created_at,
        last_update_at=last_update_at,
    )


# --------------------------------------------------------------------------
# Stdout JSON helper
# --------------------------------------------------------------------------


def parse_stdout(capsys: pytest.CaptureFixture) -> Any:
    """Read the captured stdout and return the parsed JSON (json.loads itself returns Any)."""
    captured = capsys.readouterr()
    return json.loads(captured.out)


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch, tmp_path):
    """Tests must never load a developer's credentials or reach a real venue."""
    from agent_kit.scripts import _env

    monkeypatch.setattr(_env, "KIT_ROOT", tmp_path)
    for key in list(os.environ):
        if key.startswith(("REYA_", "PERP_")) or key == "CHAIN_ID":
            monkeypatch.delenv(key)

    monkeypatch.setenv("REYA_STATE_DIR", str(tmp_path / "writer-state"))
    attempts: list = []

    def deny_network(*args, **kwargs):
        attempts.append([a for a in args if not isinstance(a, socket.socket)][:2])
        raise AssertionError("Offline test attempted network access")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    # Name resolution too: a real lookup can stall for minutes, and a read the code treats as
    # optional (`except Exception`) would swallow the refusal, so any attempt fails the test.
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    yield
    assert not attempts, f"Offline test attempted network access: {attempts}"


@pytest.fixture(autouse=True)
def _instant_position_polls(monkeypatch):
    """Keep bounded position/order re-reads (counted by tests) without real sleeps between them."""
    monkeypatch.setattr("agent_kit.scripts.trade.POSITION_POLL_S", 0)


@pytest.fixture(autouse=True)
def _short_stop_settlement(monkeypatch):
    """A runner stop reads settlement for about 1.5 s (three observations) without waiting on the warm feed.

    The real budget is the deployment's indexing ceiling; tests of that wait patch runner.indexing_ceiling.
    """
    monkeypatch.setattr("agent_kit.scripts.runner.indexing_ceiling", lambda api_url: 0)
    monkeypatch.setattr("agent_kit.scripts.runner.STOP_SETTLE_MIN_S", 1.5)


@pytest.fixture(autouse=True)
def _no_release_check(monkeypatch):
    """Keep the suite offline: agent init/status would otherwise ask GitHub for the latest release."""
    monkeypatch.setenv("REYA_KIT_UPDATE_CHECK", "0")
