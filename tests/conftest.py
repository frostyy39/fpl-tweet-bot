from fpl_bot.models import Fixture, Team


def make_teams(count: int = 4) -> tuple[Team, ...]:
    return tuple(
        Team(team_id=index, name=f"Team {index}", short_name=f"T{index}")
        for index in range(1, count + 1)
    )


def make_fixture(
    fixture_id: int,
    home_team_id: int,
    away_team_id: int,
    event_id: int = 3,
) -> Fixture:
    return Fixture(
        fixture_id=fixture_id,
        event_id=event_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
