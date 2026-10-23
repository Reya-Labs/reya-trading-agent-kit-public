#!/usr/bin/env python3
"""
Reya Agent Kit — Read commands.

All output is JSON to stdout. Errors are returned as {"error": "..."}.

Usage:
    python -m agent_kit.scripts.query prices [--symbol BTCRUSDPERP]
    python -m agent_kit.scripts.query positions
    python -m agent_kit.scripts.query orders
    python -m agent_kit.scripts.query balances
    python -m agent_kit.scripts.query margin
    python -m agent_kit.scripts.query market info [--symbol BTCRUSDPERP]
    python -m agent_kit.scripts.query market funding [--symbol BTCRUSDPERP]
"""

import argparse
import asyncio
import os
from decimal import Decimal

from sdk.open_api.api.market_data_api import MarketDataApi
from sdk.open_api.api.reference_data_api import ReferenceDataApi
from sdk.open_api.api.wallet_data_api import WalletDataApi
from sdk.open_api.api_client import ApiClient
from sdk.open_api.configuration import Configuration

from agent_kit.scripts._account_data import AccountDataClient
from agent_kit.scripts._common import (
    JsonArgumentParser,
    exception_result,
    market_categories,
    output_error,
    output_json,
    output_result,
    require_owned_account,
    require_price,
    symbol_arg,
    underlying_hours_warning,
)
from agent_kit.scripts._env import get_api_url, get_owner_address, get_trading_config, load_env
from agent_kit.scripts._executions import account_executions, execution_page
from agent_kit.scripts._margin import MARGIN_ASSUMPTIONS, account_figures, load_snapshot, position_risk
from agent_kit.scripts._orders import order_result


def _get_api_url() -> str:
    """Resolve the API URL from env or default to mainnet."""
    return get_api_url()


async def _require_known_symbol(client, symbol: str) -> None:
    """Validate a --symbol filter against market definitions.

    Without this, a typo'd symbol filters everything out and returns [] —
    which an agent reads as "no positions/orders" rather than "no such market".
    Only called when a filter is given, so the unfiltered path stays one call.
    """
    market_defs = await client.reference.get_perp_market_definitions()
    if not any(md.symbol == symbol for md in market_defs):
        raise ValueError(f"Unknown symbol '{symbol}'. Run: query.py prices to see available symbols.")


async def cmd_prices(args: argparse.Namespace) -> None:
    """Fetch oracle and mark prices. Optionally filter by --symbol."""
    api_url = _get_api_url()
    api_client = ApiClient(Configuration(host=api_url))
    markets = MarketDataApi(api_client)

    try:
        summaries = (
            [await markets.get_perp_market_summary(symbol=args.symbol)]
            if args.symbol
            else await markets.get_perp_markets_summary()
        )
        results = []
        for summary in summaries:
            entry = {
                "symbol": summary.symbol,
                "oracle_price_usd": (
                    float(require_price(summary.oracle_price, f"{summary.symbol} oracle price"))
                    if summary.oracle_price is not None
                    else None
                ),
                "mark_price_usd": (
                    float(require_price(summary.mark_price, f"{summary.symbol} mark price"))
                    if summary.mark_price is not None
                    else None
                ),
                "updated_at": summary.prices_updated_at,
            }
            results.append(entry)
        output_json(results[0] if args.symbol else results)
    finally:
        if hasattr(api_client, "rest_client") and api_client.rest_client:
            await api_client.rest_client.close()


FEE_BASE_NOTE = "Taker fees are charged on quantity x the on-chain mark price at settlement, not on the fill price."


def _bps(rate: str | None) -> str | None:
    return None if rate is None else str((Decimal(rate) * 10000).normalize())


async def cmd_fees(args: argparse.Namespace) -> None:
    """Fee tiers and global discounts; with an owner configured, also that wallet's tier. No key needed."""
    load_env()
    # Only the owner is needed; fee reads must not require trading settings such as REYA_DEX_ID.
    owner = get_owner_address() if os.environ.get("PERP_WALLET_ADDRESS_1") else None
    api_client = ApiClient(Configuration(host=_get_api_url()))
    try:
        reference = ReferenceDataApi(api_client)
        tiers = await reference.get_fee_tier_parameters()
        discounts = await reference.get_global_fee_parameters()
        result: dict = {
            "fee_tiers": [
                {
                    "tier_id": t.tier_id,
                    "tier_type": getattr(t.tier_type, "value", t.tier_type),
                    "taker_fee": t.taker_fee,
                    "maker_fee": t.maker_fee,
                    "taker_fee_bps": _bps(t.taker_fee),
                    "maker_fee_bps": _bps(t.maker_fee),
                    "volume30d": t.volume30d,
                }
                for t in tiers
            ],
            "discounts": {
                "og_discount": discounts.og_discount,
                "referee_discount": discounts.referee_discount,
                "referrer_rebate": discounts.referrer_rebate,
                "affiliate_referrer_rebate": discounts.affiliate_referrer_rebate,
            },
            "fee_base": FEE_BASE_NOTE,
            "wallet": None,
        }
        if owner:
            config = await WalletDataApi(api_client).get_wallet_configuration(address=owner)
            tier = next((t for t in tiers if t.tier_id == config.fee_tier_id), None)
            result["wallet"] = {
                "address": owner,
                "fee_tier_id": config.fee_tier_id,
                "taker_fee": tier.taker_fee if tier else None,
                "maker_fee": tier.maker_fee if tier else None,
                "taker_fee_bps": _bps(tier.taker_fee) if tier else None,
                "maker_fee_bps": _bps(tier.maker_fee) if tier else None,
                "og_status": config.og_status,
                "affiliate_status": config.affiliate_status,
                "referee_status": config.referee_status,
                "note": "OG/referee discounts may lower the effective rate; how they combine is not verified. "
                "Settled execution fees are authoritative.",
            }
        output_json(result)
    finally:
        if hasattr(api_client, "rest_client") and api_client.rest_client:
            await api_client.rest_client.close()


def _estimated_unrealized_pnl(pos, side: str, market) -> dict:
    """qty x (mark - entry), signed by side, at the current V2 mark. An estimate, not settlement.

    A missing or nonpositive mark leaves the fields null rather than zero.
    """
    mark = getattr(market, "mark_price", None) if market is not None else None
    result = {
        "mark_price": mark,
        "mark_updated_at": getattr(market, "prices_updated_at", None) if market is not None else None,
        "estimated_unrealized_pnl_usd": None,
    }
    if mark is None:
        return result
    try:
        price, entry, qty = Decimal(mark), Decimal(pos.avg_entry_price), Decimal(pos.qty)
    except (TypeError, ValueError, ArithmeticError):
        return result
    if price.is_finite() and price > 0 and entry.is_finite() and qty.is_finite():
        result["estimated_unrealized_pnl_usd"] = str(qty * (price - entry) * (1 if side == "long" else -1))
    return result


async def _definitions_by_symbol(client) -> dict:
    """Market definitions by symbol for the optional hours warning; a failed read leaves the warning out."""
    try:
        return {md.symbol: md for md in await client.reference.get_perp_market_definitions()}
    except Exception:  # The warning is optional; positions remain authoritative.
        return {}


def _hours_warning(symbol: str, definitions: dict) -> dict:
    warning = underlying_hours_warning(symbol, market_categories(definitions.get(symbol)))
    return {"market_hours_warning": warning} if warning else {}


async def cmd_positions(args: argparse.Namespace) -> None:
    """Fetch open positions for the configured perp account. Requires an owner address and account ID."""
    load_env()

    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        account_id = await require_owned_account(client)
        if args.symbol:
            await _require_known_symbol(client, args.symbol)
        positions = await client.get_positions()
        owned = [pos for pos in positions if pos.account_id == account_id]
        marks: dict = {}
        marks_error = None
        if owned:
            try:
                marks = {m.symbol: m for m in await client.markets.get_perp_markets_summary()}
            except Exception as exc:  # The estimate is optional; positions remain authoritative.
                marks_error = exception_result(exc)["error"]

        # One read serves the hours warning and the margin figures; a failed read leaves the warning out.
        definitions = await _definitions_by_symbol(client) if owned else {}
        snapshot, margin_error, settled_lm, usage = None, None, None, None
        if owned and not marks_error:
            try:
                snapshot = await load_snapshot(
                    client, account_id, summaries=marks, definitions=list(definitions.values()) or None
                )
                settled_lm = snapshot.settled().lm
                usage = account_figures(snapshot)["margin_usage"]
            except Exception as exc:  # Liquidation figures are optional; positions remain authoritative.
                margin_error = exception_result(exc)["error"]

        results = []
        for pos in owned:
            side_str = "long" if pos.side.value == "B" else "short"
            entry = {
                "symbol": pos.symbol,
                "side": side_str,
                "qty": pos.qty,
                "avg_entry_price": pos.avg_entry_price,
                **_estimated_unrealized_pnl(pos, side_str, marks.get(pos.symbol)),
                "liquidation_price_usd": None,
                "distance_to_liquidation_pct": None,
                "account_margin_usage": usage,
                **_hours_warning(pos.symbol, snapshot.definitions if snapshot is not None else definitions),
            }
            if snapshot is not None and pos.symbol in snapshot.margin.markets:
                risk = position_risk(snapshot, pos.symbol, settled_lm)
                entry["liquidation_price_usd"] = risk["liquidation_price_usd"]
                entry["distance_to_liquidation_pct"] = risk["distance_to_liquidation_pct"]
            if marks_error:
                entry["mark_unavailable_reason"] = marks_error
            elif margin_error:
                entry["margin_unavailable_reason"] = margin_error
            results.append(entry)

        if args.symbol:
            results = [r for r in results if r["symbol"] == args.symbol]

        output_json(results)


async def cmd_orders(args: argparse.Namespace) -> None:
    """Fetch open orders for the configured perp account. Requires an owner address and account ID."""
    load_env()

    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        account_id = await require_owned_account(client)
        if args.symbol:
            await _require_known_symbol(client, args.symbol)
        orders = await client.get_open_orders()

        results = []
        for order in orders:
            if order.account_id != account_id:
                continue
            results.append(order_result(order))

        if args.symbol:
            results = [r for r in results if r["symbol"] == args.symbol]

        output_json(results)


async def cmd_accounts(args: argparse.Namespace) -> None:
    """Discover owner accounts without a signer or configured account ID."""
    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        output_json(
            [{"account_id": a.account_id, "name": a.name, "type": a.type.value} for a in await client.get_accounts()]
        )


async def cmd_executions(args: argparse.Namespace) -> None:
    """Return one bounded page of settled fills for the configured account."""
    if args.start_time is not None and args.end_time is not None and args.start_time > args.end_time:
        raise ValueError("--start_time must be no later than --end_time")
    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        account_id = await require_owned_account(client)
        if args.symbol:
            await _require_known_symbol(client, args.symbol)
        page = await client.wallet.get_wallet_perp_executions(
            address=client.config.owner_wallet_address,
            start_time=args.start_time,
            end_time=args.end_time,
        )
        rows = account_executions(page, account_id, client.config.dex_id_override)
        rows = [
            r
            for r in rows
            if (not args.symbol or r["symbol"] == args.symbol) and (not args.order_id or r["order_id"] == args.order_id)
        ]
        output_json({"account_id": account_id, "executions": rows, "source_page": execution_page(page)})


async def cmd_balances(args: argparse.Namespace) -> None:
    """Fetch balances for the configured perp account. Requires an owner address and account ID."""
    load_env()

    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        account_id = await require_owned_account(client)
        balances = await client.get_account_balances()

        results = [
            {
                "account_id": bal.account_id,
                "asset": bal.asset,
                "real_balance": bal.real_balance,
            }
            for bal in balances
            if bal.account_id == account_id
        ]

        output_json(results)


async def cmd_margin(args: argparse.Namespace) -> None:
    """Equity, margin requirements, usage and liquidation prices for the configured perp account.

    Mirrors the matching engine's margin model (docs/margin.md): collateral after haircuts plus
    unrealized PnL at mark and an estimate of unsettled funding, against the liquidation margin
    (the chain liquidates below it) and the initial margin (new exposure needs it). Fails fast on
    missing asset, market or price data, since treating it as zero produces wrong risk numbers.
    """
    load_env()

    async with AccountDataClient(config=get_trading_config(include_private_key=False)) as client:
        account_id = await require_owned_account(client)
        snapshot = await load_snapshot(client, account_id)
        settled = snapshot.settled()
        positions = []
        for detail in snapshot.positions:
            symbol, qty, mark = detail["symbol"], detail["qty"], detail["mark"]
            risk = position_risk(snapshot, symbol, settled.lm)
            positions.append(
                {
                    "symbol": symbol,
                    "side": "long" if qty > 0 else "short",
                    "qty": detail["qty_text"],
                    "mark_price_usd": float(mark),
                    "notional_usd": round(float(abs(qty) * mark), 4),
                    "avg_entry_price": format(snapshot.margin.markets[symbol].entry.normalize(), "f"),
                    "unrealized_pnl_usd": float(detail["unrealized_pnl"]),
                    "unsettled_funding_estimate_usd": float(detail["funding_estimate"]),
                    "liq_margin_param": float(snapshot.margin.markets[symbol].margin_rate),
                    "liq_margin_req_usd": risk["liquidation_margin_req_usd"],
                    "liquidation_price_usd": risk["liquidation_price_usd"],
                    "distance_to_liquidation_pct": risk["distance_to_liquidation_pct"],
                    **_hours_warning(symbol, snapshot.definitions),
                }
            )
        output_json(
            {
                "account_id": account_id,
                **account_figures(snapshot),
                "collateral_value_usd": round(sum(row["value_usd"] for row in snapshot.collateral), 4),
                "effective_collateral_usd": round(sum(row["margin_credit_usd"] for row in snapshot.collateral), 4),
                "collateral_breakdown": [
                    {
                        "asset": row["asset"],
                        "balance": row["balance"],
                        "price_usd": row["price_usd"],
                        "haircut": row["haircut"],
                        "value_usd": round(row["value_usd"], 4),
                        "effective_value_usd": round(row["margin_credit_usd"], 4),
                        **({"engine_credit_usd": row["engine_credit_usd"]} if "engine_credit_usd" in row else {}),
                        **({"credit_note": row["credit_note"]} if "credit_note" in row else {}),
                    }
                    for row in snapshot.collateral
                ],
                "positions": positions,
                "assumptions": MARGIN_ASSUMPTIONS,
            }
        )


def _market_def_to_dict(md) -> dict:
    """Shape a MarketDefinition for JSON output."""
    return {
        "symbol": md.symbol,
        "market_id": md.market_id,
        "min_order_qty": md.min_order_qty,
        "qty_step_size": md.qty_step_size,
        "tick_size": md.tick_size,
        "max_leverage": md.max_leverage,
        "initial_margin_parameter": md.initial_margin_parameter,
        "liquidation_margin_parameter": md.liquidation_margin_parameter,
        "oi_cap": md.oi_cap,
        # As V2 reports it; null when the API (or this SDK) does not, never inferred.
        "categories": market_categories(md),
    }


async def cmd_market_info(args: argparse.Namespace) -> None:
    """Get structural market params (steps, ticks, margin params, leverage, OI cap). No auth needed."""
    api_url = _get_api_url()
    api_client = ApiClient(Configuration(host=api_url))
    reference = ReferenceDataApi(api_client)

    try:
        market_defs = await reference.get_perp_market_definitions()
        if args.symbol:
            md = next((m for m in market_defs if m.symbol == args.symbol), None)
            if md is None:
                raise ValueError(f"Symbol '{args.symbol}' not found in market definitions.")
            output_json(_market_def_to_dict(md))
        else:
            output_json([_market_def_to_dict(m) for m in market_defs])
    finally:
        if hasattr(api_client, "rest_client") and api_client.rest_client:
            await api_client.rest_client.close()


def _market_summary_to_funding_dict(ms) -> dict:
    """Shape a MarketSummary entry into a funding-focused dict."""
    return {
        "symbol": ms.symbol,
        "funding_rate": ms.funding_rate,
        "long_funding_value": ms.long_funding_value,
        "short_funding_value": ms.short_funding_value,
        "oi_qty": ms.oi_qty,
        "updated_at": ms.updated_at,
    }


async def cmd_market_funding(args: argparse.Namespace) -> None:
    """Get funding rate and funding accrual values per market. No auth needed."""
    api_url = _get_api_url()
    api_client = ApiClient(Configuration(host=api_url))
    markets = MarketDataApi(api_client)

    try:
        if args.symbol:
            summary = await markets.get_perp_market_summary(symbol=args.symbol)
            output_json(_market_summary_to_funding_dict(summary))
        else:
            summaries = await markets.get_perp_markets_summary()
            output_json([_market_summary_to_funding_dict(s) for s in summaries])
    finally:
        if hasattr(api_client, "rest_client") and api_client.rest_client:
            await api_client.rest_client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="query",
        description="Reya Agent Kit — read commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # prices
    prices_parser = subparsers.add_parser("prices", help="Get oracle and mark prices (no credentials needed)")
    prices_parser.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Trading symbol (e.g., BTCRUSDPERP, ETHRUSDPERP). Omit to get all prices.",
    )

    # positions
    positions_parser = subparsers.add_parser("positions", help="Get open positions (requires owner address/account ID)")
    positions_parser.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Filter by symbol (e.g., ETHRUSDPERP)",
    )

    # orders
    orders_parser = subparsers.add_parser("orders", help="Get open orders (requires owner address/account ID)")
    orders_parser.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Filter by symbol (e.g., ETHRUSDPERP)",
    )

    subparsers.add_parser("accounts", help="List owner accounts (no private key or account ID needed)")
    subparsers.add_parser("fees", help="Fee tiers, discounts and, with an owner configured, its tier (no key needed)")
    executions_parser = subparsers.add_parser("executions", help="Settled fills and fees (one bounded API page)")
    executions_parser.add_argument("--symbol", type=symbol_arg)
    executions_parser.add_argument("--order_id")
    executions_parser.add_argument("--start_time", type=int, help="Inclusive start timestamp in milliseconds")
    executions_parser.add_argument("--end_time", type=int, help="Inclusive end timestamp in milliseconds")

    # balances
    subparsers.add_parser("balances", help="Get account balances (requires owner address/account ID)")

    # margin
    subparsers.add_parser(
        "margin",
        help="Get effective margin with haircuts and liquidation requirement (requires owner address/account ID)",
    )

    # market group — structural and funding metadata (no credentials needed)
    market_parser = subparsers.add_parser("market", help="Market metadata commands")
    market_subparsers = market_parser.add_subparsers(dest="market_action", required=True)

    market_info_parser = market_subparsers.add_parser(
        "info", help="Structural params: steps, ticks, max leverage, margin params, OI cap"
    )
    market_info_parser.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Trading symbol (e.g., ETHRUSDPERP). Omit for all markets.",
    )

    market_funding_parser = market_subparsers.add_parser(
        "funding", help="Funding rate, open interest, long/short funding values"
    )
    market_funding_parser.add_argument(
        "--symbol",
        type=symbol_arg,
        default=None,
        help="Trading symbol (e.g., ETHRUSDPERP). Omit for all markets.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "prices":
            asyncio.run(cmd_prices(args))
        elif args.command == "positions":
            asyncio.run(cmd_positions(args))
        elif args.command == "orders":
            asyncio.run(cmd_orders(args))
        elif args.command == "accounts":
            asyncio.run(cmd_accounts(args))
        elif args.command == "fees":
            asyncio.run(cmd_fees(args))
        elif args.command == "executions":
            asyncio.run(cmd_executions(args))
        elif args.command == "balances":
            asyncio.run(cmd_balances(args))
        elif args.command == "margin":
            asyncio.run(cmd_margin(args))
        elif args.command == "market":
            if args.market_action == "info":
                asyncio.run(cmd_market_info(args))
            elif args.market_action == "funding":
                asyncio.run(cmd_market_funding(args))
            else:
                output_error(f"Unknown market action: {args.market_action}")
        else:
            output_error(f"Unknown command: {args.command}")
    except Exception as e:
        output_result(exception_result(e))


if __name__ == "__main__":
    main()
