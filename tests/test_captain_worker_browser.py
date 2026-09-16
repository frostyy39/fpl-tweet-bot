import ast
from pathlib import Path

import pytest

import fpl_bot.captain_worker_browser as bridge
from fpl_bot.captain_review_browser import ReviewDomCell, ReviewDomTable, ReviewPageSnapshot
from fpl_bot.errors import CaptainReviewBrowserError


@pytest.mark.parametrize(
    "ownership,points,success",
    [
        ("released", "6.48", True),
        ("unclean", "6.48", False),
        ("released", "invalid", False),
    ],
)
def test_bridge_reuses_closed_browser_and_canonical_parser(monkeypatch, ownership, points, success):
    events = []

    class FakeAcquirer:
        def __init__(self, path, *, session_observer):
            assert path == Path("dedicated")
            self.last_lifecycle = {"authentication": "authenticated", "ownership": ownership}

        def acquire(self, event_id):
            assert event_id == 5
            events.append("closed")
            return ReviewPageSnapshot(
                (
                    ReviewDomTable(
                        header_rows=(
                            tuple(
                                ReviewDomCell(s)
                                for s in ("PLAYER", "PRICE", "GW5", "TOTAL", "ELITE OWN%")
                            ),
                        ),
                        body_rows=(
                            tuple(
                                ReviewDomCell(s)
                                for s in (
                                    "João Pedro\nCHE • FWD",
                                    "unused",
                                    points,
                                    "unused",
                                    "unused",
                                )
                            ),
                        ),
                    ),
                )
            )

    monkeypatch.setattr(bridge, "PlaywrightReviewBrowserAcquirer", FakeAcquirer)
    source = bridge.ProvenBrowserAcquisition(Path("dedicated"))
    if success:
        data = source.acquire(5)
        assert data.records[0].review_name == "João Pedro"
        assert data.records[0].source_ordinal == 1
        assert data.records[0].official_element_id is None
        assert data.counts.extracted == 1
        assert data.cleanup.value == "released"
    else:
        with pytest.raises(CaptainReviewBrowserError):
            source.acquire(5)
    assert events == ["closed"]


def test_worker_import_graph_has_no_posting_or_cloud_adapter():
    root = Path(__file__).parents[1] / "src" / "fpl_bot"
    pending = ["captain_worker", "captain_worker_browser"]
    seen = set()
    forbidden = {
        "captain_worker_trial",
        "captain_service",
        "x_client",
        "x_oauth",
        "firestore",
        "cloud_tasks",
        "deadline_service",
    }
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        assert module not in forbidden
        source = (root / f"{module}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("google.cloud", "google.auth"))
                if node.module.startswith("fpl_bot."):
                    pending.append(node.module.split(".")[1])
        if module in {"captain_worker", "captain_worker_browser", "captain_session_health"}:
            for forbidden_api in (".cookies(", "storage_state(", "add_cookies("):
                assert forbidden_api not in source
