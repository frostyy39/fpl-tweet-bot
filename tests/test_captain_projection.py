from decimal import Decimal

import pytest

from fpl_bot.captain_models import CaptainProjection
from fpl_bot.captain_projection import InMemoryCaptainProjectionSource
from fpl_bot.errors import CaptainProjectionError


def test_in_memory_projection_source_is_event_specific_and_deterministic() -> None:
    projections = (
        CaptainProjection(10, Decimal("7.47")),
        CaptainProjection(11, Decimal("6.12")),
    )
    source = InMemoryCaptainProjectionSource({4: projections})

    assert source.fetch_event_projections(4) == projections


def test_projection_source_reports_missing_event_explicitly() -> None:
    source = InMemoryCaptainProjectionSource({})

    with pytest.raises(CaptainProjectionError, match="unavailable"):
        source.fetch_event_projections(4)


def test_projection_requires_stable_positive_element_id_and_decimal_points() -> None:
    with pytest.raises(ValueError, match="element_id"):
        CaptainProjection(0, Decimal("5.0"))
    with pytest.raises(ValueError, match="finite Decimal"):
        CaptainProjection(1, Decimal("NaN"))
    with pytest.raises(ValueError, match="finite Decimal"):
        CaptainProjection(1, 5.0)  # type: ignore[arg-type]
