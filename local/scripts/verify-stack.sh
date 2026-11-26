#!/usr/bin/env bash
set -Eeuo pipefail

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repository_root"

if [[ ! -f local/.env ]]; then
  echo "Missing local/.env. Start CompileCare once to generate local credentials." >&2
  exit 1
fi

compose=(docker compose --env-file local/.env -f local/docker-compose.yml)
curl_local() { curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "$@"; }

echo "Checking Compose services..."
running_services=$("${compose[@]}" ps --services --status running)
for service in postgres keycloak api web; do
  if ! grep -Fxq "$service" <<<"$running_services"; then
    echo "Required service '$service' is not running." >&2
    "${compose[@]}" ps
    exit 1
  fi
  printf '  ✓ %s is running\n' "$service"
done

echo "Checking PostgreSQL readiness..."
"${compose[@]}" exec -T postgres pg_isready -U compilecare -d compilecare >/dev/null
echo "  ✓ PostgreSQL accepts local readiness probes"

echo "Checking Web endpoint..."
curl_local http://localhost:8080/ >/dev/null
echo "  ✓ Web responds on http://localhost:8080"

echo "Checking API readiness..."
api_response=$(curl_local http://localhost:8081/)
python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("service") == "CompileCare.Api" and data.get("status") == "ready", "Unexpected API readiness payload"' <<<"$api_response"
echo "  ✓ API reports ready"

echo "Checking Development OpenAPI endpoint..."
curl_local http://localhost:8081/openapi/v1.json >/dev/null
echo "  ✓ OpenAPI document is reachable"

echo "Checking Keycloak OIDC discovery..."
oidc_response=$(curl_local http://keycloak.localhost:8082/realms/compilecare/.well-known/openid-configuration)
python3 -c 'import json,sys; data=json.load(sys.stdin); expected="http://keycloak.localhost:8082/realms/compilecare"; actual=data.get("issuer"); assert actual == expected, f"Unexpected OIDC issuer: {actual!r}"; assert data.get("authorization_endpoint") and data.get("token_endpoint") and data.get("jwks_uri"), "OIDC discovery document is incomplete"' <<<"$oidc_response"
echo "  ✓ Keycloak issuer and discovery endpoints match the local configuration"

echo
echo "CompileCare local infrastructure smoke checks passed."
echo "This does not verify user sign-in, token validation, tenant membership/RBAC, patient workflows, migrations against existing data, telemetry delivery or production readiness."
echo "Continue with the authenticated acceptance checklist using synthetic records only."
