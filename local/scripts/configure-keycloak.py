#!/usr/bin/env python3
"""Idempotently reconcile the local Keycloak realm's OIDC client scopes.

This updates only the CompileCare realm's profile/email/audience scopes and
compilecare-web default-scope assignments. It never deletes a realm or volume.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class KeycloakAdmin:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Keycloak admin API {method} {path} returned {exc.code}: {detail}") from exc


def wait_for_keycloak(base_url: str, attempts: int = 45) -> None:
    url = base_url.rstrip("/") + "/realms/master/.well-known/openid-configuration"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3):
                return
        except Exception:
            time.sleep(2)
    raise RuntimeError("Keycloak did not become ready at " + base_url)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("--realm-file", type=Path, default=Path(__file__).resolve().parents[1] / "keycloak" / "compilecare-realm.json")
    parser.add_argument("--base-url", default="http://localhost:8082")
    args = parser.parse_args()

    env = read_env(args.env_file)
    username = os.environ.get("KEYCLOAK_ADMIN", env.get("KEYCLOAK_ADMIN", "admin"))
    password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", env.get("KEYCLOAK_ADMIN_PASSWORD", ""))
    if not password or password.startswith("ChangeThis"):
        raise RuntimeError("Set a private KEYCLOAK_ADMIN_PASSWORD in local/.env before configuring the realm.")

    realm_spec = json.loads(args.realm_file.read_text(encoding="utf-8"))
    realm_name = realm_spec["realm"]
    client_spec = next(item for item in realm_spec["clients"] if item["clientId"] == "compilecare-web")
    desired_names = client_spec["defaultClientScopes"]
    scope_specs = {item["name"]: item for item in realm_spec["clientScopes"] if item["name"] in desired_names}

    wait_for_keycloak(args.base_url)
    token_request = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": username,
        "password": password,
    }).encode("ascii")
    token_url = args.base_url.rstrip("/") + "/realms/master/protocol/openid-connect/token"
    request = urllib.request.Request(
        token_url, data=token_request,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            token = json.loads(response.read())["access_token"]
    except Exception as exc:
        raise RuntimeError("Could not authenticate to the local Keycloak admin API. Check KEYCLOAK_ADMIN and KEYCLOAK_ADMIN_PASSWORD.") from exc

    admin = KeycloakAdmin(args.base_url, token)
    current_scopes = admin.request("GET", f"/admin/realms/{realm_name}/client-scopes")
    scope_ids = {scope["name"]: scope["id"] for scope in current_scopes}

    for name in desired_names:
        spec = scope_specs.get(name)
        if spec is None:
            raise RuntimeError(f"Realm definition is missing required default scope '{name}'.")
        existing_id = scope_ids.get(name)
        if existing_id:
            current = next(scope for scope in current_scopes if scope["name"] == name)
            current["description"] = spec.get("description", current.get("description"))
            current["protocol"] = spec["protocol"]
            current["attributes"] = spec.get("attributes", {})
            current["protocolMappers"] = spec.get("protocolMappers", [])
            admin.request("PUT", f"/admin/realms/{realm_name}/client-scopes/{existing_id}", current)
        else:
            admin.request("POST", f"/admin/realms/{realm_name}/client-scopes", spec)

    current_scopes = admin.request("GET", f"/admin/realms/{realm_name}/client-scopes")
    scope_ids = {scope["name"]: scope["id"] for scope in current_scopes}
    clients = admin.request(
        "GET",
        f"/admin/realms/{realm_name}/clients?clientId={urllib.parse.quote('compilecare-web')}",
    )
    if len(clients) != 1:
        raise RuntimeError("Expected exactly one compilecare-web client in the local realm.")
    client = clients[0]
    client_id = client["id"]

    # Keep an existing realm volume aligned with the checked-in client definition.
    # Realm import only runs on first initialization, so this reconciliation avoids
    # deleting local users or resetting the Keycloak data volume.
    desired_redirect_uris = client_spec.get("redirectUris", [])
    desired_web_origins = client_spec.get("webOrigins", [])
    if (set(client.get("redirectUris", [])) != set(desired_redirect_uris)
            or set(client.get("webOrigins", [])) != set(desired_web_origins)):
        client["redirectUris"] = desired_redirect_uris
        client["webOrigins"] = desired_web_origins
        admin.request("PUT", f"/admin/realms/{realm_name}/clients/{client_id}", client)

    defaults = admin.request(
        "GET",
        f"/admin/realms/{realm_name}/clients/{client_id}/default-client-scopes",
    )
    default_names = {scope["name"] for scope in defaults}
    for name in desired_names:
        if name not in default_names:
            admin.request(
                "PUT",
                f"/admin/realms/{realm_name}/clients/{client_id}/default-client-scopes/{scope_ids[name]}",
            )

    print("CompileCare local Keycloak client reconciled: redirect URIs and web origins")
    print("CompileCare local Keycloak scopes reconciled: " + ", ".join(desired_names))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
