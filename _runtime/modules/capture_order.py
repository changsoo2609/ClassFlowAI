from pathlib import Path
from typing import Callable


def _captured_sort_key(record: dict, source_index: int) -> tuple[str, str, int]:
    captured_at = str(record.get("captured_at") or record.get("created_at") or "")
    image_name = Path(str(record.get("image_path") or "")).name
    return captured_at, image_name, source_index


def _valid_order(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        order = float(value)
    except (TypeError, ValueError):
        return None
    if order < 0 or order != order or order in {float("inf"), float("-inf")}:
        return None
    return order


def normalize_display_orders(records: list[dict]) -> bool:
    """Normalize duplicate/invalid orders without changing original capture data."""
    indexed = list(enumerate(records))
    has_any_order = any(_valid_order(record.get("display_order")) is not None for record in records)
    if has_any_order:
        indexed.sort(
            key=lambda item: (
                _valid_order(item[1].get("display_order")) is None,
                _valid_order(item[1].get("display_order")) or 0,
                _captured_sort_key(item[1], item[0]),
            )
        )
    else:
        indexed.sort(key=lambda item: _captured_sort_key(item[1], item[0]))

    changed = False
    for display_order, (_, record) in enumerate(indexed):
        if record.get("display_order") != display_order:
            record["display_order"] = display_order
            changed = True
    return changed


def ordered_records(records: list[dict]) -> list[dict]:
    normalize_display_orders(records)
    return sorted(records, key=lambda record: int(record.get("display_order", 0)))


def active_ordered_records(
    records: list[dict],
    exists: Callable[[dict], bool] | None = None,
) -> list[dict]:
    exists = exists or (
        lambda record: Path(str(record.get("image_path") or "")).exists()
    )
    return [
        record
        for record in ordered_records(records)
        if not record.get("deleted") and exists(record)
    ]


def move_record(
    records: list[dict],
    selected: dict,
    direction: int,
    exists: Callable[[dict], bool] | None = None,
) -> bool:
    active = active_ordered_records(records, exists=exists)
    try:
        current = active.index(selected)
    except ValueError:
        return False
    target = current + (-1 if direction < 0 else 1)
    if target < 0 or target >= len(active):
        return False
    other = active[target]
    selected["display_order"], other["display_order"] = (
        other["display_order"],
        selected["display_order"],
    )
    normalize_display_orders(records)
    return True


def restore_capture_order(records: list[dict]) -> bool:
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: _captured_sort_key(item[1], item[0]))
    changed = False
    for display_order, (_, record) in enumerate(indexed):
        if record.get("display_order") != display_order:
            record["display_order"] = display_order
            changed = True
    return changed


def restore_capture_order_if_confirmed(
    records: list[dict],
    confirm: Callable[[], bool],
) -> bool:
    if not confirm():
        return False
    return restore_capture_order(records)


def next_display_order(records: list[dict]) -> int:
    normalize_display_orders(records)
    return len(records)
