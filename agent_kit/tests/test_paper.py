"""Paper invariants: exact accounting, failure atomicity, retries and live isolation."""

import copy
import sqlite3
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from agent_kit.scripts import paper
from agent_kit.scripts._paper import PaperStore, initialize, simulate, valuation
from agent_kit.scripts._v2 import V2Client

NOW = 100000
SYMBOL = "ETHRUSDPERP"


def inputs() -> tuple[dict, dict, list[dict]]:
    definition = {"symbol": SYMBOL, "qtyStepSize": ".1", "tickSize": "1", "minOrderQty": ".1"}
    book = {
        "symbol": SYMBOL,
        "type": "SNAPSHOT",
        "updatedAt": NOW,
        "bids": [{"px": "99", "qty": "2"}],
        "asks": [{"px": "101", "qty": "1"}, {"px": "102", "qty": "2"}],
    }
    summaries = [{"symbol": SYMBOL, "markPrice": "100", "oraclePrice": "100", "pricesUpdatedAt": NOW}]
    return definition, book, summaries


def request(**kwargs):
    return {
        "request_id": "one",
        "symbol": SYMBOL,
        "side": "buy",
        "kind": "ioc",
        "qty": "2",
        "price": "101",
        "slippage": ".01",
        "reduce_only": False,
        **kwargs,
    }


def state(collateral="1000", fee="10"):
    return initialize(collateral, fee, paper.APIS[0], NOW)


def test_partial_ioc_cancels_remainder_and_charges_taker_fee_once():
    before = state()
    after, result = simulate(before, request(), *inputs(), NOW)
    assert result["status"] == "partially_filled_cancelled"
    assert Decimal(result["filled_qty"]) == 1
    assert Decimal(result["cancelled_qty"]) == 1
    assert Decimal(result["fees"]) == Decimal(".1")  # 10 bps of 1 x mark 100, not the 101 fill
    assert Decimal(after["cash"]) == Decimal("999.9")
    assert before["positions"] == {}  # pure transition
    value = valuation(after, inputs()[2], NOW)
    assert Decimal(value["unrealized_pnl"]) == -1
    assert Decimal(value["equity"]) == Decimal("998.9")
    assert value["funding"] is None and value["liquidation_price"] is None


@pytest.mark.parametrize(
    "side, fill, mark, fee",
    [
        # devnet1 account 546 ETHRUSDPERP round trip, 11 Oct 2026, tier 0 = 3 bps; marks are the
        # on-chain MarketDataUpdatedV2 values used by settlement (txs 0xe2ac10cc..., 0x3483967f...).
        ("buy", "2530.371", "2528.001916424", "0.007584"),
        ("sell", "2525.673", "2527.981937425", "0.007583"),
    ],
)
def test_taker_fee_uses_mark_notional_with_settlement_rounding(side, fill, mark, fee):
    definition = {"symbol": SYMBOL, "qtyStepSize": ".001", "tickSize": ".001", "minOrderQty": ".001"}
    book = {
        "symbol": SYMBOL,
        "type": "SNAPSHOT",
        "updatedAt": NOW,
        "bids": [{"px": fill, "qty": "1"}],
        "asks": [{"px": fill, "qty": "1"}],
    }
    if side == "buy":
        book["bids"] = [{"px": "2500", "qty": "1"}]
    else:
        book["asks"] = [{"px": "2600", "qty": "1"}]
    summaries = [{"symbol": SYMBOL, "markPrice": mark, "oraclePrice": mark, "pricesUpdatedAt": NOW}]
    _, result = simulate(
        state(collateral="1000", fee="3"), request(side=side, qty=".01", price=fill), definition, book, summaries, NOW
    )
    assert result["fills"][0]["price"] == fill
    assert Decimal(result["fees"]) == Decimal(fee)
    assert result["fee_mark_price"] == mark


def test_close_and_flip_conserve_cost_basis_and_cash():
    s, _ = simulate(state(fee="0"), request(qty="1"), *inputs(), NOW)
    definition, book, summaries = inputs()
    book["bids"] = [{"px": "110", "qty": "2"}]
    book["asks"] = [{"px": "111", "qty": "2"}]
    summaries[0].update(markPrice="110", oraclePrice="110")
    s, result = simulate(
        s, request(request_id="flip", side="sell", qty="2", price="110"), definition, book, summaries, NOW
    )
    assert Decimal(result["realized_pnl"]) == 9
    assert s["positions"][SYMBOL] == {"qty": "-1", "basis": "-110"}
    book["bids"] = [{"px": "99", "qty": "2"}]
    book["asks"] = [{"px": "100", "qty": "2"}]
    s, result = simulate(
        s, request(request_id="close", qty="1", price="100", reduce_only=True), definition, book, summaries, NOW
    )
    assert s["positions"] == {}
    assert Decimal(s["realized_pnl"]) == 19
    assert Decimal(s["cash"]) == 1019


def test_fractional_close_does_not_lose_basis_dust():
    s = state(fee="0")
    d, book, summaries = inputs()
    book["asks"] = [{"px": "101", "qty": ".1"}, {"px": "102", "qty": ".2"}]
    s, _ = simulate(s, request(qty=".3", price="102"), d, book, summaries, NOW)
    for i in range(3):
        s, _ = simulate(
            s,
            request(request_id=f"close{i}", side="sell", qty=".1", price="99", reduce_only=True),
            d,
            book,
            summaries,
            NOW,
        )
    assert not s["positions"]
    assert Decimal(s["cash"]) == Decimal("999.2")


@pytest.mark.parametrize(
    "change",
    [
        {"qty": ".01"},
        {"qty": ".15"},
        {"qty": "-1"},
        {"qty": "NaN"},
        {"qty": "1e100"},
        {"qty": "0.0000000000000000001"},
        {"price": "101.5"},
        {"kind": "limit"},
        {"kind": "stop"},
        {"side": "other"},
        {"kind": "market", "slippage": ".1"},
        {"reduce_only": True},
    ],
)
def test_invalid_and_unsupported_commands_do_not_mutate(change):
    s = state()
    original = copy.deepcopy(s)
    with pytest.raises((ValueError, TypeError)):
        simulate(s, request(**change), *inputs(), NOW)
    assert s == original


@pytest.mark.parametrize("bad", ["stale_book", "future_book", "stale_mark", "crossed", "duplicate", "offgrid"])
def test_bad_market_inputs_fail_closed(bad):
    d, book, summaries = inputs()
    if bad == "stale_book":
        book["updatedAt"] = 0
    if bad == "future_book":
        book["updatedAt"] = NOW + 1
    if bad == "stale_mark":
        summaries[0]["pricesUpdatedAt"] = 0
    if bad == "crossed":
        book["bids"][0]["px"] = "102"
    if bad == "duplicate":
        book["asks"].append(book["asks"][0])
    if bad == "offgrid":
        book["asks"][0]["qty"] = ".15"
    with pytest.raises(ValueError):
        simulate(state(), request(), d, book, summaries, NOW)


def test_insufficient_collateral_rolls_back_and_zero_fill_is_recorded():
    s = state(collateral="100")
    with pytest.raises(ValueError, match="collateral"):
        simulate(s, request(), *inputs(), NOW)
    assert s["requests"] == {}
    s, result = simulate(s, request(price="98"), *inputs(), NOW)
    assert result["status"] == "cancelled"
    assert s["cash"] == "100.000000000000000000"


def test_market_uses_mark_slippage_bounded_ioc():
    definition, book, summaries = inputs()
    summaries[0]["oraclePrice"] = "90"  # the oracle index is not the execution reference
    s, result = simulate(state(), request(kind="market", qty="2", price=None), definition, book, summaries, NOW)
    assert Decimal(result["limit_price"]) == 101  # mark 100 × 1.01
    assert Decimal(result["filled_qty"]) == 1
    assert len(s["history"]) == 1


def test_reduce_only_cannot_flip_and_can_exit_after_losses():
    s, _ = simulate(state(), request(qty="1"), *inputs(), NOW)
    with pytest.raises(ValueError, match="Reduce-only"):
        simulate(s, request(request_id="bad", side="sell", reduce_only=True), *inputs(), NOW)
    s["cash"] = "-100"
    s, result = simulate(
        s, request(request_id="exit", side="sell", qty="1", price="99", reduce_only=True), *inputs(), NOW
    )
    assert not s["positions"] and result["status"] == "filled"


def test_store_restart_duplicate_and_failure_atomicity(tmp_path):
    directory = tmp_path / "paper"
    with PaperStore(directory) as store:
        store.save(state())
    with PaperStore(directory) as store:
        s, first = simulate(store.state, request(), *inputs(), NOW)
        store.save(s)
    with PaperStore(directory) as store:
        s, retry = simulate(store.state, request(), {}, {}, [], NOW + 999999)
        assert retry == {**first, "replayed": True}
        assert len(s["history"]) == 1
        with pytest.raises(ValueError, match="different parameters"):
            simulate(s, request(qty="1"), *inputs(), NOW)
    with pytest.raises(RuntimeError):
        with PaperStore(directory) as store:
            store.state["cash"] = "1"
            store.save(store.state)
            raise RuntimeError("crash before commit")
    with PaperStore(directory) as store:
        assert Decimal(store.state["cash"]) == Decimal("999.9")


def test_concurrent_writers_fail_without_partial_state(tmp_path):
    directory = tmp_path / "paper"
    with PaperStore(directory) as first:
        first.save(state())
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with PaperStore(directory):
                pytest.fail("second writer acquired the transaction")
    with PaperStore(directory) as store:
        assert store.state["cash"] == "1000"


def test_symlink_state_rejected(tmp_path):
    directory = tmp_path / "paper"
    directory.mkdir(mode=0o700)
    (directory / "paper.sqlite3").symlink_to(tmp_path / "foreign")
    with pytest.raises(OSError):
        with PaperStore(directory):
            pass


async def test_cli_credentials_ignored_and_only_public_gets_used(tmp_path, monkeypatch):
    monkeypatch.setenv("REYA_MODE", "live")
    monkeypatch.setenv("PERP_PRIVATE_KEY_1", "must-never-be-loaded")
    monkeypatch.setenv("PERP_PRIVATE_KEY_FILE", "/must/not/read")
    monkeypatch.setenv("REYA_API_URL", "https://malicious.invalid/v2")
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "paper")]
    await paper.run(parser.parse_args(base + ["init", "--collateral", "1000", "--fee-bps", "10"]))
    d, book, summaries = inputs()
    mock = AsyncMock(side_effect=[summaries, [d], book])
    monkeypatch.setattr(V2Client, "get", mock)
    args = parser.parse_args(base + ["order", "--symbol", SYMBOL, "--side", "buy", "--qty", "1", "--request-id", "one"])
    result = await paper.run(args)
    assert result["mode"] == "paper"
    assert [call.args[0] for call in mock.call_args_list] == [
        "/perpMarkets/summary",
        "/perpMarketDefinitions",
        f"/market/{SYMBOL}/depth",
    ]
    assert (await paper.run(args))["replayed"]
    assert mock.await_count == 3
    history = await paper.run(parser.parse_args(base + ["history"]))
    assert history["coverage"]["total_records"] == 1
    with pytest.raises(ValueError, match="ledger ID"):
        await paper.run(
            parser.parse_args(base + ["reset", "--collateral", "1", "--fee-bps", "0", "--confirm-ledger-id", "wrong"])
        )


def test_paper_module_has_no_live_submission_imports():
    import ast
    from pathlib import Path

    for name in ("paper.py", "_paper.py"):
        source = Path(paper.__file__).with_name(name).read_text()
        tree = ast.parse(source)
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert all(
            not (m or "").startswith(
                (
                    "sdk",
                    "web3",
                    "eth_",
                    "agent_kit.scripts.trade",
                    "agent_kit.scripts.account",
                    "agent_kit.scripts._writer",
                )
            )
            for m in modules
        )


async def test_refresh_series_restart_reset_and_history_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "ledger")]

    async def invoke(*args):
        return await paper.run(parser.parse_args(base + list(args)))

    init = await invoke("init", "--collateral", "100", "--fee-bps", "0")
    monkeypatch.setattr(V2Client, "get", AsyncMock(return_value=inputs()[2]))
    await invoke("state")
    assert (await invoke("history", "--snapshots"))["coverage"]["total_records"] == 0
    await invoke("refresh")
    await invoke("refresh")
    assert (await invoke("history", "--snapshots", "--limit", "1"))["coverage"]["next_offset"] == 1
    assert len((await invoke("history", "--snapshots", "--offset", "1"))["data"]) == 1
    reset = await invoke("reset", "--confirm-ledger-id", init["ledger_id"], "--collateral", "50", "--fee-bps", "1")
    assert reset["ledger_id"] != init["ledger_id"]
    assert (await invoke("history", "--snapshots"))["coverage"]["total_records"] == 0
    assert reset["cash"] == "50"


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("quantity", [".1", "1", "1.9"])
def test_roundtrip_spread_loss_and_fee_conservation(side, quantity):
    d, book, summaries = inputs()
    buy = side == "buy"
    s, first = simulate(
        state(), request(side=side, qty=quantity, price="102" if buy else "99"), d, book, summaries, NOW
    )
    s, second = simulate(
        s,
        request(
            request_id="close",
            side="sell" if buy else "buy",
            qty=quantity,
            price="99" if buy else "102",
            reduce_only=True,
        ),
        d,
        book,
        summaries,
        NOW,
    )
    paid = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in (first if buy else second)["fills"])
    received = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in (second if buy else first)["fills"])
    fees = Decimal(first["fees"]) + Decimal(second["fees"])
    assert not s["positions"]
    assert Decimal(s["cash"]) == Decimal("1000") + received - paid - fees
    assert Decimal(s["realized_pnl"]) == received - paid


@pytest.mark.parametrize("kind, change", [("market", {"price": "100"}), ("ioc", {"price": None})])
def test_incompatible_or_missing_order_price_rejected(kind, change):
    with pytest.raises(ValueError):
        simulate(state(), request(kind=kind, **change), *inputs(), NOW)


async def test_new_symbol_missing_summary_reports_symbol_and_preserves_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper.time, "time", lambda: NOW / 1000)
    parser = paper.build_parser()
    base = ["--directory", str(tmp_path / "ledger")]
    await paper.run(parser.parse_args(base + ["init", "--collateral", "1000", "--fee-bps", "10"]))
    d, book, _ = inputs()
    monkeypatch.setattr(V2Client, "get", AsyncMock(side_effect=[[], [d], book]))
    args = parser.parse_args(
        base + ["order", "--symbol", SYMBOL, "--side", "buy", "--qty", "1", "--request-id", "missing"]
    )
    with pytest.raises(ValueError, match=f"Missing market summary for {SYMBOL}"):
        await paper.run(args)
    with PaperStore(tmp_path / "ledger") as store:
        assert store.state["cash"] == "1000"
        assert not store.state["positions"]
        assert not store.state["requests"]
        assert not store.state["history"]
