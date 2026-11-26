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

# Restore the intended profile claim scope after the earlier local diagnostic workaround.
if grep -qx 'OIDC_SCOPE=openid' "$LOCAL/.env"; then
  sed -i '' 's|^OIDC_SCOPE=openid$|OIDC_SCOPE=openid profile|' "$LOCAL/.env"
fi

# Generate unique local-only credentials instead of relying on checked-in sample passwords.
command -v openssl >/dev/null || fail "OpenSSL is required to generate local-only credentials."
for key in KEYCLOAK_ADMIN_PASSWORD POSTGRES_PASSWORD; do
  current="$(sed -n "s/^${key}=//p" "$LOCAL/.env" | head -n 1)"
  if [[ -z "$current" || "$current" == replace-with-* || "$current" == "ChangeThisLocalPassword123!" || "$current" == "compilecare-local-only" ]]; then
    generated="$(openssl rand -hex 32)"
    if grep -q "^${key}=" "$LOCAL/.env"; then
      sed -i '' "s|^${key}=.*|${key}=${generated}|" "$LOCAL/.env"
    else
      printf '\n%s=%s\n' "$key" "$generated" >> "$LOCAL/.env"
    fi
    unset generated
  fi
done
chmod 600 "$LOCAL/.env"

# Generate a one-time local bootstrap secret when the example value is still present.
if ! grep -q '^PLATFORM_BOOTSTRAP_SECRET=.' "$LOCAL/.env" || grep -q '^PLATFORM_BOOTSTRAP_SECRET=replace-with-' "$LOCAL/.env"; then
  command -v openssl >/dev/null || fail "OpenSSL is required to generate the local bootstrap secret."
  bootstrap_secret="$(openssl rand -hex 32)"
  if grep -q '^PLATFORM_BOOTSTRAP_SECRET=' "$LOCAL/.env"; then
    sed -i '' "s|^PLATFORM_BOOTSTRAP_SECRET=.*|PLATFORM_BOOTSTRAP_SECRET=$bootstrap_secret|" "$LOCAL/.env"
  else
    printf '\nPLATFORM_BOOTSTRAP_SECRET=%s\n' "$bootstrap_secret" >> "$LOCAL/.env"
  fi
  echo "One-time local platform bootstrap secret (enter it in the first-run workspace setup): $bootstrap_secret"
  unset bootstrap_secret
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

# Replace the earlier manually-created container. A stopped container still
# reserves its name, so inspect all containers, not only running ones.
if docker ps -a --format '{{.Names}}' | grep -qx 'compilecare-keycloak'; then
  project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' compilecare-keycloak 2>/dev/null || true)"
  if [[ "$project" != "compilecare-local" ]]; then
    echo "Removing the earlier standalone compilecare-keycloak container."
    docker rm -f compilecare-keycloak >/dev/null || fail "Could not remove the earlier temporary Keycloak container."
  fi
fi

echo "Building and starting CompileCare (including local Keycloak)..."
docker compose --env-file "$LOCAL/.env" -f "$LOCAL/docker-compose.yml" up --build -d || fail "Compose failed. Inspect: docker compose --env-file local/.env -f local/docker-compose.yml logs"
command -v python3 >/dev/null || fail "Python 3 is required to reconcile the local Keycloak realm scopes."
python3 "$LOCAL/scripts/configure-keycloak.py" "$LOCAL/.env" --base-url http://keycloak.localhost:8082 || fail "Could not reconcile local Keycloak OIDC scopes. Inspect the Keycloak admin credentials and logs."
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
