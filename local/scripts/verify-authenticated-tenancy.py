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
CI_CLIENT_ID = "heapcare-ci-acceptance"


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
        "Read HeapCare client scopes",
        admin.call("GET", f"/admin/realms/{REALM}/client-scopes"),
        200,
    )
    audience_scope = next(
        (scope for scope in scopes if scope.get("name") == "compilecare-api-audience"),
        None,
    )
    basic_scope = next(
        (scope for scope in scopes if scope.get("name") == "basic"),
        None,
    )
    if audience_scope is None:
        raise RuntimeError("The HeapCare API audience client scope is missing.")
    if basic_scope is None:
        raise RuntimeError("The Keycloak basic client scope is missing; access tokens require its Subject (sub) mapper.")

    basic_mappers = require_status(
        "Read Keycloak basic scope protocol mappers",
        admin.call(
            "GET",
            f"/admin/realms/{REALM}/client-scopes/{basic_scope['id']}/protocol-mappers/models",
        ),
        200,
    )
    if not any(
        mapper.get("protocolMapper") == "oidc-sub-mapper"
        and mapper.get("config", {}).get("access.token.claim") == "true"
        for mapper in basic_mappers
    ):
        raise RuntimeError("The Keycloak basic scope is missing its access-token Subject (sub) mapper.")

    status, body, _ = admin.call(
        "POST",
        f"/admin/realms/{REALM}/clients",
        {
            "clientId": CI_CLIENT_ID,
            "name": "HeapCare ephemeral CI acceptance client",
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

    for label, scope in (
        ("basic subject", basic_scope),
        ("API audience", audience_scope),
    ):
        require_status(
            f"Assign {label} scope to temporary CI client",
            admin.call(
                "PUT",
                f"/admin/realms/{REALM}/clients/{client_uuid}/default-client-scopes/{scope['id']}",
            ),
            204,
        )
    return client_uuid


def create_user(admin: KeycloakAdmin, username: str, password: str) -> str:
    payload = {
        "username": username,
        "enabled": True,
        "email": f"{username}@heapcare.invalid",
        "emailVerified": True,
        "firstName": "HeapCare",
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
            f"Keycloak access token for {username} is missing the HeapCare API audience."
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


def create_department(api: Api, token: str, tenant_id: str, hospital_id: str, name: str, code: str) -> str:
    body = require_status(
        f"Create department {code}",
        api.call(
            "POST",
            f"/api/v1/hospitals/{hospital_id}/departments",
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
    if os.environ.get("HEAPCARE_CI_ACCEPTANCE") != "1":
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
        "Read HeapCare OIDC discovery",
        request(KEYCLOAK, f"/realms/{REALM}/.well-known/openid-configuration"),
        200,
    )
    issuer = discovery["issuer"]
    if issuer != f"{KEYCLOAK}/realms/{REALM}":
        raise AssertionError(f"Unexpected OIDC issuer: {issuer!r}")

    admin_token = admin_access_token(admin_username, admin_password)
    admin = KeycloakAdmin(KEYCLOAK, admin_token)
    client_uuid = create_ci_client(admin)

    suffix = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    usernames = {
        name: f"heapcare-ci-{name}-{suffix}"
        for name in ("platform", "tenant-a-admin", "tenant-b-admin", "shared", "facility-manager", "patient-registrar", "clinical-user")
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

    # Provision one real Keycloak identity for each remaining persisted tenant
    # role. These memberships are isolated to the synthetic CI tenant and are
    # used to verify the API's server-derived permission contexts end to end.
    role_memberships = {
        "FacilityManager": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["facility-manager"], "FacilityManager"
        ),
        "PatientRegistrar": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["patient-registrar"], "PatientRegistrar"
        ),
        "ClinicalUser": create_membership(
            api, tokens["tenant-a-admin"], tenant_a, issuer, subjects["clinical-user"], "ClinicalUser"
        ),
    }

    hospital_a = create_hospital(
        api, tokens["shared"], tenant_a, "CI Hospital A", f"A-{suffix.upper()}"
    )
    hospital_b = create_hospital(
        api, tokens["tenant-b-admin"], tenant_b, "CI Hospital B", f"B-{suffix.upper()}"
    )

    department_b = create_department(
        api, tokens["tenant-b-admin"], tenant_b, hospital_b,
        "CI Department B", f"DB-{suffix.upper()}"
    )

    # FacilityViewer is a facility-read-only role. Prove the API rejects
    # patient registration even when called directly with a valid Keycloak token.
    expect_api_status(
        "FacilityViewer cannot register a patient",
        api,
        "POST",
        f"/api/v1/hospitals/{hospital_b}/patients",
        tokens["shared"],
        tenant_b,
        403,
        {
            "givenName": "CI Forbidden",
            "middleName": None,
            "familyName": "FacilityViewer",
            "dateOfBirth": "1990-01-02",
        },
    )

    expect_api_status(
        "FacilityViewer cannot search patients",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients?page=1&pageSize=25",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot read patient identity details",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/00000000-0000-0000-0000-000000000000",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot search department-scoped patients",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/departments/{department_b}/patients?page=1&pageSize=25",
        tokens["shared"],
        tenant_b,
        403,
    )
    expect_api_status(
        "FacilityViewer cannot read department-scoped patient identity details",
        api,
        "GET",
        f"/api/v1/hospitals/{hospital_b}/patients/departments/{department_b}/patients/00000000-0000-0000-0000-000000000000",
        tokens["shared"],
        tenant_b,
        403,
    )

    expect_api_status(
        "FacilityViewer cannot run patient duplicate review",
        api,
        "POST",
        f"/api/v1/hospitals/{hospital_b}/patients/duplicate-candidates",
        tokens["shared"],
        tenant_b,
        403,
        {
            "givenName": "CI Forbidden",
            "middleName": None,
            "familyName": "FacilityViewer",
            "dateOfBirth": "1990-01-02",
        },
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

    # Validate every persisted tenant role using tokens issued by the real
    # Keycloak server. The API must derive the exact expected grants from the
    # persisted membership; token claims are not an authorization source.
    expected_role_permissions = {
        "HospitalAdmin": {"facility.read", "facility.write", "facility.delete", "patient.read", "patient.register", "appointment.manage", "audit.read"},
        "FacilityManager": {"facility.read", "facility.write"},
        "FacilityViewer": {"facility.read"},
        "PatientRegistrar": {"facility.read", "patient.read", "patient.register", "patient.identifier.read", "appointment.manage", "queue.manage", "billing.invoice.manage", "billing.payment.manage"},
        "ClinicalUser": {"facility.read", "patient.identity.read", "queue.read", "encounter.read", "encounter.manage", "encounter.sign", "encounter.amend"},
    }
    role_tokens = {
        "HospitalAdmin": tokens["shared"],
        "FacilityManager": tokens["facility-manager"],
        "PatientRegistrar": tokens["patient-registrar"],
        "ClinicalUser": tokens["clinical-user"],
    }
    for role, role_token in role_tokens.items():
        role_contexts = require_status(
            f"Read persisted {role} tenant context",
            api.call("GET", "/api/v1/me/tenant-contexts?page=1&pageSize=50", role_token),
            200,
        )
        role_items = role_contexts.get("items", [])
        context = next((item for item in role_items if item.get("tenantId") == tenant_a), None)
        if context is None or context.get("role") != role:
            raise AssertionError(f"Expected persisted {role} context for tenant A; received {role_contexts!r}")
        permissions = set(context.get("permissions", []))
        missing = expected_role_permissions[role] - permissions
        if missing:
            raise AssertionError(f"{role} is missing expected persisted permissions: {sorted(missing)!r}")
        if role != "HospitalAdmin" and "facility.delete" in permissions:
            raise AssertionError(f"{role} unexpectedly received facility.delete.")
        expect_api_status(
            f"{role} can read the tenant-scoped hospital collection",
            api, "GET", "/api/v1/hospitals", role_token, tenant_a, 200
        )

    # The FacilityManager may maintain facilities but must not read patient
    # records; PatientRegistrar may read patients; ClinicalUser receives only
    # patient identity access for encounter workflows, not general search.
    expect_api_status(
        "FacilityManager cannot search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["facility-manager"], tenant_a, 403
    )
    expect_api_status(
        "PatientRegistrar can search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["patient-registrar"], tenant_a, 200
    )
    expect_api_status(
        "ClinicalUser cannot search patient records", api, "GET",
        f"/api/v1/hospitals/{hospital_a}/patients?page=1&pageSize=25",
        tokens["clinical-user"], tenant_a, 403
    )

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

    # Hand the same persisted two-tenant identity to the real browser PKCE
    # test through a runner-temporary file; never write credentials to the repo
    # or emit them into the Actions log.
    browser_user_file = os.environ.get("HEAPCARE_BROWSER_TEST_USER_FILE")
    if not browser_user_file:
        raise RuntimeError("HEAPCARE_BROWSER_TEST_USER_FILE must point to the runner-temporary browser credential file.")
    browser_user_path = Path(browser_user_file)
    browser_user_path.parent.mkdir(parents=True, exist_ok=True)
    browser_user_path.write_text(
        json.dumps(
            {
                "username": usernames["shared"],
                "password": password,
                "users": {
                    name: {
                        "username": username,
                        "password": password,
                    }
                    for name, username in usernames.items()
                },
            }
        ),
        encoding="utf-8",
    )
    browser_user_path.chmod(0o600)

    print("Verified real Keycloak-issued tokens against HeapCare.Api issuer/audience validation.")
    print("Verified real Keycloak-issued identities for all five persisted tenant roles and server-derived role permissions.")
    print("Verified hospital data isolation, cross-tenant membership mutation denial, and membership revocation/reactivation.")
    print("Verified FacilityViewer and ClinicalUser/FacilityManager patient-search denials and PatientRegistrar patient-search access.")
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
