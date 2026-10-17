"""The inspected Review global-sort contract, with no browser, network or X calls."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import fpl_bot.captain_review_browser as review
from fpl_bot.captain_service import build_captain_report
from fpl_bot.errors import CaptainProjectionError, CaptainReviewBrowserError
from fpl_bot.models import Fixture, FplEvent, FplPlayer, Team
from fpl_bot.service import build_event_report

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
SCOPE = review.ReviewViewScope(True, True, True, True)


def snapshot(points=(9, 8, 7, 6), *, direction="descending", active=True, names=None):
    headers = tuple(
        review.ReviewDomCell(label, tag_name="th")
        for label in ("PLAYER", "PRICE", "GW7", "GW8", "TOTAL", "ELITE OWN%")
    )
    headers = tuple(
        replace(cell, sort_direction=direction, sort_active=active)
        if cell.text == "GW8"
        else replace(cell, sort_direction="none")
        for cell in headers
    )
    rows = tuple(
        tuple(
            review.ReviewDomCell(value)
            for value in (
                f"{names[i] if names else f'Player {i + 1}'}\nAAA • DEF",
                "£5m",
                "99",
                str(value),
                "100",
                "90%",
            )
        )
        for i, value in enumerate(points)
    )
    return review.ReviewPageSnapshot((review.ReviewDomTable((headers,), rows),), scope=SCOPE)


class Page:
    """Scripted DOM observations plus exact locator/click assertions."""

    def __init__(self, states):
        self.states = states
        self.reads = 0
        self.milliseconds = 0
        self.clicks = []
        self.resets = []

    def read(self, _page):
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return state

    def locator(self, selector):
        return Locator(self, (selector,))

    def wait_for_timeout(self, milliseconds):
        self.milliseconds += milliseconds


class Locator:
    def __init__(self, page, path):
        self.page, self.path = page, path

    def nth(self, index):
        return Locator(self.page, (*self.path, index))

    def locator(self, selector):
        return Locator(self.page, (*self.path, selector))

    def click(self, *, timeout):
        assert timeout > 0
        self.page.clicks.append(self.path)

    def evaluate(self, script):
        assert "scrollTop = 0" in script
        self.page.resets.append(self.path)


def wait(monkeypatch, states, *, timeout=2200):
    page = Page(states)
    monkeypatch.setattr(review, "_snapshot_page", page.read)
    result = review._wait_for_ranked_projection_view(
        page,
        8,
        timeout,
        monotonic_clock=lambda: page.milliseconds / 1000,
        utc_clock=lambda: NOW + timedelta(milliseconds=page.milliseconds),
    )
    return result, page


@pytest.mark.parametrize(
    "directions,clicks",
    [
        (("descending",), 0),
        (("none", "descending"), 1),
        (("none", "ascending", "descending"), 2),
        (("ascending", "descending"), 1),
        (("ascending", "none", "descending"), 2),
    ],
)
def test_exact_official_gw_header_and_verified_direction(monkeypatch, directions, clicks):
    result, page = wait(monkeypatch, [snapshot(direction=d) for d in directions])
    assert page.clicks == [("table", 0, "thead tr", 0, "th,td", 3)] * clicks
    # GW7 is leftmost: never use a positional "first GW" shortcut.
    assert result.ranked_prefix.event_id == 8
    assert result.ranked_prefix.sort_clicks == clicks
    assert result.ranked_prefix.row_count == 4
    assert page.milliseconds >= 800
    assert not page.resets  # No full scrolling/pagination acquisition.
    assert review.require_ranked_prefix(result, 8).rows[0].projected_points == 9


@pytest.mark.parametrize(
    "state",
    [
        snapshot(direction="unrecognized"),
        snapshot(direction="ascending"),  # Unresponsive click cannot be treated as success.
        snapshot(active=False),
        snapshot((8, 9, 7, 6)),  # Descending icon but non-descending values.
    ],
)
def test_sort_failure_never_falls_back_to_total_order(monkeypatch, state):
    with pytest.raises(CaptainReviewBrowserError, match="projection_sort_not_verified"):
        wait(monkeypatch, [state])


@pytest.mark.parametrize(
    "field", ["all_players", "search_empty", "price_unrestricted", "points_metric"]
)
def test_filtered_or_wrong_metric_view_is_not_accepted(monkeypatch, field):
    state = replace(snapshot(), scope=replace(SCOPE, **{field: False}))
    with pytest.raises(CaptainReviewBrowserError, match="projection_scope_not_verified"):
        wait(monkeypatch, [state])


@pytest.mark.parametrize("change", ["missing", "duplicate", "layout_change", "two_active"])
def test_changed_or_ambiguous_source_headers_fail_closed(monkeypatch, change):
    state = snapshot()
    table = state.tables[0]
    headers = table.header_rows[0]
    if change == "missing":
        headers = tuple(replace(c, text="GW9") if c.text == "GW8" else c for c in headers)
    elif change == "duplicate":
        headers = (*headers, headers[3])
    elif change == "layout_change":
        headers = tuple(replace(c, text="GW9") if c.text == "GW7" else c for c in headers)
    else:
        headers = tuple(replace(c, sort_active=True) if c.text == "TOTAL" else c for c in headers)
    changed = replace(state, tables=(replace(table, header_rows=(headers,)),))
    with pytest.raises(CaptainReviewBrowserError, match="projection_sort_not_verified"):
        wait(monkeypatch, [state, changed] if change == "layout_change" else [changed])


@pytest.mark.parametrize("change", ["identity", "points"])
def test_same_row_count_is_not_a_stable_snapshot(monkeypatch, change):
    before = snapshot()
    after = (
        snapshot(names=("New Player", "Player 2", "Player 3", "Player 4"))
        if change == "identity"
        else snapshot((10, 8, 7, 6))
    )
    result, page = wait(monkeypatch, [before, before, before, before, after])
    assert page.milliseconds >= 1600  # Restarts the full stability period at the change.
    assert result.ranked_prefix.row_digest != review._ranked_rows_digest(
        review.parse_review_projection_table(before, 8).rows
    )


def test_continuously_changing_source_cannot_be_called_stable(monkeypatch):
    states = [snapshot((10 + i, 8, 7, 6)) for i in range(20)]
    with pytest.raises(CaptainReviewBrowserError, match="projection_sort_not_verified"):
        wait(monkeypatch, states)


def test_delayed_reorder_and_list_position_are_verified(monkeypatch):
    initial = snapshot((5, 9, 6, 7), direction="none", active=False)
    old_rows_new_icon = snapshot((5, 9, 6, 7))
    scrolled = replace(snapshot(), tables=(replace(snapshot().tables[0], scroll_top=240),))
    result, page = wait(monkeypatch, [initial, old_rows_new_icon, scrolled, snapshot()])
    assert page.resets == [("table", 0)]
    assert result.ranked_prefix.first_points == 9
    assert page.milliseconds >= 1400


@pytest.mark.parametrize("state", [snapshot((9, 8, 7)), snapshot(names=("Same",) * 4)])
def test_short_or_duplicate_prefix_is_not_complete(monkeypatch, state):
    with pytest.raises(CaptainReviewBrowserError, match="ranked_prefix_not_verified"):
        wait(monkeypatch, [state])


def test_hidden_player_row_prevents_complete_prefix_claim(monkeypatch):
    state = snapshot()
    table = state.tables[0]
    hidden = tuple(replace(c, visible=False) for c in table.body_rows[-1])
    state = replace(state, tables=(replace(table, body_rows=(*table.body_rows, hidden)),))
    with pytest.raises(CaptainReviewBrowserError, match="ranked_prefix_not_verified"):
        wait(monkeypatch, [state])


def test_header_click_error_is_sanitized_and_does_not_return_old_rows(monkeypatch):
    def failed_click(*_args, **_kwargs):
        raise RuntimeError("SECRET_SENTINEL")

    monkeypatch.setattr(Locator, "click", failed_click)
    with pytest.raises(CaptainReviewBrowserError, match="projection_sort_not_verified") as error:
        wait(monkeypatch, [snapshot(direction="none", active=False)])
    assert "SECRET_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("tamper", ["rows", "scope", "event", "digest", "direction", "proof"])
def test_bridge_rechecks_evidence_against_exact_handoff_rows(monkeypatch, tamper):
    state, _ = wait(monkeypatch, [snapshot()])
    if tamper == "rows":
        state = replace(snapshot((10, 8, 7, 6)), ranked_prefix=state.ranked_prefix)
    elif tamper == "scope":
        state = replace(state, scope=None)
    elif tamper == "event":
        state = replace(state, ranked_prefix=replace(state.ranked_prefix, event_id=9))
    elif tamper == "digest":
        state = replace(state, ranked_prefix=replace(state.ranked_prefix, row_digest="0" * 64))
    elif tamper == "direction":
        state = replace(snapshot(direction="ascending"), ranked_prefix=state.ranked_prefix)
    else:
        state = replace(state, ranked_prefix=None)
    with pytest.raises(CaptainReviewBrowserError, match="ranked_prefix_not_verified"):
        review.require_ranked_prefix(state, 8)


def report_for(state, players):
    teams = (Team(1, "Team A", "AAA"), Team(2, "Team B", "BBB")) + tuple(
        Team(i, f"Team {i}", f"T{i}") for i in range(3, 21)
    )
    fixtures = tuple(Fixture(i, 8, 2 * i - 1, 2 * i) for i in range(1, 11))
    event = FplEvent(8, "Gameweek 8", NOW + timedelta(days=6), False, True)
    source = review.FplReviewBrowserProjectionSource(
        type("Static", (), {"acquire": lambda _, event_id: state})(),
        players,
        teams,
        clock=lambda: NOW,
    )
    return build_captain_report(
        build_event_report(event, teams, fixtures), source, players, teams, fixtures
    )


def test_global_reorder_includes_defender_outside_total_top_50(monkeypatch):
    # Synthetic universe: Total order is 1..90. Defender 80 has the second-best GW8.
    universe = tuple((f"Player {i}", Decimal(200 - i)) for i in range(1, 91))
    universe = tuple(
        (name, Decimal("198.5") if name == "Player 80" else points) for name, points in universe
    )
    sorted_universe = tuple(sorted(universe, key=lambda row: row[1], reverse=True))
    total_top_50 = snapshot(
        tuple(p for _, p in universe[:50]),
        names=tuple(n for n, _ in universe[:50]),
        direction="none",
        active=False,
    )
    gw_top_50 = snapshot(
        tuple(p for _, p in sorted_universe[:50]), names=tuple(n for n, _ in sorted_universe[:50])
    )
    result, page = wait(monkeypatch, [total_top_50, gw_top_50])
    assert len(page.clicks) == 1
    assert result.ranked_prefix.row_count == 50
    assert result.ranked_prefix.to_payload()["full_source_coverage"] is False
    full = snapshot(
        tuple(p for _, p in sorted_universe), names=tuple(n for n, _ in sorted_universe)
    )
    players = tuple(
        FplPlayer(i, f"Player {i}", 1, Decimal("9.9") if i == 17 else Decimal(20))
        for i in range(1, 91)
    )
    production, full_report = report_for(result, players), report_for(full, players)
    assert [s.player.element_id for s in production.top_three] == [1, 80, 2]
    assert production.differential.player.element_id == 17
    assert production.tweet == full_report.tweet
    # Preview uses this same source/selector/renderer, not a second algorithm.
    assert report_for(result, players) == production


@pytest.mark.parametrize("ownership,expected", [("10.0", 5), ("9.999", 4)])
def test_prefix_sufficiency_uses_official_ownership_and_source_tie_order(
    monkeypatch, ownership, expected
):
    # Equal points preserve global source order. UI's elite ownership is not used.
    state, _ = wait(monkeypatch, [snapshot((9, 8, 8, 8, 8, 8))])
    players = tuple(
        FplPlayer(
            i,
            f"Player {i}",
            1,
            Decimal(ownership) if i == 4 else Decimal("1") if i >= 5 else Decimal(20),
        )
        for i in range(1, 8)
    )
    selected = report_for(state, players)
    full = snapshot((9, 8, 8, 8, 8, 8, 8))
    assert [s.player.element_id for s in selected.top_three] == [1, 2, 3]
    assert selected.differential.player.element_id == expected
    assert selected.tweet == report_for(full, players).tweet


def test_no_eligible_differential_in_prefix_fails_without_guessing(monkeypatch):
    state, _ = wait(monkeypatch, [snapshot()])
    players = tuple(
        FplPlayer(i, f"Player {i}", 1, Decimal("1") if i == 5 else Decimal("10"))
        for i in range(1, 6)
    )
    with pytest.raises(CaptainProjectionError, match="no qualifying differential"):
        report_for(state, players)
    assert report_for(snapshot((9, 8, 7, 6, 5)), players).differential.player.element_id == 5


def test_unmatched_row_is_not_silently_dropped(monkeypatch):
    state, _ = wait(monkeypatch, [snapshot()])
    players = (FplPlayer(1, "Player 1", 1, Decimal("1")),)
    with pytest.raises(CaptainReviewBrowserError, match="identity_resolution_failed"):
        report_for(state, players)


def test_coverage_audit_is_fixed_allowlisted_metadata_not_raw_projections(monkeypatch):
    state, _ = wait(monkeypatch, [snapshot()])
    payload = state.ranked_prefix.to_payload()
    assert set(payload) == {
        "schema_version",
        "contract",
        "scope",
        "event_id",
        "column",
        "direction",
        "observed_at_utc",
        "row_count",
        "row_digest",
        "first_points",
        "last_points",
        "sort_clicks",
        "stable_observations",
        "full_source_coverage",
        "selection_sufficiency",
    }
    assert payload["selection_sufficiency"] == "requires_cloud_top_three_and_differential"
    assert "Player" not in str(payload)
