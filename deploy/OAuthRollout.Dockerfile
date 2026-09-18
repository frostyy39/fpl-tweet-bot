# Preserve the exact successful GW5 Good Luck application AND dependency layer.
ARG GOOD_LUCK_BASE=europe-west2-docker.pkg.dev/fpl-frosty-bot-v1/fpl-bot/fpl-bot@sha256:83218c544ccf938550753d27f17d4ffe1f930fd206ddde3456ea9b224577935e
FROM ${GOOD_LUCK_BASE}

# Only shared coordination and privileged read-only/migration tools are overlaid.
COPY --chown=app:app src/fpl_bot/cloud_token_store.py \
    src/fpl_bot/x_token_refresh.py \
    src/fpl_bot/x_errors.py \
    src/fpl_bot/x_oauth_migration.py \
    src/fpl_bot/x_oauth_migration_cli.py \
    src/fpl_bot/x_oauth_rollout_probe.py \
    /usr/local/lib/python3.12/site-packages/fpl_bot/

RUN python -c "from fpl_bot.cloud_token_store import TOKEN_METADATA_SCHEMA_VERSION; import fpl_bot.x_oauth_migration_cli, fpl_bot.x_oauth_rollout_probe; assert TOKEN_METADATA_SCHEMA_VERSION == 2"
