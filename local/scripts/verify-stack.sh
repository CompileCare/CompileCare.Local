#!/usr/bin/env bash
set -Eeuo pipefail

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repository_root"

if [[ ! -f local/.env ]]; then
  echo "Missing local/.env. Run local/Start CompileCare.command first." >&2
  exit 1
fi

compose=(docker compose --env-file local/.env -f local/docker-compose.yml -f local/docker-compose.observability.yml)
curl_local() { curl --fail --silent --show-error --connect-timeout 5 --max-time 20 "$@"; }

echo "Checking required application and observability containers..."
running_services=$("${compose[@]}" ps --services --status running)
for service in postgres keycloak api web otel-collector jaeger prometheus elasticsearch kibana; do
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

echo "Checking Web and API..."
curl_local http://localhost:8080/ >/dev/null
api_response=$(curl_local http://localhost:8081/)
python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("service") == "CompileCare.Api" and data.get("status") == "ready", "Unexpected API readiness payload"' <<<"$api_response"
curl_local http://localhost:8081/openapi/v1.json >/dev/null
echo "  ✓ Web, API readiness and Development OpenAPI respond"

echo "Checking Keycloak OIDC discovery..."
oidc_response=$(curl_local http://keycloak.localhost:8082/realms/compilecare/.well-known/openid-configuration)
python3 -c 'import json,sys; data=json.load(sys.stdin); expected="http://keycloak.localhost:8082/realms/compilecare"; actual=data.get("issuer"); assert actual == expected, f"Unexpected OIDC issuer: {actual!r}"; assert data.get("authorization_endpoint") and data.get("token_endpoint") and data.get("jwks_uri"), "OIDC discovery document is incomplete"' <<<"$oidc_response"
echo "  ✓ Keycloak issuer and OIDC discovery endpoints match"

echo "Checking observability backends..."
curl_local http://localhost:9090/-/ready >/dev/null
curl_local http://localhost:16686/ >/dev/null
curl_local http://localhost:9200/_cluster/health >/dev/null
curl_local http://localhost:5601/api/status >/dev/null
curl_local http://localhost:8889/metrics >/dev/null
echo "  ✓ Prometheus, Jaeger, Elasticsearch, Kibana and Collector metrics endpoint respond"

echo "Checking API metrics endpoint..."
curl_local http://localhost:8081/metrics >/dev/null
echo "  ✓ API Prometheus metrics endpoint responds"

echo
echo "CompileCare full local infrastructure smoke checks passed."
echo "Still required: browser sign-in, persisted tenant/RBAC checks, patient workflows, UI visual review, telemetry end-to-end correlation and production security acceptance."
echo "Use synthetic records only. These checks do not certify production readiness."
