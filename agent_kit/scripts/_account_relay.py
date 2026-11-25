"""Supported API transport and local calldata/signing for account setup.

Keep executeReya isolated here until the dedicated operation endpoints ship.
Encoding calldata and signing EIP-712 messages do not require an RPC provider.
"""

import asyncio
import json
from decimal import Decimal

import aiohttp
from eth_abi import encode
from eth_account.messages import encode_typed_data
from eth_utils import is_address, keccak, to_checksum_address

from agent_kit.scripts._env import DEPLOYMENTS
from agent_kit.scripts._http import USER_AGENT, RegionUnavailableError, region_blocked

RELAY_PATH = "/api/transaction-gelato/executeReya"
MAX_API_INTEGER = 2**53 - 1
# V2 AccountType values the kit knows. AGENTPERP (API spec 3.7.3) is the Agent account: a sub perp account
# designated for agents, which is a SUBPERP for margin, transfers and listing. The spec may add values: an
# owned account of a type not listed here is still owned (accounts() lists it with its own type), but it
# never passes a type check, so the kit takes no action on it.
AGENT_PERP = "AGENTPERP"
SUB_PERP_TYPES = ("SUBPERP", AGENT_PERP)
PERP_TYPES = ("MAINPERP", *SUB_PERP_TYPES)
ACCOUNT_TYPES = (*PERP_TYPES, "SPOT")
# Exact API deployment, not chain ID: devnet1 and Cronos share a chain ID.
# Sources: Reya common contractAddresses.ts and reya-deployments fork fixtures.
CONTRACTS = {
    "https://api.reya.xyz/v2": (
        "0xA763B6a5E09378434406C003daE6487FbbDc1a80",
        "0xCd2869D1eB1Bc8991bc55De9E9b779e912fAf736",
        "0xa9F32a851B1800742e47725DA54a09A7Ef2556A3",
    ),
    "https://api-devnet.reya-cronos.network/v2": (
        "0xC33D0A4FC05aF98447126f1680cA7316de29e5d4",
        "0xDEDbde7e82B66E499b8FC8a472a5E857be1494DE",
        "0x9DE724e7b3facF87Ce39465D3D712717182e3e55",
    ),
    "https://api-cronos.reya.xyz/v2": (
        "0xC6fB022962e1426F4e0ec9D2F8861c57926E9f72",
        "0x94ccae812f1647696754412082dd6684c2366a7f",
        "0x9DE724e7b3facF87Ce39465D3D712717182e3e55",
    ),
}


class SetupError(ValueError):
    """Operator-facing validation error, without credentials or signed calldata."""


def address(value) -> str:
    if not isinstance(value, str) or not is_address(value) or int(value, 16) == 0:
        raise SetupError("A nonzero EVM owner address is required")
    return to_checksum_address(value)


def api_integer(value) -> int:
    # Legacy APIs serialize account IDs and nonce reads as JavaScript numbers.
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        result = int(value)
    else:
        raise SetupError("API returned an invalid integer")
    if not 0 <= result <= MAX_API_INTEGER:
        raise SetupError("API integer exceeds exact JavaScript range; lossless API support is required")
    return result


def calldata(signature: str, types: list[str], values: list) -> str:
    return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()


def transfer_message(config, source: int, target: int, units: int, nonce: int, deadline: int):
    core, periphery, token = CONTRACTS[config.api_url]
    return encode_typed_data(
        domain_data={"name": "Reya", "version": "1", "verifyingContract": core},
        message_types={
            "ExecuteBySig": [
                {"name": "verifyingChainId", "type": "uint256"},
                {"name": "caller", "type": "address"},
                {"name": "accountId", "type": "uint128"},
                {"name": "commands", "type": "Command[]"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "extraSignatureData", "type": "bytes"},
            ],
            "Command": [
                {"name": "commandType", "type": "uint8"},
                {"name": "inputs", "type": "bytes"},
                {"name": "marketId", "type": "uint128"},
                {"name": "exchangeId", "type": "uint128"},
            ],
        },
        message_data={
            "verifyingChainId": config.chain_id,
            "caller": periphery,
            "accountId": source,
            "commands": [
                {
                    "commandType": 4,
                    "inputs": encode(["(uint128,address,uint256)"], [(target, token, units)]),
                    "marketId": 0,
                    "exchangeId": 0,
                }
            ],
            "nonce": nonce,
            "deadline": deadline,
            "extraSignatureData": b"",
        },
    )


def relay_payload(config, plan: dict, signer=None) -> dict:
    core, periphery, token = CONTRACTS[config.api_url]
    if plan["action"] == "create":
        target = core
        data = calldata("createAccount(address)", ["address"], [plan["owner"]])
        metadata = {"action": "createAccount", "sender": plan["owner"]}
    else:
        signature = signer.sign_message(
            transfer_message(
                config,
                plan["from_account"],
                plan["to_account"],
                int(plan["amount_units"]),
                int(plan["nonce"]),
                plan["deadline"],
            )
        )
        target = periphery
        data = calldata(
            "transferFromMAToMA((uint128,(uint128,address,uint256),(uint8,bytes32,bytes32,uint256)))",
            ["(uint128,(uint128,address,uint256),(uint8,bytes32,bytes32,uint256))"],
            [
                (
                    plan["from_account"],
                    (plan["to_account"], token, int(plan["amount_units"])),
                    (signature.v, signature.r.to_bytes(32, "big"), signature.s.to_bytes(32, "big"), plan["deadline"]),
                )
            ],
        )
        metadata = {}
    return {"txData": {"to": target, "data": data}, "contractAddress": target, "metadata": metadata}


class AccountRelay:
    def __init__(self, config):
        if config.api_url not in CONTRACTS or config.chain_id != DEPLOYMENTS[config.api_url][0]:
            raise SetupError("Account setup requires a documented API deployment with its matching chain ID")
        self.config = config
        self.owner = address(config.owner_wallet_address)
        self.base_url = config.api_url.removesuffix("/v2")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, connect=5), headers={"User-Agent": USER_AGENT}
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def request(self, method: str, path: str, **kwargs):
        # One request, no redirects and no application retries, especially for POST.
        async with self.session.request(method, self.base_url + path, allow_redirects=False, **kwargs) as response:
            try:
                raw = await response.content.readexactly(1024 * 1024 + 1)
            except asyncio.IncompleteReadError as exc:
                raw = exc.partial
            if len(raw) > 1024 * 1024:
                raise SetupError("API response exceeded the account setup size limit")
            if region_blocked(response.status, raw):
                raise RegionUnavailableError()
            data = json.loads(raw, parse_float=Decimal)
            if (
                method == "GET"
                and path.endswith("/accounts")
                and response.status == 400
                and isinstance(data, dict)
                and data.get("error") == "NO_ACCOUNTS_FOUND_ERROR"
            ):
                return []
            if response.status != 200:
                raise SetupError(f"Account API returned HTTP {response.status}; no automatic retry")
            return data

    async def accounts(self) -> dict[int, str]:
        data = await self.request("GET", f"/v2/wallet/{self.owner}/accounts")
        if not isinstance(data, list):
            raise SetupError("Invalid owned-account response")
        # Every owned account, with its type as the API names it, unknown types included: ownership checks
        # see all of them, and decisions that depend on the type test it against the types above.
        result: dict[int, str] = {}
        for item in data:
            account_id = api_integer(item["accountId"])
            kind = item.get("type")
            if account_id == 0 or account_id in result or not isinstance(kind, str) or not kind:
                raise SetupError("Invalid owned-account entry")
            result[account_id] = kind
        return result

    async def nonce(self) -> int:
        data = await self.request("GET", f"/api/owner/{self.owner}/metadata")
        if not isinstance(data, dict) or address(data.get("address")) != self.owner:
            raise SetupError("Nonce response does not match the configured owner")
        return api_integer(data["coreSigNonce"])

    async def submit(self, payload: dict):
        return await self.request("POST", RELAY_PATH, json=payload)

    async def transfers(self, cursor: str | None):
        params = {"type": "TRANSFER", "limit": "100"}
        if cursor:
            params["cursor"] = cursor
        return await self.request("GET", f"/v2/wallet/{self.owner}/transfers", params=params)
