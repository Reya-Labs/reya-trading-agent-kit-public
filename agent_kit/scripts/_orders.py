"""Consistent order readback for the CLI and runner."""


def order_result(order) -> dict:
    entry = {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "order_side": "buy" if order.side.value == "B" else "sell",
        "symbol": order.symbol,
        "post_only": order.post_only,
        "reduce_only": order.reduce_only,
        "order_type": order.order_type.value,
        "qty": order.qty,
        "limit_px": order.limit_px,
        "trigger_px": order.trigger_px,
        "status": order.status.value,
        "created_at": order.created_at,
        "time_in_force": order.time_in_force.value if order.time_in_force is not None else None,
        "triggered": order.triggered,
        "filled_qty": order.cum_qty,
    }
    if order.order_type.value in ("TAKE_PROFIT", "STOP_LOSS"):
        entry.update(
            position_side="short" if order.side.value == "B" else "long",
            close_side="buy" if order.side.value == "B" else "sell",
            quantity_mode="whole_position",
        )
    return entry
