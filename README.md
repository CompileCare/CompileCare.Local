# CompileCare Local

One-click local development environment for the CompileCare hospital platform.

This repository orchestrates the API, Web application and PostgreSQL in Docker Compose. It contains developer tooling only; hospital domain code remains in the API and Web repositories.

## Supported platforms

- macOS: double-click **Start CompileCare.command**.
- Windows 10/11: double-click **Start CompileCare.bat**.

## Prerequisites

1. Docker Desktop installed and running.
2. GitHub CLI installed and authenticated to the CompileCare organization (`gh auth login`) to clone the private Web and API repositories.
3. A development OIDC client for the Web app and API.
4. A GitHub Personal Access Token with `read:packages` permission for the private CompileCare NuGet feed.

Do not use production credentials or real patient data in local development.

## First launch

The launcher clones `CompileCare.Api` and `CompileCare.Web` beside this repository when they are not already present. It creates `local/.env` from the sample and asks for development OIDC settings and a GitHub Packages token. Secrets stay in ignored local files.

Configure your identity provider with:
- Authority/issuer URL reachable from the browser and API container.
- API audience (default `compilecare-api`).
- Web client ID (default `compilecare-web`).
- Redirect URI `http://localhost:8080/auth/callback`.
- Post-logout URI `http://localhost:8080`.
- Required API scope.

## Local endpoints

| Service | URL |
|---|---|
| Web | http://localhost:8080 |
| API | http://localhost:8081 |
| API OpenAPI (Development) | http://localhost:8081/openapi/v1.json |
| PostgreSQL | localhost:5432 (loopback only) |

The API runs in Development with migrations explicitly enabled by this local Compose stack. This opt-in must not be used in Staging or Production. Normal deployments retain the default migration behavior.

## Stop

Use **Stop CompileCare.command** or **Stop CompileCare.bat**. The PostgreSQL volume is retained. To intentionally delete the local database:

```bash
docker compose --env-file local/.env -f local/docker-compose.yml down --volumes
```

## Security

- `local/.env` and `local/.secrets/` are ignored.
- NuGet credentials are passed to Docker BuildKit as a secret, never a Docker ARG or image layer.
- Database and service ports bind to loopback only.
- API authentication, validated issuer/audience, persisted tenant membership and authorization remain enabled.
- No bypass login or automatic platform-operator bootstrap is provided.
