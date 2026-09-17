from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import PermissionDenied
from test_captain_firestore import Client, Collection, Reference, Snapshot

from fpl_bot.captain_persistence_probe import NAME, PROJECT, run_probe


class ProbeClient(Client):
    project = PROJECT
    _database_string = NAME

    def collection(self, name):
        assert name.startswith("captain_")
        client = self

        class ProbeReference(Reference):
            def get(self, *, transaction=None):
                if transaction is not None:
                    return super().get(transaction=transaction)
                return Snapshot(self.name, client.data.get(self.name))

        class ProbeCollection(Collection):
            def document(self, name):
                return ProbeReference(f"{self.name}/{name}")

        return ProbeCollection(name)


class Admin:
    def __init__(self, deny=True):
        self.calls, self.deny = [], deny

    def get_database(self, *, request, retry, timeout):
        assert retry is None and timeout == 10
        self.calls.append(request["name"])
        if request["name"].endswith("/(default)") and self.deny:
            raise PermissionDenied("denied")
        return SimpleNamespace()


def test_probe_read_only_domain_operations_and_isolated_transaction_replay():
    client, admin = ProbeClient(), Admin()
    result = run_probe(
        client, admin, datetime(2026, 9, 17, tzinfo=UTC), transactional=client.transactional
    )
    assert result["default_database_metadata_denied"] is True
    assert result["postable"] is False and result["pending_intents"] == 0
    assert len(client.data) == 1
    assert all(name.startswith("captain_integration_probes/") for name in client.data)
    assert list(client.versions.values()) == [1]
    assert admin.calls == [NAME, f"projects/{PROJECT}/databases/(default)"]


def test_unexpected_default_database_permission_fails_before_persistence():
    client = ProbeClient()
    with pytest.raises(ValueError, match="isolation"):
        run_probe(
            client,
            Admin(deny=False),
            datetime(2026, 9, 17, tzinfo=UTC),
            transactional=client.transactional,
        )
    assert client.data == {}


def test_probe_refuses_wrong_database_without_calls():
    client, admin = ProbeClient(), Admin()
    client._database_string = "projects/fpl-frosty-bot-v1/databases/(default)"
    with pytest.raises(ValueError):
        run_probe(
            client, admin, datetime(2026, 9, 17, tzinfo=UTC), transactional=client.transactional
        )
    assert admin.calls == [] and client.data == {}
