#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL="$ROOT/local"
API="$ROOT/../CompileCare.Api"
WEB="$ROOT/../CompileCare.Web"
BASE_COMPOSE="$LOCAL/docker-compose.yml"
OBS_COMPOSE="$LOCAL/docker-compose.observability.yml"
cd "$ROOT"

fail() {
  echo "ERROR: $*" >&2
  echo "See logs with: docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml logs --tail=150" >&2
  read -r -p "Press Enter to close..." || true
  exit 1
}

command -v docker >/dev/null || fail "Docker Desktop is required. Install from https://www.docker.com/products/docker-desktop/."
docker info >/dev/null 2>&1 || fail "Docker Desktop is not running. Start it and retry."
command -v gh >/dev/null || fail "GitHub CLI is required. Install with: brew install gh"
gh auth status >/dev/null 2>&1 || fail "Authenticate GitHub CLI with: gh auth login"
command -v git >/dev/null || fail "Git is required."
command -v openssl >/dev/null || fail "OpenSSL is required to generate local-only credentials."
command -v python3 >/dev/null || fail "Python 3 is required for local Keycloak configuration."

# Clone missing application repositories; update existing clean main checkouts
# with fast-forward-only pulls. Never overwrite or discard local developer work.
sync_repository() {
  local repo="$1"
  local path="$2"
  if [[ ! -d "$path/.git" ]]; then
    [[ ! -e "$path" ]] || fail "$path exists but is not a Git checkout. Move it aside or inspect it before retrying."
    echo "Cloning $repo..."
    gh repo clone "CompileCare/$repo" "$path" || fail "Unable to clone CompileCare/$repo."
    return
  fi

  local current_branch
  current_branch="$(git -C "$path" branch --show-current)"
  [[ "$current_branch" == "main" ]] || fail "$repo is on '$current_branch', not 'main'. Switch it to main after preserving your work, then rerun."
  [[ -z "$(git -C "$path" status --porcelain)" ]] || fail "$repo has uncommitted changes. Commit or stash them before the launcher updates main."
  echo "Updating $repo (fast-forward only)..."
  git -C "$path" fetch --prune origin main || fail "Could not fetch $repo."
  git -C "$path" pull --ff-only origin main || fail "$repo cannot fast-forward. Resolve the branch state manually; no local changes were overwritten."
}

sync_repository "CompileCare.Api" "$API"
sync_repository "CompileCare.Web" "$WEB"
export COMPILECARE_API_PATH="$API" COMPILECARE_WEB_PATH="$WEB"

# Keep a stable local environment and persistent database/identity volumes.
NEW_LOCAL_ENV=false
if [[ ! -f "$LOCAL/.env" ]]; then
  cp "$LOCAL/.env.example" "$LOCAL/.env"
  NEW_LOCAL_ENV=true
fi

if [[ "$NEW_LOCAL_ENV" == "true" ]]; then
  echo "Creating fresh local-only credentials..."
  for key in KEYCLOAK_ADMIN_PASSWORD POSTGRES_PASSWORD; do
    generated="$(openssl rand -hex 32)"
    sed -i '' "s|^$key=.*|$key=$generated|" "$LOCAL/.env"
    unset generated
  done
  bootstrap_secret="$(openssl rand -hex 32)"
  sed -i '' "s|^PLATFORM_BOOTSTRAP_SECRET=.*|PLATFORM_BOOTSTRAP_SECRET=$bootstrap_secret|" "$LOCAL/.env"
  echo "One-time platform bootstrap secret: $bootstrap_secret"
  echo "Save it securely; it is not printed again."
  unset bootstrap_secret
fi

for key in KEYCLOAK_ADMIN_PASSWORD POSTGRES_PASSWORD PLATFORM_BOOTSTRAP_SECRET; do
  current="$(sed -n "s/^$key=//p" "$LOCAL/.env" | head -n 1)"
  [[ -n "$current" && "$current" != replace-with-* ]] || fail "$key is missing or still a placeholder in local/.env. Set a private local value; do not reuse production credentials."
done
chmod 600 "$LOCAL/.env"

# Use the authenticated GitHub CLI token for the private CompileCare NuGet feed.
mkdir -p "$LOCAL/.secrets"
CREDENTIALS="$LOCAL/.secrets/nuget-source-credentials"
if [[ ! -s "$CREDENTIALS" ]]; then
  echo "Checking GitHub Packages read access..."
  gh auth refresh -h github.com -s read:packages >/dev/null || fail "GitHub CLI needs the read:packages scope. Complete the browser authorization and retry."
  gh_user="$(gh api user --jq .login)" || fail "Could not determine the authenticated GitHub username."
  gh_token="$(gh auth token)" || fail "Could not retrieve the GitHub CLI token."
  [[ -n "$gh_user" && -n "$gh_token" ]] || fail "GitHub CLI returned empty package credentials."
  umask 077
  printf 'Username=%s;Password=%s;ValidAuthenticationTypes=Basic' "$gh_user" "$gh_token" > "$CREDENTIALS"
  unset gh_token
fi
chmod 600 "$CREDENTIALS"

# Remove only the legacy standalone Keycloak container, never the Compose project.
if docker ps -a --format '{{.Names}}' | grep -qx 'compilecare-keycloak'; then
  project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' compilecare-keycloak 2>/dev/null || true)"
  if [[ "$project" != "compilecare-local" ]]; then
    echo "Removing legacy standalone Keycloak container..."
    docker rm -f compilecare-keycloak >/dev/null || fail "Could not remove the legacy standalone Keycloak container."
  fi
fi

compose=(docker compose --env-file "$LOCAL/.env" -f "$BASE_COMPOSE" -f "$OBS_COMPOSE")
echo "Validating the complete Compose configuration..."
"${compose[@]}" config --quiet || fail "Docker Compose configuration is invalid."

echo "Pulling required base images and building the application images..."
"${compose[@]}" pull --ignore-buildable || fail "Could not pull one or more runtime images."
"${compose[@]}" build --pull api web || fail "API/Web Docker build failed."
echo "Starting CompileCare, PostgreSQL, Keycloak and the complete observability stack..."
"${compose[@]}" up -d --remove-orphans || fail "Compose startup failed."

echo "Reconciling local Keycloak OIDC client configuration..."
python3 "$LOCAL/scripts/configure-keycloak.py" "$LOCAL/.env" --base-url http://keycloak.localhost:8082 || fail "Could not reconcile local Keycloak OIDC scopes."

echo "Waiting for application and observability endpoints..."
ready=false
for i in {1..180}; do
  if curl -fsS http://localhost:8081/ >/dev/null 2>&1 &&
     curl -fsS http://localhost:8080/ >/dev/null 2>&1 &&
     curl -fsS http://localhost:9090/-/ready >/dev/null 2>&1 &&
     curl -fsS http://localhost:16686/ >/dev/null 2>&1 &&
     curl -fsS http://localhost:9200/_cluster/health >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done
[[ "$ready" == "true" ]] || {
  "${compose[@]}" ps
  fail "Timed out waiting for the full stack. Inspect Compose logs."
}

echo "Running infrastructure and observability smoke checks..."
bash "$LOCAL/scripts/verify-stack.sh" || fail "One or more stack checks failed."

echo
echo "CompileCare is ready."
echo "  Workspace:     http://localhost:8080"
echo "  API/OpenAPI:   http://localhost:8081/openapi/v1.json"
echo "  Keycloak:      http://keycloak.localhost:8082"
echo "  Prometheus:    http://localhost:9090"
echo "  Jaeger:        http://localhost:16686"
echo "  Kibana:        http://localhost:5601"
echo "  Elasticsearch: http://localhost:9200"
echo
echo "Use synthetic test data only. No production patient data or credentials."
open http://localhost:8080
read -r -p "Press Enter to close..." || true
