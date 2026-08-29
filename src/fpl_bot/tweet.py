"""Exact, deterministic rendering for the single V1 tweet."""

import re

EVENT_CODE_PATTERN = re.compile(r"(?:GW|BGW|DGW|BDGW)[1-9]\d*\Z")


def render_v1_tweet(event_code: str) -> str:
    if not EVENT_CODE_PATTERN.fullmatch(event_code):
        raise ValueError("event_code must look like GW3, BGW29, DGW34, or BDGW37")
    return f"Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #{event_code}"
