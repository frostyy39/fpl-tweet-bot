# Captain production X onboarding

This milestone creates a second, disabled Captain publisher boundary for the eventual real X
account. It does not arm Captain, change Good Luck, reuse a rehearsal candidate or create an X
post.

## Authority layout

The two account grants are deliberately separate:

| Environment | OAuth metadata database | Token-state secret | Consumers |
| --- | --- | --- | --- |
| FPLBotTest | `shared-x-oauth` | `x-oauth-token-state` | current Good Luck and disabled test Captain publisher |
| Production | `production-shared-x-oauth` | `production-x-oauth-token-state` | disabled production Captain publisher; future production Good Luck |

Both use the schema-2 crash/ambiguity-safe coordinator implementation. The production grant is one
shared authority for its eventual Captain and Good Luck consumers; it is not split into two rotating
refresh-token copies. Static OAuth application credentials may be shared by both grants, but their
account access/refresh tokens, Firestore authority documents and Secret Manager token-state secrets
are never copied or shared.

The production metadata database is a delete-protected Native-mode database in `europe-west2`.
The production publisher is a separate scale-to-zero service in `europe-west1`, where fresh FPL
access is proven. Its identity has conditioned Datastore access only to `captain-state` and
`production-shared-x-oauth`, token access only to the production token-state secret, and read access
to the static OAuth client credentials. It has no `(default)`, `shared-x-oauth`, test token-secret,
Compute, Windows-worker or Good Luck authority.

`src/fpl_bot/production_x_identity.py` intentionally contains no configured user ID before human
authorization. The production runtime and deployment script both refuse to start in that state.
After read-only `/2/users/me` verifies the intended account, the numeric ID is committed as the
source-level immutable destination guard and must exactly match the deployment environment.

## Human no-post authorization boundary

Use the existing retained OAuth Client ID and current-user-DPAPI encrypted Client Secret outside the
repository. Do not print or paste either secret into a command. Choose a new, non-existing output
path outside the repository, then run:

```powershell
.\deploy\authorize-x-production-account.ps1 `
  -ClientIdPath "<absolute external path to retained client-id file>" `
  -EncryptedClientSecretPath "<absolute external path to DPAPI client-secret file>" `
  -TokenOutputPath "<new absolute external path to production tokens.dpapi>"
```

Before approving, verify the browser is signed into the intended real production X account. The
helper binds only to `127.0.0.1:8765/callback`, validates OAuth state, requests the reviewed scopes,
exchanges the code, and calls only `/2/users/me`. It never creates a post. It writes the complete
token handoff only through current-user DPAPI and exclusive file creation outside the repository.

Record only these safe output fields:

- `authorization_succeeded`;
- `verified_user_id`;
- `required_scopes_present`;
- `refresh_token_present`;
- `token_type`;
- `access_token_expires_at_utc`;
- `new_dpapi_handoff_saved`.

Do not continue if the displayed username/account or numeric ID is unexpected. Do not bootstrap the
file under an assumed ID.

## Reviewed bootstrap and disabled deployment

After committing the verified ID, initialize the empty production authority exactly once:

```powershell
fpl-bot-x-bootstrap `
  --project-id=fpl-frosty-bot-v1 `
  --project-number=524790767721 `
  --database-id=production-shared-x-oauth `
  --secret-id=production-x-oauth-token-state `
  --expected-user-id=<reviewed numeric production ID> `
  --token-file="<external production tokens.dpapi path>"
```

This creates one token-secret version before transactionally creating schema-2 authority revision 1.
Firestore stores only audit metadata and the exact Secret Manager version pointer. The command does
not refresh, call X or post. Existing authority, mismatched identity, orphaned candidate versions,
uncertainty or malformed input fail closed.

The one-shot verifier may then call `/2/users/me` through the production database/secret while
`X_POSTING_ENABLED=false`. It may refresh only if the access token genuinely requires it, using the
same schema-2 coordinator. Finally, a commit-tagged production publisher image is deployed with
`deploy/deploy-captain-production-publisher.ps1`. The script reads the committed identity rather
than accepting a destination parameter, deploys privately with `X_POSTING_ENABLED=false`, and adds
only `captain-prod-pub-invoker` as invoker. No Scheduler or Cloud Task is created.

## Future Good Luck production migration

Good Luck remains on FPLBotTest in this milestone. Before GW6 production promotion it needs a
separate quiet-window review that:

1. verifies the production authority is idle/current and the test authority remains healthy;
2. grants the Good Luck runtime database-scoped access to `production-shared-x-oauth` and
   secret-scoped access to `production-x-oauth-token-state`;
3. deploys Good Luck with business state still `(default)`, but production OAuth database/secret,
   `X_ENVIRONMENT=production` and the same immutable production numeric user ID;
4. performs read-only `/2/users/me` through the shared production authority;
5. proves Captain/Good Luck refresh contention uses one schema-2 authority;
6. restores the exact Scheduler/queue configuration without creating an early task or X post.

The test authority is not migrated, overwritten or used as fallback. Production Captain and Good
Luck must dynamically follow the production authority's current Secret Manager version; neither may
pin or duplicate the rotating refresh token.
