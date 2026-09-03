"""One-shot no-post replacement of an initialized X OAuth token generation."""

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fpl_bot.cloud_token_store import serialize_token_state
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError
from fpl_bot.x_token_bootstrap import ValidatedLocalTokenState
from fpl_bot.x_token_refresh import VersionedXTokenState, XOAuthTokenState


class ReseedTokenStateStore(Protocol):
    def read(self) -> VersionedXTokenState: ...

    def reseed_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool: ...


class XTokenReseedStatus(StrEnum):
    RESEEDED = "reseeded"
    RECONCILED = "reconciled"
    ALREADY_AUTHORITATIVE = "already_authoritative"


@dataclass(frozen=True, slots=True)
class XTokenReseedResult:
    status: XTokenReseedStatus
    previous_revision: str
    authoritative_revision: str


def reseed_x_token_state(
    local_state: ValidatedLocalTokenState,
    store: ReseedTokenStateStore,
    *,
    expected_revision: str,
) -> XTokenReseedResult:
    """Replace one exact authority revision and verify the resulting generation."""

    if not isinstance(local_state, ValidatedLocalTokenState):
        raise XTokenStateError("Local OAuth token state is invalid for reseed")
    expected_number = _parse_revision(expected_revision)
    current = _read(store)
    if _states_match(current.state, local_state.state):
        return XTokenReseedResult(
            XTokenReseedStatus.ALREADY_AUTHORITATIVE,
            current.revision,
            current.revision,
        )
    if current.revision != expected_revision:
        raise XTokenStoreError("OAuth token reseed expected revision is no longer authoritative")

    replaced = store.reseed_if_revision(expected_revision, local_state.state)
    if not isinstance(replaced, bool):
        raise XTokenStoreError("OAuth token reseed compare-and-swap returned an invalid result")
    authoritative = _read(store)
    next_revision = str(expected_number + 1)
    if _states_match(authoritative.state, local_state.state):
        if authoritative.revision != next_revision:
            raise XTokenStoreError("OAuth token reseed authority revision is invalid")
        status = XTokenReseedStatus.RESEEDED if replaced else XTokenReseedStatus.RECONCILED
        return XTokenReseedResult(status, expected_revision, authoritative.revision)
    if replaced:
        raise XTokenStoreError("OAuth token reseed authority could not be verified")
    raise XTokenStoreError("OAuth token reseed lost its authority compare-and-swap")


def _read(store: ReseedTokenStateStore) -> VersionedXTokenState:
    try:
        result = store.read()
    except XTokenStoreError:
        raise
    except Exception:
        raise XTokenStoreError("OAuth token authority could not be read for reseed") from None
    if not isinstance(result, VersionedXTokenState):
        raise XTokenStoreError("OAuth token store returned an invalid reseed snapshot")
    return result


def _parse_revision(value: str) -> int:
    if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
        raise XTokenStateError("OAuth token reseed revision must be a positive integer")
    return int(value)


def _states_match(left: XOAuthTokenState, right: XOAuthTokenState) -> bool:
    return secrets.compare_digest(serialize_token_state(left), serialize_token_state(right))
