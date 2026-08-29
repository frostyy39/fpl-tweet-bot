import pytest

from fpl_bot.tweet import render_v1_tweet


def test_exact_v1_tweet_rendering() -> None:
    assert render_v1_tweet("BDGW37") == ("Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #BDGW37")


@pytest.mark.parametrize("event_code", ["", "GW", "GW0", "gameweek3", "#GW3"])
def test_tweet_renderer_rejects_invalid_event_code(event_code: str) -> None:
    with pytest.raises(ValueError, match="event_code"):
        render_v1_tweet(event_code)
