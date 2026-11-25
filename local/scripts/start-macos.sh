#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL="$ROOT/local"
cd "$ROOT"

fail() { echo "ERROR: $*" >&2; read -r -p "Press Enter to close..."; exit 1; }
command -v docker >/dev/null || fail "Docker Desktop is required. Install from https://www.docker.com/products/docker-desktop/."
docker info >/dev/null 2>&1 || fail "Docker Desktop is not running. Start it and retry."
command -v gh >/dev/null || fail "GitHub CLI is required. Install with: brew install gh"
gh auth status >/dev/null 2>&1 || fail "Authenticate GitHub CLI with: gh auth login"

API="$ROOT/../CompileCare.Api"
WEB="$ROOT/../CompileCare.Web"
if [[ ! -d "$API/.git" ]]; then gh repo clone CompileCare/CompileCare.Api "$API" || fail "Unable to clone CompileCare.Api."; fi
if [[ ! -d "$WEB/.git" ]]; then gh repo clone CompileCare/CompileCare.Web "$WEB" || fail "Unable to clone CompileCare.Web."; fi
export COMPILECARE_API_PATH="$API" COMPILECARE_WEB_PATH="$WEB"

if [[ ! -f "$LOCAL/.env" ]]; then
  cp "$LOCAL/.env.example" "$LOCAL/.env"
  read -r -p "Development OIDC Authority URL [http://keycloak.localhost:8082/realms/compilecare]: " authority
  authority="${authority:-http://keycloak.localhost:8082/realms/compilecare}"
  read -r -p "API Audience [compilecare-api]: " audience
  audience="${audience:-compilecare-api}"
  read -r -p "Web Client ID [compilecare-web]: " client
  client="${client:-compilecare-web}"
  read -r -p "OIDC Scope [openid profile]: " scope
  scope="${scope:-openid profile}"
  sed -i '' "s|^OIDC_AUTHORITY=.*|OIDC_AUTHORITY=$authority|" "$LOCAL/.env"
  sed -i '' "s|^OIDC_AUDIENCE=.*|OIDC_AUDIENCE=$audience|" "$LOCAL/.env"
  sed -i '' "s|^OIDC_CLIENT_ID=.*|OIDC_CLIENT_ID=$client|" "$LOCAL/.env"
  sed -i '' "s|^OIDC_SCOPE=.*|OIDC_SCOPE=$scope|" "$LOCAL/.env"
fi

mkdir -p "$LOCAL/.secrets"
CREDENTIALS="$LOCAL/.secrets/nuget-source-credentials"
if [[ ! -s "$CREDENTIALS" ]]; then
  read -r -p "GitHub username [Shatru-compile]: " username
  username="${username:-Shatru-compile}"
  read -r -s -p "GitHub PAT (read:packages): " token; echo
  [[ -n "$token" ]] || fail "GitHub Packages token required."
  umask 077
  printf 'Username=%s;Password=%s;ValidAuthenticationTypes=Basic' "$username" "$token" > "$CREDENTIALS"
  unset token
fi
chmod 600 "$CREDENTIALS"

# The earlier manual Keycloak container used the same host port. Stop that
# temporary container once before the Compose-managed Keycloak is started.
if docker ps --format '{{.Names}}' | grep -qx 'compilecare-keycloak'; then
  echo "A temporary compilecare-keycloak container is already running."
  echo "Stop it with: docker stop compilecare-keycloak"
  fail "Stop the temporary Keycloak container, then run the launcher again."
fi

echo "Building and starting CompileCare (including local Keycloak)..."
docker compose --env-file "$LOCAL/.env" -f "$LOCAL/docker-compose.yml" up --build -d || fail "Compose failed. Inspect: docker compose --env-file local/.env -f local/docker-compose.yml logs"
for i in {1..90}; do
  if curl -fsS http://localhost:8081/ >/dev/null 2>&1 && curl -fsS http://localhost:8080/ >/dev/null 2>&1; then
    echo "CompileCare is running at http://localhost:8080"
    echo "Local identity provider: http://keycloak.localhost:8082"
    open http://localhost:8080
    read -r -p "Press Enter to close..."
    exit 0
  fi
  sleep 2
done
docker compose --env-file "$LOCAL/.env" -f "$LOCAL/docker-compose.yml" ps
fail "Startup timeout. Inspect Docker Compose logs."
