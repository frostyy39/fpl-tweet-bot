# Preserve deployed Good Luck dependency/business layers. Only database wiring
# and metadata-only/read-only operator tools change; no planning/posting modules.
ARG GOOD_LUCK_BASE=europe-west2-docker.pkg.dev/fpl-frosty-bot-v1/fpl-bot/fpl-bot@sha256:b365ecf5e8d24547a411b1ce87448fb67c8a61722d61623fddc028efb09a0c39
FROM ${GOOD_LUCK_BASE}
COPY --chown=app:app src/fpl_bot/runtime_config.py src/fpl_bot/production.py \
    src/fpl_bot/x_oauth_verify.py src/fpl_bot/x_oauth_rollout_probe.py \
    src/fpl_bot/x_oauth_database_migration.py \
    /usr/local/lib/python3.12/site-packages/fpl_bot/
RUN python -c "import fpl_bot.x_oauth_database_migration; from fpl_bot.runtime_config import X_OAUTH_FIRESTORE_DATABASE_ID_VARIABLE; assert X_OAUTH_FIRESTORE_DATABASE_ID_VARIABLE == 'X_OAUTH_FIRESTORE_DATABASE_ID'"
