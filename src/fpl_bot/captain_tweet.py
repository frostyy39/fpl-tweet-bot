"""Pure canonical Captain post rendering with a deterministic X length guard."""

from decimal import ROUND_HALF_UP, Decimal
from unicodedata import normalize

from fpl_bot.captain_models import CaptainFixture, CaptainSelection
from fpl_bot.errors import CaptainRenderingError
from fpl_bot.tweet import EVENT_CODE_PATTERN

X_POST_WEIGHTED_LIMIT = 280
PROJECTION_QUANTUM = Decimal("0.01")
MEDALS = ("🥇", "🥈", "🥉")


def format_projection(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CaptainRenderingError("Captain projection must be a finite Decimal")
    return str(value.quantize(PROJECTION_QUANTUM, rounding=ROUND_HALF_UP))


def render_fixture(fixture: CaptainFixture) -> str:
    acronym = (
        fixture.opponent.short_name.upper()
        if fixture.is_home
        else fixture.opponent.short_name.lower()
    )
    venue = "H" if fixture.is_home else "a"
    return f"{acronym} ({venue})"


def render_captain_tweet(
    event_code: str,
    top_three: tuple[CaptainSelection, CaptainSelection, CaptainSelection],
    differential: CaptainSelection,
) -> str:
    if not EVENT_CODE_PATTERN.fullmatch(event_code):
        raise CaptainRenderingError("Captain event code is invalid")
    lines = [
        "🧢 CAPTAIN PICKS 🧢",
        "",
        f"#{event_code} Projected Points:",
        "",
    ]
    lines.extend(
        _selection_line(medal, selection)
        for medal, selection in zip(MEDALS, top_three, strict=True)
    )
    lines.extend(
        [
            "",
            "Differential:",
            "",
            _selection_line("🐴", differential),
            "",
            "Good luck!",
            "",
            "#FPL #FPLCommunity",
        ]
    )
    rendered = "\n".join(lines)
    weighted_length = x_weighted_text_length(rendered)
    if weighted_length > X_POST_WEIGHTED_LIMIT:
        raise CaptainRenderingError(
            f"Captain post exceeds the supported X limit of {X_POST_WEIGHTED_LIMIT} characters"
        )
    return rendered


def x_weighted_text_length(text: str) -> int:
    """Apply X's one-or-two weight ranges; Captain posts intentionally contain no URLs."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = normalize("NFC", text)
    return sum(_codepoint_weight(ord(character)) for character in normalized)


def _selection_line(symbol: str, selection: CaptainSelection) -> str:
    if not selection.fixtures:
        raise CaptainRenderingError("Captain selection must contain at least one fixture")
    fixtures = " & ".join(render_fixture(fixture) for fixture in selection.fixtures)
    return (
        f"{symbol} {selection.player.web_name} v {fixtures} - "
        f"{format_projection(selection.projected_points)}"
    )


def _codepoint_weight(codepoint: int) -> int:
    if (
        0x0000 <= codepoint <= 0x10FF
        or 0x2000 <= codepoint <= 0x200D
        or 0x2010 <= codepoint <= 0x201F
        or 0x2032 <= codepoint <= 0x2037
    ):
        return 1
    return 2
