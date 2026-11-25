"""Bounded V2 snapshot/diff state. A connection alone never means fresh data."""

from typing import Any

import time

from agent_kit.scripts._v2 import decimal, integer, rows

EVENT_FEEDS = ("perpExecutions", "executionBusts", "transfers")
TERMINAL_ORDER_STATUSES = ("FILLED", "CANCELLED")


class FeedState:
    def __init__(self, api_url: str, channel: str, *, capacity=10000):
        if not channel.startswith("/v2/"):
            raise ValueError("Only V2 channels are supported")
        self.api_url, self.channel = api_url, channel
        self.kind = channel.rsplit("/", 1)[-1]
        self.capacity = capacity
        self.reset()

    def reset(self, *, keep_order_continuity=False):
        """Clear the view. observe() keeps orderChanges continuity across its per-attempt
        reset; an explicit reset (e.g. a wallet switch) forgets it."""
        continuity = self._order_continuity() if keep_order_continuity else None
        self.records: dict[tuple, dict] = {}
        self.unidentified: list[dict] = []
        self.versions: dict[tuple, tuple] = {}
        self.book: dict[str, dict] = {"bids": {}, "asks": {}}
        self.phase = "recovering"
        self.received_at: float | None = None
        self.last_timestamp = -1
        self.snapshot_sequence = -1
        self.depth_updated_at: int | None = None
        self.coverage: dict = {"state": "not_requested"}
        self.snapshot_keys: set = set()
        self.resume_after: int | None = None
        # Highest orderChanges sequence through which this view is gap-free.
        self.covered_through: int | None = None
        self._continuity = continuity

    def _order_continuity(self):
        """Terminal orders already observed, and the sequence the view was gap-free through.

        Merges any continuity still pending from an earlier attempt, so consecutive failed
        reconnects (or a resubscription whose backfill failed) keep the older resume point."""
        if self.kind != "orderChanges":
            return None
        pending = self._continuity
        carried = dict(pending[0]) if pending else {}
        carried.update(
            {
                key: (self.versions[key], row)
                for key, row in self.records.items()
                if row.get("status") in TERMINAL_ORDER_STATUSES
            }
        )
        resume = self.covered_through if self.covered_through is not None else (pending[1] if pending else None)
        return (carried, resume) if resume is not None else None

    def snapshot(self, contents, *, now=None):
        continuity = self._order_continuity()
        self.reset()
        if self.kind == "depth":
            self._depth(contents, snapshot=True)
        else:
            data = contents.get("data") if isinstance(contents, dict) else contents
            if self.kind == "orderChanges":
                self.snapshot_sequence = integer(contents.get("snapshotSequenceNumber"), "snapshotSequenceNumber")
            if self.kind == "summary" and isinstance(contents, dict) and "symbol" in contents:
                data = [contents]
            for row in rows(data):
                self._row(row, timestamp=-1, snapshot=True)
            if self.kind == "orderChanges":
                self.snapshot_keys = set(self.records)
                if continuity:
                    # The gap after resume_after stays open until backfill() applies history.
                    self.resume_after = self.covered_through = continuity[1]
                    self._continuity = continuity
                    for key, (rank, row) in continuity[0].items():
                        if key not in self.records:
                            self.versions[key], self.records[key] = rank, row
                else:
                    self.covered_through = self.snapshot_sequence
        self.phase = "ready"
        self.received_at = time.monotonic() if now is None else now

    def update(self, message: dict, *, now=None):
        if message.get("channel") != self.channel:
            raise ValueError("Update belongs to a different wallet/environment/channel")
        if self.phase not in ("ready", "stale"):
            raise ValueError("A fresh snapshot is required before applying updates")
        timestamp = integer(message.get("timestamp"), "timestamp")
        if self.kind == "depth":
            # There is no depth sequence/cursor. Never guess how to reorder a diff.
            if timestamp < self.last_timestamp:
                raise ValueError("Out-of-order depth diff requires resubscription")
            self._depth(message.get("data"), snapshot=False)
        else:
            data = message.get("data")
            if self.kind == "summary" and isinstance(data, dict):
                data = [data]
            for row in rows(data):
                self._row(row, timestamp=timestamp)
                if (
                    self.kind == "orderChanges"
                    and self.covered_through is not None
                    and self.covered_through >= self.snapshot_sequence
                ):
                    self.covered_through = max(self.covered_through, integer(row.get("sequenceNumber")))
        self.last_timestamp = max(self.last_timestamp, timestamp)
        self.received_at = time.monotonic() if now is None else now
        self.phase = "ready"

    def backfill(self, result):
        if self.kind not in (*EVENT_FEEDS, "orderChanges"):
            return
        for row in rows(result.get("data")):
            if self.kind == "orderChanges" and integer(row.get("sequenceNumber")) <= self.snapshot_sequence:
                # The open-order snapshot supersedes older history for CURRENT state, but a
                # resubscription must not drop terminal changes made while disconnected.
                self._recover_missed_order(row)
                continue
            self._row(row, timestamp=-1)
        self.coverage = result["coverage"]
        if self.kind == "orderChanges":
            # History up to the snapshot has been applied: the resubscription gap is closed.
            self.covered_through = max(self.covered_through or -1, self.snapshot_sequence)
            self._continuity = None

    def _recover_missed_order(self, row):
        sequence = integer(row.get("sequenceNumber"))
        if self.resume_after is None or sequence <= self.resume_after:
            return
        if row.get("status") not in TERMINAL_ORDER_STATUSES:
            return
        key = (integer(row.get("accountId"), "accountId"), row["symbol"], row["orderId"])
        if key in self.snapshot_keys:
            return
        for field in ("qty", "cumQty", "limitPx", "avgFillPx"):
            if row.get(field) is not None:
                decimal(row[field], field)
        if self.versions.get(key, (-1,)) < (sequence,):
            self.versions[key], self.records[key] = (sequence,), row
        if len(self.versions) + len(self.unidentified) > self.capacity:
            raise ValueError("Feed capacity exceeded; restart with a new bounded observation window")

    def _row(self, row, timestamp, snapshot=False):
        key: tuple
        rank: tuple
        for field in (
            "qty",
            "price",
            "amount",
            "realBalance",
            "balanceDEPRECATED",
            "netDepositsAfter",
            "avgEntryPrice",
            "avgEntryFundingValue",
            "limitPx",
            "triggerPx",
            "cumQty",
            "execQty",
            "avgFillPx",
            "takerFee",
            "makerFee",
        ):
            if row.get(field) is not None:
                decimal(row[field], field)
        if self.kind in EVENT_FEEDS:
            key = (integer(row.get("sequenceNumber")),)
            if self.kind == "perpExecutions" and key == (0,):
                # Legacy zero is absence of an event ID, not a deduplication key.
                # Preserve each observation; even equal rows can be different trades.
                self.unidentified.append(row)
                if len(self.unidentified) + len(self.versions) > self.capacity:
                    raise ValueError("Feed capacity exceeded; restart with a new bounded observation window")
                return
            if key in self.records and self.records[key] != row:
                raise ValueError("Conflicting immutable feed record")
            rank = (key[0],)
            for field in ("qty", "price", "amount", "netDepositsAfter"):
                if field in row:
                    decimal(row[field], field)
        elif self.kind == "orderChanges":
            key = (integer(row.get("accountId"), "accountId"), row["symbol"], row["orderId"])
            rank = (self.snapshot_sequence if snapshot else integer(row.get("sequenceNumber")),)
            if not snapshot and rank[0] <= self.snapshot_sequence:
                return
        elif self.kind == "positions":
            key = (
                integer(row.get("accountId"), "accountId"),
                integer(row.get("exchangeId"), "exchangeId"),
                row["symbol"],
            )
            quantity = decimal(row.get("qty"), "qty")
            if quantity < 0 or row.get("side") not in ("B", "A"):
                raise ValueError("Invalid position")
            decimal(row.get("avgEntryPrice"), "avgEntryPrice")
            # Funding/ADL-only updates may keep lastTradeSequenceNumber unchanged.
            rank = (integer(row.get("lastTradeSequenceNumber")), timestamp)
        elif self.kind == "accountBalances":
            key = (integer(row.get("accountId"), "accountId"), row["asset"])
            decimal(row.get("realBalance"), "realBalance")
            rank = (timestamp,)
        elif self.kind == "accounts":
            account_id = row.get("accountId")
            if isinstance(account_id, str) and account_id.isascii() and account_id.isdecimal():
                account_id = int(account_id)
            key = (integer(account_id, "accountId"),)
            rank = (timestamp,)
            if "owner" in row and row["owner"].lower() != self.channel.split("/")[3].lower():
                if row.get("removed") is not True:
                    raise ValueError("Account event owner differs from channel owner")
        else:
            key = (row.get("symbol") or row["asset"],)
            rank = (integer(row.get("updatedAt"), "updatedAt"),)
            for field in ("oraclePrice", "markPrice", "fundingRate", "longFundingValue", "shortFundingValue"):
                if row.get(field) is not None:
                    decimal(row[field], field)
        previous = self.versions.get(key)
        if previous is not None and rank < previous:
            return
        if previous == rank and self.records.get(key) == row:
            return
        self.versions[key] = rank
        if (self.kind == "positions" and decimal(row["qty"], "qty") == 0) or row.get("removed") is True:
            self.records.pop(key, None)
        else:
            self.records[key] = row
        if len(self.versions) + len(self.unidentified) > self.capacity:
            raise ValueError("Feed capacity exceeded; restart with a new bounded observation window")

    def _depth(self, data, *, snapshot):
        expected = "SNAPSHOT" if snapshot else "UPDATE"
        if (
            not isinstance(data, dict)
            or data.get("type") != expected
            or data.get("symbol") != self.channel.split("/")[3]
        ):
            raise ValueError("Invalid depth discriminator or symbol")
        integer(data.get("updatedAt"), "updatedAt")
        for side in ("bids", "asks"):
            levels = rows(data.get(side))
            if len(levels) > (100 if snapshot else 200):
                raise ValueError("Depth frame exceeds bounded top-100 contract")
            for row in levels:
                price = decimal(row.get("px"), "depth px")
                quantity = decimal(row.get("qty"), "depth qty")
                if quantity < 0:
                    raise ValueError("Negative depth quantity")
                if quantity == 0:
                    self.book[side].pop(price, None)
                else:
                    self.book[side][price] = row
            if len(self.book[side]) > 100:
                raise ValueError("Depth view exceeds 100 levels; a fresh snapshot is required")
        self.depth_updated_at = data["updatedAt"]

    def view(self, *, now=None, max_age=30):
        now = time.monotonic() if now is None else now
        age = None if self.received_at is None else max(0, now - self.received_at)
        stale = age is not None and age > max_age
        phase = self.phase
        # Missing history outranks quiet data: a quiet feed must not hide a coverage gap.
        if phase == "ready" and (self.coverage.get("state") == "incomplete" or self.unidentified):
            phase = "incomplete"
        elif phase == "ready" and stale:
            phase = "stale"
        data: Any
        if self.kind == "depth":
            data = {
                side: [self.book[side][key] for key in sorted(self.book[side], reverse=side == "bids")]
                for side in ("bids", "asks")
            }
            data["updatedAt"] = getattr(self, "depth_updated_at", None)
        else:
            data = list(self.records.values()) + self.unidentified
        return {
            "api_url": self.api_url,
            "channel": self.channel,
            "state": phase,
            "stale": stale,
            "data_age_seconds": age,
            "data": data,
            "history_coverage": self.coverage,
            "unidentified_observations": len(self.unidentified),
            "identity_note": "Legacy zero-sequence executions are observations, not deduplicated events; no verified sums.",
            **(
                {
                    "order_scope": "Open orders at the first snapshot, later changes, and terminal orders "
                    "recovered across resubscriptions. Orders finished before this observation started are "
                    "excluded; use data history or reconcile-orders."
                }
                if self.kind == "orderChanges"
                else {}
            ),
            "safe_for_order_admission": False,
        }
