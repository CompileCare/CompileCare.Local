# HeapCare Local

> **Migration status:** HeapCare branding, local checkout directories, launcher names, container identities and HEAPCARE_* path variables are in place. Legacy COMPILECARE_* path variables are still accepted by the launchers. The Keycloak realm display name/import filename now use HeapCare; the existing realm/client/audience IDs, database names, legacy connection-string fallback and persistent Docker volume names are intentionally retained for compatibility.

One-click local development environment for the HeapCare hospital platform.

This repository orchestrates the API, Web application, PostgreSQL and a preconfigured local Keycloak identity provider in Docker Compose. Hospital domain code remains in the API and Web repositories.

## Supported platforms

- macOS: double-click `local/Start HeapCare.command` to clone/update the application checkouts, build images and start the full application plus observability stack.
- Windows 10/11: double-click `local/Start HeapCare.bat` to clone missing application checkouts, build images and start the same nine-service application and observability stack.

## Prerequisites

1. Docker Desktop installed and running.
2. Git and GitHub CLI installed and authenticated (`gh auth login`) with access to the CompileCare organization repositories.
3. The GitHub CLI token must have `read:packages` access for the private GitHub Packages feed under the current CompileCare organization owner. The launcher requests this scope and stores the resulting credentials in the ignored `local/.secrets/` directory.

Do not use production credentials or real patient data in local development.

## First launch

Both launchers clone the GitHub repositories currently named `CompileCare.Api` and `CompileCare.Web` into adjacent local folders named `HeapCare.Api` and `HeapCare.Web` when missing. Existing adjacent `CompileCare.Api` / `CompileCare.Web` checkouts are detected and reused without renaming or overwriting them. The macOS launcher additionally requires existing checkouts to be clean and on `main`, then fetches and fast-forward updates them only; it never force-resets, switches branches or discards local changes. On first run either launcher creates `local/.env` with random local-only Keycloak/PostgreSQL credentials and a one-time platform bootstrap secret. The macOS launcher obtains NuGet package credentials from the authenticated GitHub CLI token; the Windows launcher prompts for a GitHub username and a `read:packages` PAT and stores it in the ignored `local/.secrets/` directory. Secrets stay in ignored local files.

The local Keycloak realm is imported automatically on first startup:

- Realm: `compilecare`
- Authority/issuer: `http://keycloak.localhost:8082/realms/compilecare`
- Web client: `compilecare-web` (public client, Authorization Code flow, PKCE S256)
- API audience: `compilecare-api` (added to access tokens by the default `compilecare-api-audience` client scope)
- User subject: the Keycloak `basic` default client scope supplies the `sub` claim in access tokens. The local reconciler verifies the `oidc-sub-mapper` is enabled for access tokens; this is required because the API binds persisted tenant membership to the validated issuer and subject.
- Sign-in redirect URI: `http://localhost:8080/auth/callback`
- Silent-renew redirect URI: `http://localhost:8080/auth/silent-callback`
- Post-logout URI and Web origin: `http://localhost:8080`

The `.localhost` hostname resolves to loopback in the browser and is also registered as a Docker network alias, allowing the browser and API container to use the same issuer URL. The local API explicitly opts into HTTP metadata only in Development.

On a fresh setup, the launcher generates independent random 256-bit Keycloak admin and PostgreSQL passwords in the ignored `local/.env` before startup. Existing `.env` credentials are preserved to avoid silently desynchronizing persistent Docker volumes. A legacy PostgreSQL sample password triggers a warning; the known Keycloak sample password is rejected because the realm reconciliation tool refuses it. For an existing Keycloak volume, change the admin password in the running Keycloak console first, then update `local/.env`. For PostgreSQL, change the `compilecare` database role password before updating `local/.env` and restarting the API. The local Keycloak admin username defaults to `admin`; these credentials are local-only and must never be reused elsewhere. After the admin console is available, create a permanent administrator in the master realm and verify that account before deleting the temporary bootstrap administrator. Do not reuse these credentials outside local development.

Keycloak imports the realm only when its data volume is first initialized. When the checked-in realm definition changes, reconcile the existing local client without deleting users or resetting the volume:

```bash
python3 local/scripts/configure-keycloak.py local/.env
```

This command idempotently updates the `compilecare-web` redirect URIs, web origins and default client scopes. To intentionally reset the local identity provider and re-import the realm (this deletes local Keycloak users and configuration):

```bash
docker compose --env-file local/.env -f local/docker-compose.yml down
docker volume rm compilecare-local_compilecare-keycloak
```

The launcher automatically removes the earlier standalone `compilecare-keycloak` container (including a stopped container) before starting the Compose-managed service. It does not remove the Compose-managed container. This avoids a container-name conflict on port 8082 during the migration from the temporary manual setup.

## Local endpoints

| Service | URL |
|---|---|
| Web | http://localhost:8080 |
| API | http://localhost:8081 |
| API OpenAPI (Development) | http://localhost:8081/openapi/v1.json |
| Keycloak | http://keycloak.localhost:8082 |
| Keycloak admin console | http://keycloak.localhost:8082/admin |
| PostgreSQL | localhost:5432 (loopback only) |

The API runs in Development with migrations explicitly enabled by this local Compose stack. This opt-in must not be used in Staging or Production. Normal deployments retain the default migration behavior.

## Local observability (included in both one-click launchers)

Both launchers start the OpenTelemetry Collector, Prometheus, Jaeger, Elasticsearch and Kibana alongside the application. They validate the merged Compose configuration, pull runtime images, build the API and Web images, start dependencies, reconcile the local Keycloak client and wait for the application and observability endpoints. The Windows launcher also provisions the Kibana data view and checks PostgreSQL, API OpenAPI and API metrics readiness. The API emits structured console and Elasticsearch logs, exposes `/metrics` and exports OpenTelemetry traces/metrics. For manual startup and signal troubleshooting, use the [local observability runbook](engineering/local-observability.md):

```bash
docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml up --build -d
```

Dashboards: Kibana at http://localhost:5601 (logs), Prometheus at http://localhost:9090 (metrics), and Jaeger at http://localhost:16686 (traces). This overlay is local-only and must use synthetic data.

## Verify the local stack

After the one-click launcher finishes, execute the same infrastructure and observability smoke checks independently if needed:

```bash
bash local/scripts/verify-stack.sh
```

The script checks all nine application and observability services, PostgreSQL readiness, Web/API/OpenAPI, Keycloak OIDC discovery, Prometheus, Jaeger, Elasticsearch, Kibana, Collector metrics and the API metrics endpoint. It never prints credentials or reads patient data. These checks are only an infrastructure smoke test; complete the authenticated acceptance checklist separately with synthetic records. Passing this script does not prove tenant authorization, patient workflows, telemetry delivery or production readiness.

### Real OIDC and persisted multi-tenant acceptance (CI only)

The Local GitHub Actions full-stack job additionally runs `local/scripts/verify-authenticated-tenancy.py` against the isolated nine-service Compose stack. It creates a temporary Keycloak client used only by CI, obtains real Keycloak-issued access tokens, bootstraps a platform operator, creates two tenants and persists different roles for the same synthetic identity. It then verifies API issuer/audience validation, tenant-specific permissions, hospital data isolation, cross-tenant membership mutation denial, membership revocation/reactivation and the current-user tenant-context response against PostgreSQL.

This script requires `COMPILECARE_CI_ACCEPTANCE=1` and is deliberately restricted to the ephemeral CI environment. It uses no patient or billing records, does not change the checked-in `compilecare-web` PKCE client and must not be run against a persistent developer or production environment. The workflow removes its isolated Docker project and volumes after the job.

## Stop

Use `local/Stop HeapCare.command` or `local/Stop HeapCare.bat`. Both stop commands stop the application and observability Compose overlay. PostgreSQL, Keycloak, Prometheus and Elasticsearch volumes are retained. To intentionally delete local data, use the explicit volume-removal command:

```bash
docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml down --volumes
```

## Security

- `local/.env` and `local/.secrets/` are ignored.
- NuGet credentials are passed to Docker BuildKit as a secret, never a Docker ARG or image layer.
- Database and service ports bind to loopback only.
- Keycloak is local-development-only and has no production data or external exposure.
- API authentication, validated issuer/audience, persisted tenant membership and authorization remain enabled.
- No bypass login or automatic platform-operator bootstrap is provided.

## Local Web Content Security Policy

The Web container now enforces CSP. The local Compose configuration explicitly allows the local API origin (`http://localhost:8081`) and Keycloak origin (`http://keycloak.localhost:8082`) for browser connections, and Keycloak for the silent-renew iframe. `ALLOW_INSECURE_CSP_ORIGINS=true` is set only by this local-development Compose file because the local identity/API endpoints use loopback HTTP.

If you override `OIDC_AUTHORITY` or the Web API URL for a different local topology, also set `CSP_CONNECT_SRC` and `CSP_FRAME_SRC` in ignored `local/.env` to the corresponding bare origins (scheme, host and optional port only). Do not copy the local HTTP exception into production. Production Web containers must use HTTPS origins and must not set `ALLOW_INSECURE_CSP_ORIGINS`.


## Cross-repository full-stack verification

The pull-request workflow's Full nine-service Docker stack and API telemetry verification job checks the current main branches of CompileCare.Api, CompileCare.Web and CompileCare.Observability against this local Compose topology. It starts the nine-service stack with ephemeral credentials, validates infrastructure/observability, exercises real Keycloak-issued identities and persisted multi-tenant authorization, and runs the real OIDC Authorization Code + PKCE browser suite. See [the 2026-11-27 verification plan](qa/full-stack-verification-2026-11-27.md) for the scope and acceptance boundary.

A passing hosted CI run is evidence for the exact repository SHAs checked out by that run. It is not a substitute for running the one-click launcher on the target developer Mac/Windows machines, manual accessibility/screen-reader review, provider-native production backup/PITR, or pilot sign-off.


## HeapCare identity migration

HeapCare is the product-facing brand. The local Compose project name, PostgreSQL database/role, Keycloak realm (`compilecare`), OIDC client/audience IDs, connection-string key, persistent volume names and existing `COMPILECARE_*` launcher variables are intentionally retained as compatibility contracts in this phase. Existing local Docker volumes and Keycloak users must not be deleted or silently recreated. A future identity-provider migration must provision the HeapCare realm and clients, migrate users/roles, verify issuer/audience and callback behavior, and only then retire the legacy realm. The current GitHub organization and repository names also remain unchanged until the administrative rename is completed.
