"""Unsigned account reads using the SDK's generated public data endpoints.

ReyaTradingClient constructs a signer even for GET requests. Keep inspection
independent of private keys, while sharing the same deployment configuration.
"""

from sdk.open_api.api.market_data_api import MarketDataApi
from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.api.wallet_data_api import WalletDataApi
from sdk.open_api.api_client import ApiClient
from sdk.open_api.configuration import Configuration
from sdk.reya_rest_api.config import TradingConfig


class AccountDataClient:
    def __init__(self, config: TradingConfig):
        self.config = config
        self._api_client = ApiClient(Configuration(host=config.api_url))
        self.wallet = WalletDataApi(self._api_client)
        self.reference = ReferenceDataApi(self._api_client)
        self.markets = MarketDataApi(self._api_client)

    async def __aenter__(self) -> "AccountDataClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._api_client.close()

    async def get_accounts(self):
        return await self.wallet.get_wallet_accounts(address=self.config.owner_wallet_address)

    async def get_positions(self):
        return await self.wallet.get_wallet_positions(address=self.config.owner_wallet_address)

    async def get_open_orders(self):
        return await self.wallet.get_wallet_open_orders(address=self.config.owner_wallet_address)

    async def get_account_balances(self):
        return await self.wallet.get_wallet_account_balances(address=self.config.owner_wallet_address)
