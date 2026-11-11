import pytest

from engine.execution.slicer import max_order_qty, slice_quantity


@pytest.mark.parametrize(
    ("limit", "lot", "expected"),
    [
        (1800, 65, 1755),  # NIFTY from 2 Nov 2026: 27 lots
        (3510, 65, 3445),  # exactly 54 lots → stay strictly below: 53 lots
        (900, 30, 870),
        (2800, 120, 2760),
        (600, 25, 575),
    ],
)
def test_max_order_qty(limit: int, lot: int, expected: int) -> None:
    assert max_order_qty(limit, lot) == expected


def test_slice_quantity() -> None:
    assert slice_quantity(65, 1800, 65) == [65]
    assert slice_quantity(65 * 60, 1800, 65) == [1755, 1755, 390]
    assert sum(slice_quantity(65 * 60, 1800, 65)) == 65 * 60


@pytest.mark.parametrize(
    ("qty", "limit", "lot"),
    [(0, 1800, 65), (100, 1800, 65), (65, 50, 65), (65, 0, 65)],
)
def test_slice_rejects_bad_input(qty: int, limit: int, lot: int) -> None:
    with pytest.raises(ValueError):
        slice_quantity(qty, limit, lot)
