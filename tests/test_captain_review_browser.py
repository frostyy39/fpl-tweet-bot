from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import fpl_bot.captain_review_browser as review_browser
from fpl_bot.captain_review_browser import (
    DEDICATED_PROFILE_MARKER,
    PLAYWRIGHT_CHROME_CHANNEL,
    FplReviewBrowserProjectionSource,
    ReviewDomCell,
    ReviewDomTable,
    ReviewPageSnapshot,
    _launch_stable_chrome_context,
    _ProjectionViewFailure,
    _wait_for_projections_view,
    collect_review_identity_diagnostics,
    extract_review_projections,
    find_stable_chrome_executable,
    inspect_review_table_structure,
    open_manual_review_login,
    parse_review_projection_table,
    prepare_dedicated_profile,
    require_dedicated_profile,
    stable_chrome_login_command,
)
from fpl_bot.captain_service import build_captain_report
from fpl_bot.errors import CaptainProjectionError, CaptainReviewBrowserError
from fpl_bot.models import EventReport, Fixture, FplEvent, FplPlayer, Team
from fpl_bot.service import build_event_report


class StaticAcquirer:
    def __init__(self, snapshot: ReviewPageSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[int] = []

    def acquire(self, event_id: int) -> ReviewPageSnapshot:
        self.calls.append(event_id)
        return self.snapshot


class FailingAcquirer:
    def __init__(self, category: str) -> None:
        self.category = category

    def acquire(self, event_id: int) -> ReviewPageSnapshot:
        raise CaptainReviewBrowserError(self.category)


class FakeProcess:
    def __init__(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self.wait_calls = 0

    def wait(self) -> int:
        self.wait_calls += 1
        return self.return_code


class FakeChromium:
    def __init__(self) -> None:
        self.kwargs = None
        self.context = object()

    def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class FakeControl:
    def __init__(self, *, text: str = "PROJECTIONS", visible: bool = True) -> None:
        self.text = text
        self.visible = visible
        self.click_calls = 0
        self.click_timeouts: list[int] = []

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self) -> str:
        return self.text

    def click(self, *, timeout: int) -> None:
        self.click_calls += 1
        self.click_timeouts.append(timeout)


class FakeControls:
    def __init__(self, controls: tuple[FakeControl, ...]) -> None:
        self.controls = controls

    def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> FakeControl:
        return self.controls[index]


class FakePage:
    def __init__(
        self,
        controls: tuple[FakeControl, ...],
        clock: "FakeClock",
    ) -> None:
        self.controls = FakeControls(controls)
        self.clock = clock
        self.url = "https://app.fplreview.com/"
        self.wait_calls: list[int] = []

    def locator(self, selector: str) -> FakeControls:
        assert selector == "button"
        return self.controls

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)
        self.clock.advance(milliseconds)


class FakeClock:
    def __init__(self) -> None:
        self.elapsed_milliseconds = 0
        self.base = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.elapsed_milliseconds / 1000

    def utc_now(self) -> datetime:
        return self.base + timedelta(milliseconds=self.elapsed_milliseconds)

    def advance(self, milliseconds: int) -> None:
        self.elapsed_milliseconds += milliseconds


class SnapshotSequence:
    def __init__(self, snapshots: tuple[ReviewPageSnapshot, ...]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def __call__(self, unused: FakePage) -> ReviewPageSnapshot:
        snapshot = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snapshot


def wait_for_view(
    page: FakePage,
    clock: FakeClock,
    *,
    event_id: int = 4,
    timeout_milliseconds: int = 1000,
):
    return _wait_for_projections_view(
        page,
        event_id,
        timeout_milliseconds,
        app_loaded_at_utc=clock.utc_now(),
        utc_clock=clock.utc_now,
        monotonic_clock=clock.monotonic,
        poll_milliseconds=100,
    )


def cell(text: str, *, visible: bool = True) -> ReviewDomCell:
    return ReviewDomCell(text, visible)


def table(
    rows: list[tuple[ReviewDomCell, ...]],
    headers: tuple[ReviewDomCell, ...] | None = None,
) -> ReviewPageSnapshot:
    if headers is None:
        rows = [
            logical_row(row[0].text, row[1].text, row[2].text) if len(row) == 3 else row
            for row in rows
        ]
    return ReviewPageSnapshot(
        (
            ReviewDomTable(
                header_rows=(
                    headers
                    or (
                        cell("PLAYER"),
                        cell("PRICE"),
                        cell("GW4"),
                        cell("TOTAL"),
                        cell("ELITE OWN%"),
                    ),
                ),
                body_rows=tuple(rows),
            ),
        )
    )


def logical_row(
    name: str,
    team_code: str,
    points: str,
    *,
    price: str = "£7.5m",
    total: str = "20.0",
    elite_ownership: str = "12.0%",
) -> tuple[ReviewDomCell, ...]:
    return (
        cell(f"{name}\n{team_code} • MID"),
        cell(price),
        cell(points),
        cell(total),
        cell(elite_ownership),
    )


def official_teams(count: int = 20) -> tuple[Team, ...]:
    return tuple(Team(index, f"Team {index}", f"T{index}") for index in range(1, count + 1))


def official_players(count: int = 6, *, ownership: str = "20.0") -> tuple[FplPlayer, ...]:
    return tuple(
        FplPlayer(index, f"Player {index}", ((index - 1) % 20) + 1, Decimal(ownership))
        for index in range(1, count + 1)
    )


def projection_rows(count: int = 6) -> list[tuple[ReviewDomCell, ...]]:
    return [
        logical_row(
            f"Player {index}",
            f"T{((index - 1) % 20) + 1}",
            str(100 - index),
        )
        for index in range(1, count + 1)
    ]


def test_valid_table_parses_exact_decimals_and_preserves_source_order() -> None:
    snapshot = table(
        [
            (cell("Player 2"), cell("T2"), cell("7.50")),
            (cell("Player 1"), cell("T1"), cell("7.50")),
            (cell("Player 3"), cell("T3"), cell("6.125")),
        ]
    )

    result = extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert [(item.element_id, item.projected_points) for item in result] == [
        (2, Decimal("7.50")),
        (1, Decimal("7.50")),
        (3, Decimal("6.125")),
    ]


def test_live_desktop_rows_ignore_unused_presentation_fields() -> None:
    snapshot = ReviewPageSnapshot(
        (
            ReviewDomTable(
                header_rows=(
                    (
                        cell("PLAYER"),
                        cell("PRICE"),
                        cell("GW4"),
                        cell("TOTAL"),
                        cell("ELITE OWN%"),
                    ),
                ),
                body_rows=(
                    (
                        cell("responsive combined cell", visible=False),
                        cell("Haaland\nMCI • FWD\n93"),
                        cell("£15.5m"),
                        cell("5.53"),
                        cell("80.08"),
                        cell("97.2%"),
                    ),
                    (
                        cell("responsive combined cell", visible=False),
                        cell("B.Fernandes\nMUN • MID\n91"),
                        cell("£12m\nPP: 12.0"),
                        cell("5.30"),
                        cell("76.18"),
                        cell("30.0%"),
                    ),
                    (
                        cell("responsive combined cell", visible=False),
                        cell("Palmer\nCHE • MID"),
                        cell("arbitrary future price presentation"),
                        cell("7.35"),
                        cell("unused total presentation"),
                        cell("unused ownership presentation"),
                    ),
                    (
                        cell("responsive combined cell", visible=False),
                        cell("Rogers\nAVL • MID\npublic trailing note\nanother note"),
                        cell("not parsed"),
                        cell("6.08"),
                        cell("not parsed"),
                        cell("not parsed"),
                    ),
                ),
            ),
        )
    )

    canonical = parse_review_projection_table(snapshot, 4)

    assert [(row.display_name, row.team_short_code) for row in canonical.rows] == [
        ("Haaland", "MCI"),
        ("B.Fernandes", "MUN"),
        ("Palmer", "CHE"),
        ("Rogers", "AVL"),
    ]
    assert [row.projected_points for row in canonical.rows] == [
        Decimal("5.53"),
        Decimal("5.30"),
        Decimal("7.35"),
        Decimal("6.08"),
    ]


def test_responsive_duplicate_cells_are_resolved_structurally() -> None:
    snapshot = table(
        [
            (
                cell("Player 1 T1 £7.5m", visible=False),
                *logical_row("Player 1", "T1", "7.43"),
            ),
            (
                cell("Player 2 T2 £7.5m", visible=False),
                *logical_row("Player 2", "T2", "6.37"),
            ),
        ],
        headers=(
            cell("PLAYER"),
            cell("PRICE"),
            cell("GW4"),
            cell("TOTAL"),
            cell("ELITE OWN%"),
            cell("Player / Price", visible=False),
        ),
    )

    result = extract_review_projections(snapshot, 4, official_players(), official_teams())
    canonical = parse_review_projection_table(snapshot, 4)

    assert [item.element_id for item in result] == [1, 2]
    assert canonical.raw_header_count == 6
    assert canonical.logical_headers == ("PLAYER", "PRICE", "GW4", "TOTAL", "ELITE OWN%")
    assert canonical.logical_header_raw_indexes == (0, 1, 2, 3, 4)
    assert canonical.representative_raw_row_cell_count == 6
    assert canonical.logical_row_cell_count == 5


def test_multiple_semantically_valid_logical_blocks_fail_closed() -> None:
    headers = (
        cell("PLAYER"),
        cell("PRICE"),
        cell("GW4"),
        cell("TOTAL"),
        cell("ELITE OWN%"),
        cell("PLAYER"),
        cell("PRICE"),
        cell("GW4"),
        cell("TOTAL"),
        cell("ELITE OWN%"),
    )
    logical = logical_row("Player 1", "T1", "7.43")
    snapshot = table([(*logical, *logical)], headers=headers)

    with pytest.raises(CaptainReviewBrowserError) as raised:
        parse_review_projection_table(snapshot, 4)

    assert raised.value.category == "invalid_projection_table"


@pytest.mark.parametrize("missing", ["PLAYER", "PRICE", "TOTAL", "ELITE OWN%"])
def test_missing_required_semantic_header_fails_closed(missing: str) -> None:
    headers = tuple(
        cell(label)
        for label in ("PLAYER", "PRICE", "GW4", "TOTAL", "ELITE OWN%")
        if label != missing
    )
    snapshot = table([logical_row("Player 1", "T1", "7.43")], headers=headers)

    with pytest.raises(CaptainReviewBrowserError) as raised:
        parse_review_projection_table(snapshot, 4)

    assert raised.value.category == "invalid_projection_table"


def test_unused_price_total_and_ownership_values_do_not_block_extraction() -> None:
    snapshot = table(
        [
            (
                cell("Player 1\nT1 • MID"),
                cell("not-a-price-and-that-is-irrelevant"),
                cell("7.43"),
                cell("not-a-total"),
                cell("not-ownership"),
            ),
        ]
    )

    canonical = parse_review_projection_table(snapshot, 4)

    assert canonical.rows[0].projected_points == Decimal("7.43")


@pytest.mark.parametrize(
    "player_cell",
    (
        "Player 1",
        "Player 1\nT1",
        "Player 1\nT1 • MID\nT2 • DEF",
    ),
)
def test_missing_or_ambiguous_team_position_line_fails_closed(player_cell: str) -> None:
    snapshot = table(
        [
            (
                cell(player_cell),
                cell("unused"),
                cell("7.43"),
                cell("unused"),
                cell("unused"),
            )
        ]
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        parse_review_projection_table(snapshot, 4)

    assert raised.value.category == "invalid_projection_table"


def test_clearly_auxiliary_row_is_excluded_but_genuine_row_is_preserved() -> None:
    snapshot = ReviewPageSnapshot(
        (
            ReviewDomTable(
                header_rows=(
                    (
                        cell("PLAYER"),
                        cell("PRICE"),
                        cell("GW4"),
                        cell("TOTAL"),
                        cell("ELITE OWN%"),
                    ),
                ),
                body_rows=(
                    (cell("responsive auxiliary content", visible=False),),
                    logical_row("Player 1", "T1", "7.43"),
                ),
            ),
        )
    )

    canonical = parse_review_projection_table(snapshot, 4)

    assert canonical.raw_body_row_count == 2
    assert canonical.genuine_player_row_count == 1
    assert canonical.auxiliary_row_count == 1
    assert canonical.auxiliary_reason == "no_visible_logical_column_block"
    assert [row.source_row_ordinal for row in canonical.rows] == [2]


def test_all_player_like_row_failures_are_collected_without_row_dropping() -> None:
    snapshot = table(
        [
            (
                cell("Player 1 without team metadata"),
                cell("unused"),
                cell("7.43"),
                cell("unused"),
                cell("unused"),
            ),
            logical_row("Player 2", "T2", "not-points"),
        ]
    )
    source = FplReviewBrowserProjectionSource(
        StaticAcquirer(snapshot),
        official_players(),
        official_teams(),
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        source.fetch_event_projections(4)

    assert raised.value.category == "invalid_projection_table"
    assert source.last_row_extraction_diagnostic is not None
    assert [
        failure.source_row_ordinal for failure in source.last_row_extraction_diagnostic.failures
    ] == [
        1,
        2,
    ]
    assert [failure.reason for failure in source.last_row_extraction_diagnostic.failures] == [
        "invalid_player_cell",
        "invalid_selected_gameweek_projection",
    ]


def test_draft_table_with_matching_gameweek_never_satisfies_projection_readiness(
    monkeypatch,
) -> None:
    draft = ReviewPageSnapshot(
        (
            ReviewDomTable(
                ((cell("DRAFT"), cell("GW4"), cell("TOT\nEVAL")),),
                ((cell("Draft\nA"), cell("63.4\n-/1"), cell("326.8\n739.7\nTOTAL")),),
            ),
        )
    )
    clock = FakeClock()
    page = FakePage((FakeControl(),), clock)
    monkeypatch.setattr(review_browser, "_snapshot_page", SnapshotSequence((draft,)))

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock, timeout_milliseconds=300)

    assert raised.value.category == "projection_table_unavailable"
    assert page.controls.controls[0].click_calls == 1


def test_other_gameweek_matrix_never_satisfies_projection_readiness(monkeypatch) -> None:
    matrix = ReviewPageSnapshot(
        (
            ReviewDomTable(
                ((cell("TEAM"), cell("GW4"), cell("SCORE")),),
                ((cell("ARS"), cell("12"), cell("1")),),
            ),
        )
    )
    clock = FakeClock()
    page = FakePage((FakeControl(),), clock)
    monkeypatch.setattr(review_browser, "_snapshot_page", SnapshotSequence((matrix,)))

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock, timeout_milliseconds=300)

    assert raised.value.category == "projection_table_unavailable"


def test_projection_table_already_present_is_stably_accepted_without_click(monkeypatch) -> None:
    snapshot = table(projection_rows(2))
    control = FakeControl()
    clock = FakeClock()
    page = FakePage((control,), clock)
    sequence = SnapshotSequence((snapshot, snapshot))
    monkeypatch.setattr(review_browser, "_snapshot_page", sequence)

    result = wait_for_view(page, clock)

    assert result.snapshot.tables == snapshot.tables
    assert result.diagnostic.initial_semantic_table_present is True
    assert result.diagnostic.ready_at_utc is not None
    assert control.click_calls == 0


def test_navigation_control_requires_exact_normalized_button_text(monkeypatch) -> None:
    draft = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    projections = table(projection_rows(1))
    controls = (
        FakeControl(text="Projection Settings"),
        FakeControl(text="  projections\n "),
    )
    clock = FakeClock()
    page = FakePage(controls, clock)
    monkeypatch.setattr(
        review_browser,
        "_snapshot_page",
        SnapshotSequence((draft, projections, projections)),
    )

    wait_for_view(page, clock)

    assert controls[0].click_calls == 0
    assert controls[1].click_calls == 1


def test_navigation_diagnostic_bounds_relevant_rendered_control_texts(monkeypatch) -> None:
    draft = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    projections = table(projection_rows(1))
    controls = (
        FakeControl(text="Projection Settings"),
        FakeControl(text=" projections "),
    )
    clock = FakeClock()
    page = FakePage(controls, clock)
    monkeypatch.setattr(
        review_browser,
        "_snapshot_page",
        SnapshotSequence((draft, projections, projections)),
    )

    result = wait_for_view(page, clock)

    assert result.diagnostic.observations[0].relevant_control_texts == (
        "PROJECTION SETTINGS",
        "PROJECTIONS",
    )


def test_draft_is_replaced_by_projection_table_after_exactly_one_click(monkeypatch) -> None:
    draft = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    projections = table(projection_rows(2))
    control = FakeControl()
    clock = FakeClock()
    page = FakePage((control,), clock)
    sequence = SnapshotSequence((draft, projections, projections))
    monkeypatch.setattr(review_browser, "_snapshot_page", sequence)

    result = wait_for_view(page, clock)

    assert result.snapshot.tables == projections.tables
    assert control.click_calls == 1
    assert sequence.calls == 3
    assert result.diagnostic.click_completed is True
    assert result.diagnostic.all_required_same_table_at_utc is not None


def test_replaced_dom_table_nodes_are_requeried_without_stale_handles(monkeypatch) -> None:
    draft = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    first_projection_node = table(projection_rows(2))
    replacement_projection_node = table(projection_rows(2))
    assert first_projection_node is not replacement_projection_node
    clock = FakeClock()
    control = FakeControl()
    page = FakePage((control,), clock)
    sequence = SnapshotSequence((draft, first_projection_node, replacement_projection_node))
    monkeypatch.setattr(review_browser, "_snapshot_page", sequence)

    result = wait_for_view(page, clock)

    assert result.snapshot.tables == replacement_projection_node.tables
    assert control.click_calls == 1
    assert sequence.calls == 3


def test_semantic_headers_can_arrive_asynchronously_in_stages(monkeypatch) -> None:
    draft = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    partial_player = table([logical_row("Player 1", "T1", "7")], headers=(cell("PLAYER"),))
    partial_gameweek = table(
        [logical_row("Player 1", "T1", "7")],
        headers=(cell("PLAYER"), cell("PRICE"), cell("GW4")),
    )
    complete = table(projection_rows(1))
    clock = FakeClock()
    page = FakePage((FakeControl(),), clock)
    sequence = SnapshotSequence((draft, partial_player, partial_gameweek, complete, complete))
    monkeypatch.setattr(review_browser, "_snapshot_page", sequence)

    result = wait_for_view(page, clock)

    assert result.snapshot.tables == complete.tables
    assert sequence.calls == 5
    assert result.diagnostic.first_player_at_utc is not None
    assert result.diagnostic.first_price_at_utc is not None
    assert result.diagnostic.first_expected_gameweek_at_utc is not None
    assert result.diagnostic.first_total_at_utc is not None


def test_multiple_visible_projection_controls_fail_closed(monkeypatch) -> None:
    snapshot = ReviewPageSnapshot(
        (ReviewDomTable(((cell("DRAFT"), cell("GW4")),), ((cell("A"), cell("1")),)),)
    )
    controls = (FakeControl(), FakeControl())
    clock = FakeClock()
    page = FakePage(controls, clock)
    monkeypatch.setattr(review_browser, "_snapshot_page", SnapshotSequence((snapshot,)))

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock)

    assert raised.value.category == "projection_table_unavailable"
    assert all(control.click_calls == 0 for control in controls)


def test_projection_navigation_timeout_is_typed(monkeypatch) -> None:
    clock = FakeClock()
    page = FakePage((), clock)
    monkeypatch.setattr(
        review_browser,
        "_snapshot_page",
        SnapshotSequence((ReviewPageSnapshot(()),)),
    )

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock, timeout_milliseconds=300)

    assert raised.value.category == "projection_table_unavailable"
    assert raised.value.diagnostic.ready_at_utc is None
    assert sum(page.wait_calls) == 300


def test_projection_view_with_wrong_gameweek_is_distinct(monkeypatch) -> None:
    snapshot = table(
        [logical_row("Player 1", "T1", "7")],
        headers=(
            cell("PLAYER"),
            cell("PRICE"),
            cell("GW5"),
            cell("TOTAL"),
            cell("ELITE OWN%"),
        ),
    )
    clock = FakeClock()
    page = FakePage((FakeControl(),), clock)
    monkeypatch.setattr(review_browser, "_snapshot_page", SnapshotSequence((snapshot,)))

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock, timeout_milliseconds=300)

    assert raised.value.category == "upcoming_event_column_missing"


def test_authentication_failure_remains_distinct_from_navigation_failure(monkeypatch) -> None:
    clock = FakeClock()
    page = FakePage((), clock)
    monkeypatch.setattr(
        review_browser,
        "_snapshot_page",
        SnapshotSequence((ReviewPageSnapshot((), login_required=True),)),
    )

    with pytest.raises(_ProjectionViewFailure) as raised:
        wait_for_view(page, clock)

    assert raised.value.category == "reauthentication_required"


def test_readiness_timeout_budget_starts_after_app_load(monkeypatch) -> None:
    empty = ReviewPageSnapshot(())
    clock = FakeClock()
    page = FakePage((), clock)
    monkeypatch.setattr(review_browser, "_snapshot_page", SnapshotSequence((empty,)))

    with pytest.raises(_ProjectionViewFailure):
        wait_for_view(page, clock, timeout_milliseconds=450)

    assert sum(page.wait_calls) == 450
    assert clock.elapsed_milliseconds == 450


def test_structural_inspection_and_acquisition_share_semantic_predicate(monkeypatch) -> None:
    snapshot = table(projection_rows(1))
    original = review_browser._semantic_projection_alignments
    calls: list[int | None] = []

    def tracked(page_snapshot, event_id):
        calls.append(event_id)
        return original(page_snapshot, event_id)

    monkeypatch.setattr(review_browser, "_semantic_projection_alignments", tracked)
    inspections = inspect_review_table_structure(snapshot, 4)
    clock = FakeClock()
    page = FakePage((FakeControl(),), clock)
    monkeypatch.setattr(
        review_browser,
        "_snapshot_page",
        SnapshotSequence((snapshot, snapshot)),
    )

    result = wait_for_view(page, clock)

    assert inspections
    assert result.snapshot.tables == snapshot.tables
    assert calls.count(4) >= 3


@pytest.mark.parametrize(
    "headers",
    [
        (cell("Player"), cell("Team"), cell("Week Four")),
        (),
    ],
)
def test_changed_or_missing_expected_header_fails_closed(
    headers: tuple[ReviewDomCell, ...],
) -> None:
    snapshot = ReviewPageSnapshot((ReviewDomTable((headers,), tuple(projection_rows(1))),))

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert raised.value.category == "upcoming_event_column_missing"


def test_selected_upcoming_gameweek_column_must_match() -> None:
    snapshot = table(projection_rows(1), headers=(cell("Player"), cell("Team"), cell("GW3")))

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert raised.value.category == "upcoming_event_column_missing"


def test_duplicate_selected_gameweek_headers_are_rejected_as_ambiguous() -> None:
    snapshot = table(
        projection_rows(1),
        headers=(
            cell("PLAYER"),
            cell("PRICE"),
            cell("GW4"),
            cell("GW4"),
            cell("TOTAL"),
            cell("ELITE OWN%"),
        ),
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert raised.value.category == "invalid_projection_table"


def test_structural_inspection_is_bounded_and_allowlisted() -> None:
    rich_cell = ReviewDomCell(
        "Player 1\nT1",
        visible=True,
        tag_name="td",
        class_name="desktop-cell",
        aria_hidden="false",
        hidden=False,
        display="table-cell",
        visibility="visible",
    )
    snapshot = table([(rich_cell, cell("£7.5m"), cell("7"), cell("20"), cell("12%"))])

    inspections = inspect_review_table_structure(snapshot, 4)

    assert len(inspections) == 1
    assert inspections[0].total_header_count == 5
    assert inspections[0].sample_rows[0].cell_count == 5
    assert inspections[0].sample_rows[0].cells[0].tag_name == "td"
    assert inspections[0].sample_rows[0].cells[0].display == "table-cell"


@pytest.mark.parametrize("points", ["", "not-points", "NaN", "Infinity"])
def test_malformed_projection_fails_closed(points: str) -> None:
    snapshot = table([(cell("Player 1"), cell("T1"), cell(points))])

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert raised.value.category == "invalid_projection_table"


def test_identity_resolution_is_exact_after_unicode_and_whitespace_normalization() -> None:
    players = (FplPlayer(1, "João Pedro", 1, Decimal("20")),)
    snapshot = table([(cell("  João   Pedro  \nT1"), cell("T1"), cell("7.0"))])

    result = extract_review_projections(snapshot, 4, players, official_teams())

    assert result[0].element_id == 1


def test_zero_identity_matches_fails_closed() -> None:
    snapshot = table([(cell("Unknown"), cell("T1"), cell("7.0"))])

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, official_players(), official_teams())

    assert raised.value.category == "identity_resolution_failed"


def test_multiple_exact_identity_matches_fails_closed() -> None:
    players = (
        FplPlayer(1, "Same Name", 1, Decimal("20")),
        FplPlayer(2, "Same Name", 1, Decimal("5")),
    )
    snapshot = table([(cell("Same Name"), cell("T1"), cell("7.0"))])

    with pytest.raises(CaptainReviewBrowserError) as raised:
        extract_review_projections(snapshot, 4, players, official_teams())

    assert raised.value.category == "identity_resolution_failed"


def test_identity_diagnostics_collect_every_failure_without_partial_success() -> None:
    players = (
        FplPlayer(1, "Alpha", 1, Decimal("20")),
        FplPlayer(2, "Beta", 2, Decimal("20")),
        FplPlayer(3, "Gamma", 3, Decimal("20")),
        FplPlayer(4, "Duplicate", 4, Decimal("20")),
        FplPlayer(5, "Duplicate", 4, Decimal("5")),
        FplPlayer(6, "Resolved", 5, Decimal("20")),
    )
    snapshot = table(
        [
            (cell("Resolved"), cell("T5"), cell("9.0")),
            (cell("Missing"), cell("T1"), cell("8.0")),
            (cell("Beta"), cell("T1"), cell("7.0")),
            (cell("Alpha"), cell("ZZZ"), cell("6.0")),
            (cell("Duplicate"), cell("T4"), cell("5.0")),
        ]
    )

    summary = collect_review_identity_diagnostics(
        snapshot,
        4,
        players,
        official_teams(),
        acquired_at_utc=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert summary.total_projection_rows == 5
    assert summary.resolved_candidate_count == 1
    assert [failure.source_row_ordinal for failure in summary.failures] == [2, 3, 4, 5]
    assert [failure.category for failure in summary.failures] == [
        "zero_exact_match",
        "exact_name_wrong_team",
        "unknown_team_code",
        "duplicate_exact_match",
    ]
    assert summary.failures[0].exact_name_exists is False
    assert summary.failures[0].review_team_code_exists is True
    assert [
        (item.web_name, item.element_id) for item in summary.failures[0].same_team_official_players
    ] == [("Alpha", 1)]
    assert [
        (item.team_short_code, item.element_id)
        for item in summary.failures[1].exact_name_official_matches
    ] == [("T2", 2)]
    assert summary.failures[2].review_team_code_exists is False
    assert summary.failures[3].exact_official_match_count == 2


def test_source_preserves_all_public_identity_diagnostics_while_failing_closed() -> None:
    players = (
        FplPlayer(1, "Known", 1, Decimal("20")),
        FplPlayer(2, "Other", 2, Decimal("20")),
    )
    source = FplReviewBrowserProjectionSource(
        StaticAcquirer(
            table(
                [
                    (cell("Known"), cell("T1"), cell("8")),
                    (cell("Unknown A"), cell("T1"), cell("7")),
                    (cell("Unknown B"), cell("T2"), cell("6")),
                ]
            )
        ),
        players,
        official_teams(),
        clock=lambda: datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        source.fetch_event_projections(4)

    assert raised.value.category == "identity_resolution_failed"
    assert source.last_projections is None
    assert source.last_identity_diagnostic is not None
    assert source.last_identity_diagnostic.total_projection_rows == 3
    assert source.last_identity_diagnostic.resolved_candidate_count == 1
    assert len(source.last_identity_diagnostic.failures) == 2


def test_ordinary_and_diagnostic_paths_consume_same_canonical_parser(monkeypatch) -> None:
    snapshot = table(projection_rows(2))
    canonical = parse_review_projection_table(snapshot, 4)
    calls: list[int] = []

    def canonical_parser(page_snapshot, event_id):
        assert page_snapshot is snapshot
        calls.append(event_id)
        return canonical

    monkeypatch.setattr(review_browser, "parse_review_projection_table", canonical_parser)

    extract_review_projections(snapshot, 4, official_players(), official_teams())
    summary = collect_review_identity_diagnostics(
        snapshot,
        4,
        official_players(),
        official_teams(),
        acquired_at_utc=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert calls == [4, 4]
    assert summary.total_projection_rows == len(canonical.rows)


def test_source_records_local_acquisition_time_not_dataset_generation_time() -> None:
    acquired = datetime(2026, 9, 6, 11, 30, tzinfo=UTC)
    acquirer = StaticAcquirer(table(projection_rows(3)))
    source = FplReviewBrowserProjectionSource(
        acquirer,
        official_players(),
        official_teams(),
        clock=lambda: acquired,
    )

    source.fetch_event_projections(4)

    assert acquirer.calls == [4]
    assert source.last_acquisition is not None
    assert source.last_acquisition.acquired_at_utc == acquired
    assert source.last_acquisition.total_projection_rows == 3
    assert source.last_acquisition.resolved_candidate_count == 3


@pytest.mark.parametrize(
    ("snapshot", "category"),
    [
        (ReviewPageSnapshot((), login_required=True), "reauthentication_required"),
        (ReviewPageSnapshot((), premium_unavailable=True), "premium_unavailable"),
    ],
)
def test_authentication_and_premium_failures_are_typed(
    snapshot: ReviewPageSnapshot,
    category: str,
) -> None:
    source = FplReviewBrowserProjectionSource(
        StaticAcquirer(snapshot), official_players(), official_teams()
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        source.fetch_event_projections(4)

    assert raised.value.category == category


def test_table_timeout_is_safely_propagated() -> None:
    source = FplReviewBrowserProjectionSource(
        FailingAcquirer("table_load_timeout"), official_players(), official_teams()
    )

    with pytest.raises(CaptainReviewBrowserError) as raised:
        source.fetch_event_projections(4)

    assert raised.value.category == "table_load_timeout"


def test_unmarked_existing_profile_is_never_adopted(tmp_path: Path) -> None:
    profile = tmp_path / "existing-browser-profile"
    profile.mkdir()
    (profile / "existing-state").write_text("not inspected", encoding="utf-8")

    with pytest.raises(CaptainReviewBrowserError) as raised:
        prepare_dedicated_profile(profile)

    assert raised.value.category == "dedicated_profile_required"


def test_marked_dedicated_profile_is_required(tmp_path: Path) -> None:
    profile = tmp_path / "captain-profile"

    assert prepare_dedicated_profile(profile) == profile.resolve()
    assert (profile / DEDICATED_PROFILE_MARKER).is_file()
    assert require_dedicated_profile(profile) == profile.resolve()


def test_legacy_dedicated_marker_is_upgraded_for_stable_chrome(tmp_path: Path) -> None:
    profile = tmp_path / "captain-profile"
    profile.mkdir()
    marker = profile / DEDICATED_PROFILE_MARKER
    marker.write_text("Dedicated FPL Review Captain browser profile.\n", encoding="utf-8")

    prepare_dedicated_profile(profile)

    marker_text = marker.read_text(encoding="utf-8")
    assert "browser_channel=chrome" in marker_text
    backup = tmp_path / "captain-profile.playwright-chromium-backup"
    assert backup.is_dir()
    assert (backup / DEDICATED_PROFILE_MARKER).is_file()


def test_repository_local_profile_is_rejected_even_if_name_is_ignored() -> None:
    repository_profile = Path(__file__).parents[1] / ".captain-browser-profile"

    with pytest.raises(CaptainReviewBrowserError) as raised:
        prepare_dedicated_profile(repository_profile)

    assert raised.value.category == "dedicated_profile_required"


def test_ordinary_default_chrome_profile_is_always_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ordinary_profile = tmp_path / "Google" / "Chrome" / "User Data"

    with pytest.raises(CaptainReviewBrowserError) as raised:
        prepare_dedicated_profile(ordinary_profile)

    assert raised.value.category == "dedicated_profile_required"


def test_stable_chrome_login_command_uses_only_dedicated_profile(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    profile = tmp_path / "captain-profile"

    command = stable_chrome_login_command(chrome, profile)

    assert command[0] == str(chrome)
    assert f"--user-data-dir={profile}" in command
    assert not any("--profile-directory" in argument for argument in command)
    assert not any("remote-debugging" in argument for argument in command)
    assert not any("Google\\Chrome\\User Data" in argument for argument in command)


def test_manual_bootstrap_launches_stable_chrome_and_waits_for_clean_close(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "captain-profile"
    chrome = tmp_path / "chrome.exe"
    chrome.touch()
    process = FakeProcess()
    captured: list[tuple[str, ...]] = []

    def launch(command):
        captured.append(tuple(command))
        return process

    open_manual_review_login(profile, chrome_executable=chrome, process_factory=launch)

    assert process.wait_calls == 1
    assert captured == [stable_chrome_login_command(chrome, profile.resolve())]
    assert require_dedicated_profile(profile) == profile.resolve()


def test_missing_stable_chrome_fails_with_safe_typed_error(tmp_path: Path) -> None:
    with pytest.raises(CaptainReviewBrowserError) as raised:
        find_stable_chrome_executable((tmp_path / "missing-chrome.exe",))

    assert raised.value.category == "stable_chrome_unavailable"


def test_automated_acquisition_uses_compatible_stable_chrome_channel(tmp_path: Path) -> None:
    chromium = FakeChromium()

    context = _launch_stable_chrome_context(chromium, tmp_path, headless=True)

    assert context is chromium.context
    assert chromium.kwargs == {
        "user_data_dir": str(tmp_path),
        "channel": PLAYWRIGHT_CHROME_CHANNEL,
        "headless": True,
        "viewport": {"width": 1440, "height": 1000},
    }


def test_entire_table_is_ranked_and_differential_can_be_rank_26() -> None:
    players = list(official_players(30, ownership="50"))
    players[25] = FplPlayer(26, "Player 26", 6, Decimal("9.99"))
    source = FplReviewBrowserProjectionSource(
        StaticAcquirer(table(projection_rows(30))),
        players,
        official_teams(),
    )
    teams = official_teams()
    fixtures = tuple(Fixture(index, 4, index * 2 - 1, index * 2) for index in range(1, 11))
    event = FplEvent(4, "Gameweek 4", datetime(2026, 9, 12, 17, 30, tzinfo=UTC), False, True)
    report: EventReport = build_event_report(event, teams, fixtures)

    result = build_captain_report(report, source, players, teams, fixtures)

    assert result.differential.player.element_id == 26
    ranked = sorted(
        source.last_projections or (),
        key=lambda item: item.projected_points,
        reverse=True,
    )
    assert [item.element_id for item in ranked].index(26) + 1 == 26
    assert source.last_acquisition is not None
    assert source.last_acquisition.total_projection_rows == 30


def test_no_differential_in_complete_available_table_fails_closed() -> None:
    players = official_players(30, ownership="10.0")
    source = FplReviewBrowserProjectionSource(
        StaticAcquirer(table(projection_rows(30))), players, official_teams()
    )
    teams = official_teams()
    fixtures = tuple(Fixture(index, 4, index * 2 - 1, index * 2) for index in range(1, 11))
    event = FplEvent(4, "Gameweek 4", datetime(2026, 9, 12, 17, 30, tzinfo=UTC), False, True)
    report = build_event_report(event, teams, fixtures)

    with pytest.raises(CaptainProjectionError, match="no qualifying differential"):
        build_captain_report(report, source, players, teams, fixtures)


def test_browser_source_contains_no_session_export_or_production_capability() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "fpl_bot" / "captain_review_browser.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "fplreview_session",
        ".cookies(",
        "storage_state(",
        "XApiClient",
        "CloudTasksClient",
        "Firestore",
        "SecretManager",
        "create_text_post",
        "POST /2/tweets",
        "scheduler",
    )

    assert all(item not in source for item in forbidden)


def test_browser_profile_patterns_are_excluded_from_git_and_build_context() -> None:
    repository = Path(__file__).parents[1]
    for filename in (".gitignore", ".dockerignore"):
        content = (repository / filename).read_text(encoding="utf-8")
        assert ".captain-browser-profile/" in content
        assert "captain-browser-profile/" in content
