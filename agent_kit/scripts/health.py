#!/usr/bin/env python3
"""
Reya Agent Kit — Health check.

Verifies connectivity to the Reya API without requiring credentials.

All output is JSON to stdout. On failure the response leads with "error" per
the kit contract, plus diagnostic context (api_url, sdk_version), and exits 1.

Usage:
    python -m agent_kit.scripts.health
"""

import asyncio
import sys
import time
from importlib.metadata import PackageNotFoundError, version

from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.api_client import ApiClient
from sdk.open_api.configuration import Configuration

from agent_kit.scripts._common import JsonArgumentParser, exception_result, output_json
from agent_kit.scripts._env import get_api_url

# Resolve the installed SDK version from package metadata. The SDK is a git
# dependency here, so reading it from the dist metadata works whether the SDK is
# installed as a wheel or in editable mode.
try:
    SDK_VERSION = version("reya-python-sdk")
except PackageNotFoundError:  # pragma: no cover
    SDK_VERSION = "unknown"


async def check_health() -> None:
    api_url = get_api_url()
    api_client = ApiClient(Configuration(host=api_url))
    reference = ReferenceDataApi(api_client)

    try:
        start = time.time()
        # Count tradable perp market definitions — the prices feed also carries
        # spot price feeds, which inflated this number.
        market_defs = await reference.get_perp_market_definitions()
        latency_ms = round((time.time() - start) * 1000)

        output_json(
            {
                "status": "ok",
                "api_url": api_url,
                "sdk_version": SDK_VERSION,
                "markets_available": len(market_defs),
                "latency_ms": latency_ms,
            }
        )

    except Exception as e:
        output_json(
            {
                **exception_result(e),
                "status": "error",
                "api_url": api_url,
                "sdk_version": SDK_VERSION,
            }
        )
        sys.exit(1)

    finally:
        if hasattr(api_client, "rest_client") and api_client.rest_client:
            await api_client.rest_client.close()


def main() -> None:
    # No arguments — the parser exists so stray args honor the JSON error
    # contract (exit 1, {"error": ...}) instead of argparse usage text.
    JsonArgumentParser(prog="health", description="Reya Agent Kit — connectivity check").parse_args()
    try:
        asyncio.run(check_health())
    except Exception as e:
        # Failures outside check_health's own handler (e.g. client construction)
        # must still emit JSON, never a traceback.
        output_json({**exception_result(e), "status": "error"})
        sys.exit(1)


if __name__ == "__main__":
    main()
