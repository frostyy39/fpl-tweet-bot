"""Production Captain controller composition; routes only immutable candidate evidence."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from fpl_bot.api import FplApiClient
from fpl_bot.captain_cloud_runtime import UtcClock, compose
from fpl_bot.captain_cloud_tasks import CaptainCloudTasksAdapter, CaptainCloudTasksConfig
from fpl_bot.captain_compute import CaptainComputeAdapter, ComputeTarget
from fpl_bot.captain_firestore import FirestoreCaptainRepository, FirestoreCaptainVmOperations
from fpl_bot.captain_http import GoogleOidcAuthorizer, default_google_token_verifier
from fpl_bot.captain_publication_tasks import (
    CaptainPublicationTasksAdapter,
    CaptainPublicationTasksConfig,
)
from fpl_bot.production_x_identity import PRODUCTION_X_USER_ID


@dataclass(frozen=True, slots=True)
class ProductionCaptainControllerConfig:
    project: str
    database: str
    origin: str
    worker_email: str
    tasks_email: str
    planner_email: str
    destination_user_id: str
    publisher_origin: str
    publisher_invoker_email: str

    def __post_init__(self) -> None:
        expected_project = "fpl-frosty-bot-v1"
        expected_origin = "https://captain-controller-524790767721.europe-west1.run.app"
        expected_publisher = (
            "https://captain-production-publisher-524790767721.europe-west1.run.app"
        )
        if (
            self.project != expected_project
            or self.database != "captain-state"
            or self.origin != expected_origin
            or self.publisher_origin != expected_publisher
            or self.destination_user_id != PRODUCTION_X_USER_ID
            or self.destination_user_id is None
        ):
            raise ValueError("invalid production Captain controller boundary")
        expected = {
            f"captain-worker@{self.project}.iam.gserviceaccount.com",
            f"captain-tasks@{self.project}.iam.gserviceaccount.com",
            f"captain-planner@{self.project}.iam.gserviceaccount.com",
        }
        if {self.worker_email, self.tasks_email, self.planner_email} != expected:
            raise ValueError("production Captain controller identities are fixed")
        if self.publisher_invoker_email != (
            f"captain-prod-pub-invoker@{self.project}.iam.gserviceaccount.com"
        ):
            raise ValueError("production publisher invoker identity is fixed")
        if any(urlsplit(value).path for value in (self.origin, self.publisher_origin)):
            raise ValueError("controller and publisher origins cannot contain routes")

    @classmethod
    def environment(cls, environ=None) -> "ProductionCaptainControllerConfig":
        source = os.environ if environ is None else environ
        names = (
            "PROJECT",
            "DATABASE",
            "ORIGIN",
            "WORKER_EMAIL",
            "TASKS_EMAIL",
            "PLANNER_EMAIL",
            "DESTINATION_USER_ID",
            "PUBLISHER_ORIGIN",
            "PUBLISHER_INVOKER_EMAIL",
        )
        try:
            return cls(*(source["CAPTAIN_" + name] for name in names))
        except KeyError as exc:
            raise ValueError("production Captain controller configuration is incomplete") from exc


def create_app():
    config = ProductionCaptainControllerConfig.environment()
    kwargs = {"project": config.project, "database": config.database}
    repository = FirestoreCaptainRepository(**kwargs)
    operations = FirestoreCaptainVmOperations(**kwargs)
    scheduler = CaptainCloudTasksAdapter(
        CaptainCloudTasksConfig(
            config.project,
            "europe-west2",
            "captain-orchestration",
            config.origin,
            config.tasks_email,
            config.origin,
        )
    )
    publication_router = CaptainPublicationTasksAdapter(
        CaptainPublicationTasksConfig(
            config.project,
            "europe-west2",
            "captain-orchestration",
            config.publisher_origin,
            config.publisher_invoker_email,
            config.publisher_origin,
            config.destination_user_id,
        )
    )
    provider = CaptainComputeAdapter.from_default_credentials(
        ComputeTarget(
            config.project,
            "europe-west2-b",
            "captain-browser-london-windows-trial",
        )
    )
    return compose(
        config,
        repository,
        operations,
        FplApiClient(),
        UtcClock(),
        scheduler,
        provider,
        GoogleOidcAuthorizer(default_google_token_verifier),
        publisher_router=publication_router,
    )
