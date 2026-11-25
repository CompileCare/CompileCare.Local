# CompileCare Local

One-click local development environment for the CompileCare hospital platform.

This repository orchestrates the API, Web application, PostgreSQL and a preconfigured local Keycloak identity provider in Docker Compose. Hospital domain code remains in the API and Web repositories.

## Supported platforms

- macOS: double-click **Start CompileCare.command**.
- Windows 10/11: double-click **Start CompileCare.bat**.

## Prerequisites

1. Docker Desktop installed and running.
2. GitHub CLI installed and authenticated to the CompileCare organization (`gh auth login`) to clone the private Web and API repositories.
3. A GitHub Personal Access Token with `read:packages` permission for the private CompileCare NuGet feed.

Do not use production credentials or real patient data in local development.

## First launch

The launcher clones `CompileCare.Api` and `CompileCare.Web` beside this repository when they are not already present. It creates `local/.env` from the sample and asks for local OIDC settings and a GitHub Packages token. Secrets stay in ignored local files.

The local Keycloak realm is imported automatically on first startup:

- Realm: `compilecare`
- Authority/issuer: `http://keycloak.localhost:8082/realms/compilecare`
- Web client: `compilecare-web` (public client, Authorization Code flow, PKCE S256)
- API audience: `compilecare-api` (added to access tokens by the default `compilecare-api-audience` client scope)
- Redirect URI: `http://localhost:8080/auth/callback`
- Post-logout URI and Web origin: `http://localhost:8080`

The `.localhost` hostname resolves to loopback in the browser and is also registered as a Docker network alias, allowing the browser and API container to use the same issuer URL. The local API explicitly opts into HTTP metadata only in Development.

The local Keycloak bootstrap admin defaults are stored in the ignored `local/.env`. Change `KEYCLOAK_ADMIN_PASSWORD` before first startup. After the admin console is available, create a permanent administrator in the master realm and verify that account before deleting the temporary bootstrap administrator. Do not reuse these credentials outside local development.

Keycloak imports the realm only when its data volume is first initialized. To intentionally reset the local identity provider and re-import the realm (this deletes local Keycloak users and configuration):

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

## Local observability (optional)

The API emits structured console logs, exposes /metrics and is instrumented for OpenTelemetry. To start the optional collector, Prometheus, Jaeger, Elasticsearch and Kibana services, use the [local observability runbook](engineering/local-observability.md):

```bash
docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml up --build -d
```

Dashboards: Kibana at http://localhost:5601 (logs), Prometheus at http://localhost:9090 (metrics), and Jaeger at http://localhost:16686 (traces). This overlay is local-only and must use synthetic data.

## Stop

Use **Stop CompileCare.command** or **Stop CompileCare.bat**. PostgreSQL and Keycloak volumes are retained. To intentionally delete the local database and identity provider:

```bash
docker compose --env-file local/.env -f local/docker-compose.yml down --volumes
```

## Security

- `local/.env` and `local/.secrets/` are ignored.
- NuGet credentials are passed to Docker BuildKit as a secret, never a Docker ARG or image layer.
- Database and service ports bind to loopback only.
- Keycloak is local-development-only and has no production data or external exposure.
- API authentication, validated issuer/audience, persisted tenant membership and authorization remain enabled.
- No bypass login or automatic platform-operator bootstrap is provided.
