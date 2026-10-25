"""Trigger rules for `watch trigger`: a crossing on the WebSocket mark, a funding average, and freshness.

Pure logic: no I/O. watch.py feeds every summary push (`/v2/perpMarket/{symbol}/summary`) to
`Trigger.on_push` and calls `Trigger.on_idle` while no push arrives. The rules mirror the hosted
workflows' crossing rule, so a level means the same thing locally as on Reya's service:

- A crossing needs the previous fresh mark on the untriggered side and a new fresh mark at or past
  the level (touching counts). A level already reached at the first push is refused, never fired.
- A push is fresh while pushes keep coming (no gap over PUSH_GAP_S, by this computer's clock and by
  the server's `pricesUpdatedAt`), arrive at live pace and on time, and the prices keep updating
  (`pricesUpdatedAt` moved within PRICE_STALL_S). Pace and timing use the row's `updatedAt`, which
  the server stamps on every push. Live pace, until the delivery baseline is established: a push is
  part of a burst when it arrived bunched with the one before it and, over the last PACE_PUSHES
  pushes, receipts advanced less than PACE of the stamps (frames the socket held from the start, so
  the baseline cannot be trusted yet). Once PACE_MIN_PUSHES pushes in a row arrived at live pace, the
  baseline is established and the on-time rule alone judges delivery, so a short network stall
  (frames bunched but within PUSH_GAP_S) is not stale, while frames held through a pause are. On time: receipt minus `updatedAt` no more than
  PUSH_GAP_S above the quickest delivery over the last DELIVERY_PUSHES live-paced pushes (counted,
  and only pushes not bunched with the one before join it, so no pause and no number of held frames
  can displace it; a constant clock offset or steady delay cancels out, and a clock step heals within
  those pushes). Until PACE_MIN_PUSHES pushes can be judged, at the start, nothing fires: a crossing then
  is compared with the first mark once they can (a touch that reverses within that second is
  missed). Without `updatedAt` only the gap and price rules apply. A crossing seen only across a
  stale gap is never fired: the watch halts and says so.
- Funding: the average of the pushed `fundingRate` (per cent per hour, the unit `strategy draft
  --funding` uses) over the last window of contiguous fresh pushes. A gap restarts the window. With a
  level it is a condition judged at the crossing; alone it fires when the full-window average moves
  from not meeting the condition to meeting it.
- No fresh push for HALT_STALE_S ends the watch.

The server pushes every 500 ms; Reya computes the mark once a second and funding every 5 s.
"""

from typing import Literal

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

PUSH_GAP_S = 3.0  # six missed 500 ms pushes; also how late a push may arrive (see Trigger.on_push)
PRICE_STALL_S = 10.0  # the venue's own mark-age gate
PACE = 0.5  # live receipts advance at least this share of what the row stamps advance; a buffered burst does not
PACE_PUSHES, PACE_MIN_PUSHES = 4, 3  # pace is judged over the last 4 pushes, once there are 3, so jitter averages out
# The quickest delivery is the best over this many live-paced pushes (about 30 s at the usual 500 ms). Counted, not
# timed: a pause of any length cannot empty it, so frames held through the pause are always measured against
# deliveries from before it.
DELIVERY_PUSHES = 60
HALT_STALE_S = 60.0

MARK_OPS = ("lte", "gte")
FUNDING_OPS = ("lt", "lte", "gt", "gte")


@dataclass(frozen=True)
class Level:
    op: str  # "lte": fires at or below `price`; "gte": at or above
    price: Decimal

    def reached(self, mark: Decimal) -> bool:
        return mark <= self.price if self.op == "lte" else mark >= self.price

    @property
    def text(self) -> str:
        return f"mark {'<=' if self.op == 'lte' else '>='} {self.price}"


@dataclass(frozen=True)
class FundingRule:
    op: str
    pct_per_hour: Decimal
    window_s: float

    def holds(self, average: Decimal) -> bool:
        return {
            "lt": average < self.pct_per_hour,
            "lte": average <= self.pct_per_hour,
            "gt": average > self.pct_per_hour,
            "gte": average >= self.pct_per_hour,
        }[self.op]

    @property
    def text(self) -> str:
        sign = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[self.op]
        return f"{int(self.window_s // 60)}-minute average funding {sign} {self.pct_per_hour}%/h"


def parse_level(text: str) -> Level:
    op, _, value = text.partition(":")
    if op not in MARK_OPS or not value:
        raise ValueError(f"--mark must be lte:PRICE or gte:PRICE, got '{text}'")
    price = Decimal(value)
    if not price.is_finite() or price <= 0:
        raise ValueError(f"--mark needs a positive price, got '{value}'")
    return Level(op, price)


def parse_funding(text: str, window_minutes: int) -> FundingRule:
    op, _, value = text.partition(":")
    if op not in FUNDING_OPS or not value:
        raise ValueError(f"--funding must be lt|lte|gt|gte:PCT_PER_HOUR, got '{text}'")
    rate = Decimal(value)
    if not rate.is_finite():
        raise ValueError(f"--funding needs a number (per cent per hour), got '{value}'")
    return FundingRule(op, rate, window_minutes * 60.0)


@dataclass(frozen=True)
class Push:
    receipt: float  # this computer's wall clock when the push arrived, in seconds
    prices_updated_at: int  # server, milliseconds: the last price update
    mark: Decimal
    funding: Decimal | None
    updated_at: int | None = None  # server, milliseconds: when this push's row was computed (every push)


@dataclass(frozen=True)
class Decision:
    kind: Literal["watching", "fire", "halt", "stale", "recovered", "skip"]
    reason: str | None = None
    detail: dict = field(default_factory=dict)


class Trigger:
    def __init__(self, level: Level | None, funding: FundingRule | None):
        if level is None and funding is None:
            raise ValueError("Give --mark, --funding or both")
        self.level, self.funding = level, funding
        self.prev: Push | None = None  # the last fresh push, for the crossing
        self.last: Push | None = None  # the last push of any kind
        self.price_change_at: float | None = None
        self.last_fresh_at: float | None = None
        self.stale = False
        self.gap = False  # a stale gap since `prev`: the next fresh push cannot be a crossing
        self.window: deque = deque()
        self.window_start: float | None = None
        self.funding_ok: bool | None = None  # funding-only: the last full-window verdict
        self.had_baseline = False  # funding-only: a not-met verdict before the current gap
        self.deliveries: deque = deque(maxlen=DELIVERY_PUSHES)  # receipt minus row stamp (ms) of live-paced pushes
        self.recent: deque = deque(maxlen=PACE_PUSHES)  # (receipt, row stamp) of the last pushes
        self.paced = True  # enough pushes since the start to judge their pace
        self.calm = 0  # pushes in a row that arrived at live pace (not bunched)
        self.established = False  # the delivery baseline comes from live-paced pushes
        self.late_ms: int | None = None  # how much later than the quickest delivery the last push arrived

    # ------------------------------------------------------------------ state

    def _detail(self, push: Push, **extra) -> dict:
        detail: dict = {"mark": str(push.mark), "prices_updated_at": push.prices_updated_at}
        if self.late_ms is not None:
            detail["late_ms"] = self.late_ms
        if self.level is not None:
            detail["level"] = self.level.text
        if self.funding is not None:
            average = self.average()
            detail["funding_pct_per_hour"] = None if push.funding is None else str(push.funding)
            detail["funding_average_pct_per_hour"] = None if average is None else str(average)
        return {**detail, **extra}

    def average(self) -> Decimal | None:
        """The window's average funding once the window is full, else None."""
        if self.funding is None or self.window_start is None or not self.window:
            return None
        if self.window[-1][0] - self.window_start < self.funding.window_s:
            return None
        return sum((rate for _, rate in self.window), Decimal(0)) / len(self.window)

    def _restart_window(self) -> None:
        self.window.clear()
        self.window_start = None

    def _record_funding(self, push: Push) -> None:
        if self.funding is None:
            return
        if push.funding is None:
            self._restart_window()
            return
        if self.window_start is None:
            self.window_start = push.receipt
        self.window.append((push.receipt, push.funding))
        while self.window and self.window[0][0] < push.receipt - self.funding.window_s:
            self.window.popleft()

    # ------------------------------------------------------------------ events

    def on_idle(self, now: float) -> Decision | None:
        """No push since the last call: a gap past PUSH_GAP_S is stale, past HALT_STALE_S ends the watch."""
        if self.last_fresh_at is not None and now - self.last_fresh_at > HALT_STALE_S:
            return Decision("halt", "feed_stale", {"stale_s": round(now - self.last_fresh_at, 1)})
        if self.last is not None and now - self.last.receipt > PUSH_GAP_S and not self.stale:
            self.stale, self.gap = True, True
            return Decision("stale", "no_push", {"since_s": round(now - self.last.receipt, 1)})
        return None

    def on_push(self, push: Push) -> Decision | None:
        if self.last is not None and push.prices_updated_at < self.last.prices_updated_at:
            return None  # older than what was already seen (a resubscribe snapshot): never evaluated
        last, self.last = self.last, push
        if last is None or push.prices_updated_at != last.prices_updated_at:
            self.price_change_at = push.receipt
        contiguous = (
            last is not None
            and push.receipt - last.receipt <= PUSH_GAP_S
            and push.prices_updated_at - last.prices_updated_at <= PUSH_GAP_S * 1000
        )
        stalled = self.price_change_at is not None and push.receipt - self.price_change_at > PRICE_STALL_S
        late = self._late(last, push)
        if last is None:
            return self._first(push)
        if not contiguous or stalled or late:
            if self.last_fresh_at is not None and push.receipt - self.last_fresh_at > HALT_STALE_S:
                return Decision("halt", "feed_stale", {"stale_s": round(push.receipt - self.last_fresh_at, 1)})
            self.gap = True
            self._restart_window()
            if not self.stale:
                self.stale = True
                reason = "prices_not_updating" if stalled else "late_delivery" if late else "push_gap"
                return Decision("stale", reason, self._detail(push))
            return None
        self.last_fresh_at = push.receipt
        if self.gap:
            return self._after_gap(push)
        self._record_funding(push)
        return self._evaluate(push)

    def _late(self, last: Push | None, push: Push) -> bool:
        """A push from a buffered burst, or delivered over PUSH_GAP_S later than the recent quickest one."""
        if push.updated_at is None:
            self.late_ms, self.paced = None, True
            return False
        lag_ms = push.receipt * 1000 - push.updated_at
        self.recent.append((push.receipt, push.updated_at))
        self.paced = len(self.recent) >= PACE_MIN_PUSHES
        first_receipt, first_stamp = self.recent[0]
        span_ms = push.updated_at - first_stamp
        slow = span_ms > 0 and (push.receipt - first_receipt) * 1000 < PACE * span_ms
        bunched = (
            last is not None
            and last.updated_at is not None
            and push.updated_at > last.updated_at
            and (push.receipt - last.receipt) * 1000 < PACE * (push.updated_at - last.updated_at)
        )
        self.calm = 0 if bunched else self.calm + 1
        burst = not self.established and self.paced and slow and bunched
        if self.calm >= PACE_MIN_PUSHES:
            self.established = True
        if not bunched:
            # Only a push at live pace joins the baseline: frames held through a pause arrive bunched, so however
            # many there are they never displace the deliveries from before it.
            self.deliveries.append(lag_ms)
        best = min(self.deliveries, default=lag_ms)
        self.late_ms = int(max(0.0, lag_ms - best))
        return burst or self.late_ms > PUSH_GAP_S * 1000

    def _first(self, push: Push) -> Decision:
        self.last_fresh_at = push.receipt
        self._record_funding(push)
        if self.level is not None and self.level.reached(push.mark):
            return Decision("halt", "already_triggered", self._detail(push))
        self.prev = push
        return Decision("watching", None, self._detail(push))

    def _after_gap(self, push: Push) -> Decision:
        """The first fresh push after a stale gap: report the recovery; a crossing hidden in the gap halts."""
        self.gap, self.stale = False, False
        prev, self.prev = self.prev, push
        self._record_funding(push)
        if self.level is not None and prev is not None and not self.level.reached(prev.mark):
            if self.level.reached(push.mark):
                return Decision("halt", "crossed_while_stale", self._detail(push, before_gap=str(prev.mark)))
        if self.level is None and self.funding_ok is False:
            self.had_baseline = True
        if self.level is None:
            self.funding_ok = None
        return Decision("recovered", None, self._detail(push))

    def _evaluate(self, push: Push) -> Decision | None:
        if not self.paced:
            return None  # too few pushes since the start or a gap to judge their pace: compare with `prev` later
        prev, self.prev = self.prev, push
        if self.level is not None:
            if prev is None or self.level.reached(prev.mark) or not self.level.reached(push.mark):
                return None
            if self.funding is not None:
                average = self.average()
                if average is None:
                    return Decision("skip", "funding_window_not_full", self._detail(push))
                if not self.funding.holds(average):
                    return Decision("skip", "funding_condition_not_met", self._detail(push))
            return Decision("fire", None, self._detail(push))
        assert self.funding is not None
        average = self.average()
        if average is None:
            return None
        ok = self.funding.holds(average)
        previous, self.funding_ok = self.funding_ok, ok
        if previous is None:
            if ok:
                reason = "crossed_while_stale" if self.had_baseline else "already_triggered"
                return Decision("halt", reason, self._detail(push))
            return Decision("watching", "funding_window_full", self._detail(push))
        if not previous and ok:
            return Decision("fire", None, self._detail(push))
        return None
