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
curl_local http://localhost:8889/metrics >/dev/null
echo "  ✓ Prometheus, Jaeger, Elasticsearch and Collector metrics endpoint respond"

echo "Provisioning the CompileCare Logs Kibana data view (waits for Kibana readiness)..."
python3 local/scripts/provision-kibana-data-view.py --base-url http://localhost:5601
curl_local http://localhost:5601/api/status >/dev/null
echo "  ✓ Kibana is ready and data view compilecare-logs-* is provisioned"

echo "Checking API metrics endpoint..."
curl_local http://localhost:8081/metrics >/dev/null
echo "  ✓ API Prometheus metrics endpoint responds"

echo "Checking API-to-Collector-to-Jaeger telemetry end to end..."
python3 - <<'PY'
import json
import time
import urllib.error
import urllib.request

API = "http://localhost:8081"
JAEGER = "http://localhost:16686"
COLLECTOR_METRICS = "http://localhost:8889/metrics"

def get(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8"), response.headers

# Produce a simple HTTP server span and an Npgsql connection/database span
# using only readiness checks; do not use real patient or billing data.
for path in ("/", "/health/ready"):
    with urllib.request.urlopen(API + path, timeout=20) as response:
        if response.status >= 500:
            raise RuntimeError(f"API telemetry probe failed at {path}: HTTP {response.status}")

# BatchSpanProcessor flush and OTLP metric reader export are asynchronous.
# Wait for Jaeger to index the API service and the PostgreSQL child operation.
deadline = time.monotonic() + 90
services = []
operations = []
while time.monotonic() < deadline:
    try:
        body, _ = get(JAEGER + "/api/v3/services")
        services = json.loads(body).get("services", [])
        if "CompileCare.Api" in services:
            op_body, _ = get(JAEGER + "/api/v3/operations?service=CompileCare.Api")
            operations = json.loads(op_body).get("operations", [])
            operation_names = [item.get("name", "") for item in operations]
            if any(name.startswith("CONNECT ") or name.lower() == "postgresql" for name in operation_names):
                break
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass
    time.sleep(2)
else:
    raise RuntimeError(
        "CompileCare.Api traces did not reach Jaeger with an Npgsql database operation. "
        f"services={services!r}, operations={operations!r}"
    )

# Require an API HTTP metric at the Collector's Prometheus exporter. This
# distinguishes OTLP -> Collector -> Prometheus from the existing direct API scrape.
metric_deadline = time.monotonic() + 90
metric_excerpt = ""
while time.monotonic() < metric_deadline:
    try:
        metric_excerpt, _ = get(COLLECTOR_METRICS)
        if "http_server_request_duration" in metric_excerpt:
            break
    except (urllib.error.URLError, TimeoutError):
        pass
    time.sleep(2)
else:
    raise RuntimeError("No ASP.NET Core request-duration metric reached the Collector Prometheus exporter.")

print("Verified CompileCare.Api in Jaeger with an Npgsql database span.")
print("Verified ASP.NET Core request metrics arrive through the OTLP Collector.")
print(f"Jaeger operations: {[item.get('name') for item in operations]}")
PY
echo "  ✓ API traces, Npgsql database spans and Collector-fed API metrics verified"

echo
echo "CompileCare full local infrastructure smoke checks passed."
echo "Still required: browser sign-in, persisted tenant/RBAC checks, patient workflows, UI visual review, telemetry end-to-end correlation and production security acceptance."
echo "Use synthetic records only. These checks do not certify production readiness."
