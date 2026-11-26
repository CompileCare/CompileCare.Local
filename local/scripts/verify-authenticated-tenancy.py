#!/usr/bin/env python3
"""CI-only real-Keycloak and PostgreSQL multi-tenant acceptance test.

This test deliberately uses an ephemeral Compose stack and a temporary Keycloak
client with the password grant enabled. It does not modify the production
compilecare-web client or production identity-provider settings.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = LOCAL_ROOT / ".env"
KEYCLOAK = "http://keycloak.localhost:8082"
REALM = "compilecare"
API = "http://localhost:8081"
CI_CLIENT_ID = "compilecare-ci-acceptance"


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Keycloak returned a token that is not a JWT.")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    form: dict[str, str] | None = None,
    token: str | None = None,
) -> tuple[int, Any, dict[str, str]]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
            return response.status, body, dict(response.headers.items())
    except urllib.error.HTTPError as error:
        raw = error.read()
        content_type = error.headers.get("Content-Type", "")
        body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
        return error.code, body, dict(error.headers.items())


def require_status(
    action: str,
    result: tuple[int, Any, dict[str, str]],
    expected: int,
) -> Any:
    status, body, _ = result
    if status != expected:
        raise AssertionError(
            f"{action}: expected HTTP {expected}, received HTTP {status}; response={body!r}"
        )
    return body


class KeycloakAdmin:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, payload: Any = None) -> tuple[int, Any, dict[str, str]]:
        return request(self.base_url, path, method=method, payload=payload, token=self.token)


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        token: str,
        tenant_id: str | None = None,
        payload: Any = None,
    ) -> tuple[int, Any, dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"}
        if tenant_id is not None:
            headers["X-Tenant-ID"] = tenant_id
        return self._request(method, path, headers, payload)

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        payload: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        outgoing = {"Accept": "application/json", **headers}
        if data is not None:
            outgoing["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=outgoing, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
                return response.status, body, dict(response.headers.items())
        except urllib.error.HTTPError as error:
            raw = error.read()
            content_type = error.headers.get("Content-Type", "")
            body = json.loads(raw) if raw and "json" in content_type else raw.decode("utf-8", errors="replace")
            return error.code, body, dict(error.headers.items())


def admin_access_token(username: str, password: str) -> str:
    result = request(
        KEYCLOAK,
        "/realms/master/protocol/openid-connect/token",
        method="POST",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        },
    )
    body = require_status("Keycloak master admin login", result, 200)
    token = body.get("access_token")
    if not token:
        raise RuntimeError("Keycloak admin login returned no access token.")
    return token


def create_ci_client(admin: KeycloakAdmin) -> str:
    scopes = require_status(
        "Read CompileCare client scopes",
        admin.call("GET", f"/admin/realms/{REALM}/client-scopes"),
        200,
    )
    audience_scope = next(
        (scope for scope in scopes if scope.get("name") == "compilecare-api-audience"),
        None,
    )
    if audience_scope is None:
        raise RuntimeError("The CompileCare API audience client scope is missing.")

    status, body, _ = admin.call(
        "POST",
        f"/admin/realms/{REALM}/clients",
        {
            "clientId": CI_CLIENT_ID,
            "name": "CompileCare ephemeral CI acceptance client",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": False,
        },
    )
    if status != 201:
        raise AssertionError(
            f"Create temporary Keycloak CI client: expected HTTP 201, received HTTP {status}; response={body!r}"
        )

    clients = require_status(
        "Find temporary Keycloak CI client",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/clients?clientId={urllib.parse.quote(CI_CLIENT_ID)}",
        ),
        200,
    )
    if len(clients) != 1:
        raise RuntimeError("Expected exactly one temporary CI client in Keycloak.")
    client_uuid = clients[0]["id"]

    require_status(
        "Assign API audience scope to temporary CI client",
        admin.call(
            "PUT",
            f"/admin/realms/{REALM}/clients/{client_uuid}/default-client-scopes/{audience_scope['id']}",
        ),
        204,
    )
    return client_uuid


def create_user(admin: KeycloakAdmin, username: str, password: str) -> str:
    payload = {
        "username": username,
        "enabled": True,
        "email": f"{username}@compilecare.invalid",
        "emailVerified": True,
        "firstName": "CompileCare",
        "lastName": "CI Acceptance",
        "credentials": [
            {"type": "password", "value": password, "temporary": False}
        ],
    }
    require_status(
        f"Create synthetic Keycloak user {username}",
        admin.call("POST", f"/admin/realms/{REALM}/users", payload),
        201,
    )
    users = require_status(
        f"Find synthetic Keycloak user {username}",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/users?username={urllib.parse.quote(username)}&exact=true",
        ),
        200,
    )
    if len(users) != 1:
        raise RuntimeError(f"Expected exactly one Keycloak user for {username}.")
    return users[0]["id"]


def user_access_token(username: str, password: str, expected_issuer: str) -> tuple[str, str]:
    result = request(
        KEYCLOAK,
        f"/realms/{REALM}/protocol/openid-connect/token",
        method="POST",
        form={
            "grant_type": "password",
            "client_id": CI_CLIENT_ID,
            "username": username,
            "password": password,
            "scope": "openid profile email",
        },
    )
    body = require_status(f"Obtain real Keycloak token for {username}", result, 200)
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"Keycloak returned no access token for {username}.")

    claims = decode_jwt_payload(token)
    if claims.get("iss") != expected_issuer:
        raise AssertionError(
            f"Keycloak issuer mismatch for {username}: {claims.get('iss')!r}"
        )
    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if "compilecare-api" not in audiences:
        raise AssertionError(
            f"Keycloak access token for {username} is missing the CompileCare API audience."
        )
    subject = claims.get("sub")
    if not subject:
        raise AssertionError(f"Keycloak access token for {username} has no subject.")
    return token, subject


def create_tenant(api: Api, platform_token: str, name: str, slug: str) -> str:
    body = require_status(
        f"Create {name}",
        api.call(
            "POST",
            "/api/v1/platform/tenants",
            platform_token,
            payload={"name": name, "slug": slug},
        ),
        201,
    )
    return body["id"]


def provision_initial_admin(
    api: Api,
    platform_token: str,
    tenant_id: str,
    issuer: str,
    subject: str,
) -> str:
    body = require_status(
        "Provision initial tenant administrator",
        api.call(
            "POST",
            f"/api/v1/platform/tenants/{tenant_id}/initial-administrator",
            platform_token,
            payload={"issuer": issuer, "subject": subject},
        ),
        201,
    )
    return body["id"]


def set_tenant_active(api: Api, platform_token: str, tenant_id: str) -> None:
    require_status(
        "Activate provisioned tenant",
        api.call("POST", f"/api/v1/platform/tenants/{tenant_id}/activate", platform_token),
        200,
    )


def create_membership(
    api: Api,
    admin_token: str,
    tenant_id: str,
    issuer: str,
    subject: str,
    role: str,
) -> str:
    body = require_status(
        f"Create {role} membership",
        api.call(
            "POST",
            "/api/v1/tenant/memberships",
            admin_token,
            tenant_id,
            {
                "issuer": issuer,
                "subject": subject,
                "role": role,
                "scopes": [{"scopeType": "Tenant"}],
            },
        ),
        201,
    )
    return body["id"]


def create_hospital(api: Api, token: str, tenant_id: str, name: str, code: str) -> str:
    body = require_status(
        f"Create hospital {code}",
        api.call(
            "POST",
            "/api/v1/hospitals",
            token,
            tenant_id,
            {"name": name, "code": code},
        ),
        201,
    )
    return body["id"]


def expect_api_status(
    action: str,
    api: Api,
    method: str,
    path: str,
    token: str,
    tenant_id: str | None,
    expected: int,
    payload: Any = None,
) -> Any:
    return require_status(
        action,
        api.call(method, path, token, tenant_id, payload),
        expected,
    )


def run() -> None:
    if os.environ.get("COMPILECARE_CI_ACCEPTANCE") != "1":
        raise RuntimeError(
            "This script mutates Keycloak and PostgreSQL test data. "
            "Run it only from the isolated CI Compose workflow."
        )
    if not ENV_FILE.is_file():
        raise RuntimeError(f"Missing ephemeral environment file: {ENV_FILE}")

    env = read_env(ENV_FILE)
    admin_username = env.get("KEYCLOAK_ADMIN", "admin")
    admin_password = env.get("KEYCLOAK_ADMIN_PASSWORD", "")
    bootstrap_secret = env.get("PLATFORM_BOOTSTRAP_SECRET", "")
    if not admin_password or not bootstrap_secret:
        raise RuntimeError("The isolated CI environment is missing Keycloak or platform bootstrap credentials.")

    # Confirm discovery and issuer before creating any identity.
    discovery = require_status(
        "Read CompileCare OIDC discovery",
        request(KEYCLOAK, f"/realms/{REALM}/.well-known/openid-configuration"),
        200,
    )
    issuer = discovery["issuer"]
    if issuer != f"{KEYCLOAK}/realms/{REALM}":
        raise AssertionError(f"Unexpected CompileCare OIDC issuer: {issuer!r}")

    admin_token = admin_access_token(admin_username, admin_password)
    admin = KeycloakAdmin(KEYCLOAK, admin_token)
    client_uuid = create_ci_client(admin)

    suffix = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    usernames = {
        name: f"cc-ci-{name}-{suffix}"
        for name in ("platform", "tenant-a-admin", "tenant-b-admin", "shared")
    }
    subjects: dict[str, str] = {}
    tokens: dict[str, str] = {}
    for name, username in usernames.items():
        create_user(admin, username, password)
        tokens[name], subjects[name] = user_access_token(username, password, issuer)

    api = Api(API)

    # Bootstrap a persisted platform operator through the Development-only
    # endpoint, using a token actually issued and signed by local Keycloak.
    require_status(
        "Bootstrap platform operator using real Keycloak identity",
        api.call(
            "POST",
            "/api/v1/platform/bootstrap/operator",
            tokens["platform"],
            payload={"bootstrapSecret": bootstrap_secret},
        ),
        201,
    )
    operator_status = require_status(
        "Read persisted platform operator status",
        api.call("GET", "/api/v1/platform/operator/status", tokens["platform"]),
        200,
    )
    if operator_status.get("isPlatformOperator") is not True:
        raise AssertionError("The real Keycloak identity was not persisted as a platform operator.")

    tenant_a = create_tenant(api, tokens["platform"], "CI Tenant A", f"ci-a-{suffix}")
    tenant_b = create_tenant(api, tokens["platform"], "CI Tenant B", f"ci-b-{suffix}")

    provision_initial_admin(api, tokens["platform"], tenant_a, issuer, subjects["tenant-a-admin"])
    provision_initial_admin(api, tokens["platform"], tenant_b, issuer, subjects["tenant-b-admin"])
    set_tenant_active(api, tokens["platform"], tenant_a)
    set_tenant_active(api, tokens["platform"], tenant_b)

    # The same external user has different persisted roles in two tenants.
    membership_a = create_membership(
        api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["shared"], "HospitalAdmin"
    )
    membership_b = create_membership(
        api, tokens["tenant-b-admin"], tenant_b, issuer, subjects["shared"], "FacilityViewer"
    )

    hospital_a = create_hospital(
        api, tokens["shared"], tenant_a, "CI Hospital A", f"A-{suffix.upper()}"
    )
    hospital_b = create_hospital(
        api, tokens["tenant-b-admin"], tenant_b, "CI Hospital B", f"B-{suffix.upper()}"
    )

    contexts = require_status(
        "List current user's active tenant contexts",
        api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", tokens["shared"]),
        200,
    )
    items = contexts.get("items", [])
    if contexts.get("totalCount") != 2 or len(items) != 2:
        raise AssertionError(f"Expected two active persisted tenant contexts, received {contexts!r}")
    context_a = next((item for item in items if item.get("tenantId") == tenant_a), None)
    context_b = next((item for item in items if item.get("tenantId") == tenant_b), None)
    if context_a is None or context_b is None:
        raise AssertionError("The two persisted tenant memberships were not both returned.")
    if context_a.get("role") != "HospitalAdmin" or context_b.get("role") != "FacilityViewer":
        raise AssertionError("The same identity did not receive its tenant-specific persisted roles.")
    if "facility.delete" not in context_a.get("permissions", []):
        raise AssertionError("HospitalAdmin permissions were not derived from persisted membership.")
    if "facility.write" in context_b.get("permissions", []) or "facility.delete" in context_b.get("permissions", []):
        raise AssertionError("External or cross-tenant claims escalated the FacilityViewer permissions.")

    # Tenant data and permissions must follow the selected persisted membership.
    list_a = require_status(
        "List hospitals in tenant A",
        api.call("GET", "/api/v1/hospitals", tokens["shared"], tenant_a),
        200,
    )
    list_b = require_status(
        "List hospitals in tenant B",
        api.call("GET", "/api/v1/hospitals", tokens["shared"], tenant_b),
        200,
    )
    ids_a = {item["id"] for item in list_a.get("items", [])}
    ids_b = {item["id"] for item in list_b.get("items", [])}
    if ids_a != {hospital_a} or ids_b != {hospital_b}:
        raise AssertionError(
            f"Tenant data isolation failed: tenant A IDs={ids_a!r}; tenant B IDs={ids_b!r}"
        )

    expect_api_status(
        "Tenant A identity cannot read tenant B hospital",
        api, "GET", f"/api/v1/hospitals/{hospital_b}", tokens["shared"], tenant_a, 404
    )
    expect_api_status(
        "Tenant B identity cannot read tenant A hospital",
        api, "GET", f"/api/v1/hospitals/{hospital_a}", tokens["shared"], tenant_b, 404
    )
    expect_api_status(
        "FacilityViewer cannot create a hospital",
        api, "POST", "/api/v1/hospitals", tokens["shared"], tenant_b, 403
    )
    expect_api_status(
        "Platform operator without tenant membership cannot select tenant A",
        api, "GET", "/api/v1/hospitals", tokens["platform"], tenant_a, 403
    )
    expect_api_status(
        "Authenticated request without tenant selection is rejected",
        api, "GET", "/api/v1/hospitals", tokens["shared"], None, 400
    )

    # A tenant administrator cannot mutate a membership owned by another tenant.
    expect_api_status(
        "Tenant A administrator cannot update tenant B membership",
        api, "PUT", f"/api/v1/tenant/memberships/{membership_b}",
        tokens["tenant-a-admin"], tenant_a, 404,
        {"role": "FacilityViewer", "scopes": [{"scopeType": "Tenant"}]}
    )
    expect_api_status(
        "Tenant B administrator cannot update tenant A membership",
        api, "PUT", f"/api/v1/tenant/memberships/{membership_a}",
        tokens["tenant-b-admin"], tenant_b, 404,
        {"role": "FacilityViewer", "scopes": [{"scopeType": "Tenant"}]}
    )

    # Revocation takes effect on the next request without invalidating access to
    # the user's independent membership in tenant A.
    require_status(
        "Deactivate tenant B membership",
        api.call(
            "POST",
            f"/api/v1/tenant/memberships/{membership_b}/deactivate",
            tokens["tenant-b-admin"],
            tenant_b,
        ),
        200,
    )
    expect_api_status(
        "Deactivated tenant B membership is denied",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_b, 403
    )
    expect_api_status(
        "Tenant A membership remains active after tenant B revocation",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_a, 200
    )
    inactive_contexts = require_status(
        "List contexts after tenant B revocation",
        api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", tokens["shared"]),
        200,
    )
    remaining = inactive_contexts.get("items", [])
    if inactive_contexts.get("totalCount") != 1 or len(remaining) != 1 or remaining[0].get("tenantId") != tenant_a:
        raise AssertionError(f"Inactive tenant membership remained visible: {inactive_contexts!r}")

    require_status(
        "Reactivate tenant B membership",
        api.call(
            "POST",
            f"/api/v1/tenant/memberships/{membership_b}/reactivate",
            tokens["tenant-b-admin"],
            tenant_b,
        ),
        200,
    )
    expect_api_status(
        "Reactivated tenant B membership regains its own tenant access",
        api, "GET", "/api/v1/hospitals", tokens["shared"], tenant_b, 200
    )

    print("Verified real Keycloak-issued tokens against CompileCare.Api issuer/audience validation.")
    print("Verified two PostgreSQL-persisted tenant memberships with different roles and permissions.")
    print("Verified hospital data isolation, cross-tenant membership mutation denial, and membership revocation/reactivation.")
    print("No real patient or billing data was created.")


def main() -> None:
    run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not print request headers, credentials, refresh/access tokens or secrets.
        print(f"AUTHENTICATED TENANCY ACCEPTANCE FAILED: {error}", file=__import__("sys").stderr)
        raise SystemExit(1)
