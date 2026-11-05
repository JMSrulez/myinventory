"""Local Meraki inventory collector."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

API_ROOT = "https://api.meraki.com/api/v1/"
APP_DIR = Path(__file__).resolve().parent
INVENTORY_DB_PATH = Path(os.environ.get(
    "MERAKI_INVENTORY_DATABASE", str(APP_DIR / "data" / "inventory.sqlite3")
))
API_KEY: str | None = None
CONFIGURED_API_KEY: str | None = None
ORGANIZATIONS: list[dict[str, Any]] = []
NETWORKS: dict[str, dict[str, Any]] = {}
PROXMOX_SESSION: dict[str, str] | None = None
PROXMOX_INFO: dict[str, Any] | None = None


def load_configured_api_key() -> str | None:
    secret_file = os.environ.get("MERAKI_API_KEY_FILE")
    if secret_file:
        try:
            key = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit("Impossible de lire le fichier de clé API Meraki configuré.") from exc
        if key:
            return key
    return os.environ.get("MERAKI_API_KEY", "").strip() or None


CONFIGURED_API_KEY = load_configured_api_key()


def initialize_inventory_database() -> None:
    INVENTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(INVENTORY_DB_PATH) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS inventory_networks (
                network_id TEXT PRIMARY KEY,
                network_name TEXT NOT NULL,
                organization_id TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_devices (
                network_id TEXT NOT NULL REFERENCES inventory_networks(network_id) ON DELETE CASCADE,
                device_key TEXT NOT NULL,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                serial TEXT NOT NULL,
                management_ip TEXT NOT NULL,
                current_version TEXT,
                status TEXT,
                PRIMARY KEY (network_id, device_key)
            );
        """)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(inventory_devices)")
        }
        if "current_version" not in columns:
            connection.execute("ALTER TABLE inventory_devices ADD COLUMN current_version TEXT")
        if "status" not in columns:
            connection.execute("ALTER TABLE inventory_devices ADD COLUMN status TEXT")


def read_inventory() -> list[dict[str, Any]]:
    with sqlite3.connect(INVENTORY_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        networks = connection.execute(
            "SELECT * FROM inventory_networks ORDER BY network_name COLLATE NOCASE"
        ).fetchall()
        result = []
        for network in networks:
            devices = connection.execute(
                "SELECT name, model, serial, management_ip, current_version, status FROM inventory_devices "
                "WHERE network_id = ? ORDER BY name COLLATE NOCASE, serial",
                (network["network_id"],),
            ).fetchall()
            result.append({
                "id": network["network_id"],
                "name": network["network_name"],
                "organizationId": network["organization_id"],
                "organizationName": network["organization_name"],
                "capturedAt": network["captured_at"],
                "devices": [dict(device) for device in devices],
            })
    return result


def read_inventory_network(network_id: str) -> dict[str, str] | None:
    with sqlite3.connect(INVENTORY_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        network = connection.execute(
            "SELECT network_id, network_name, organization_id, organization_name "
            "FROM inventory_networks WHERE network_id = ?",
            (network_id,),
        ).fetchone()
    if network is None:
        return None
    return {
        "id": network["network_id"],
        "name": network["network_name"],
        "organizationId": network["organization_id"],
        "organizationName": network["organization_name"],
    }


def delete_inventory_network(network_id: str) -> bool:
    with sqlite3.connect(INVENTORY_DB_PATH) as connection:
        connection.execute("DELETE FROM inventory_devices WHERE network_id = ?", (network_id,))
        result = connection.execute(
            "DELETE FROM inventory_networks WHERE network_id = ?", (network_id,)
        )
        return result.rowcount > 0


def store_inventory(networks: list[dict[str, Any]]) -> int:
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    device_count = 0
    with sqlite3.connect(INVENTORY_DB_PATH) as connection:
        for network in networks:
            connection.execute(
                "INSERT INTO inventory_networks "
                "(network_id, network_name, organization_id, organization_name, captured_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(network_id) DO UPDATE SET "
                "network_name = excluded.network_name, "
                "organization_id = excluded.organization_id, "
                "organization_name = excluded.organization_name, "
                "captured_at = excluded.captured_at",
                (
                    network["id"], network["name"], network["organizationId"],
                    network["organizationName"], captured_at,
                ),
            )
            connection.execute(
                "DELETE FROM inventory_devices WHERE network_id = ?",
                (network["id"],),
            )
            for index, device in enumerate(network["devices"]):
                serial = str(device.get("serial") or "")
                device_key = serial or str(device.get("mac") or "") or "device-" + str(index)
                connection.execute(
                    "INSERT INTO inventory_devices "
                    "(network_id, device_key, name, model, serial, management_ip, current_version, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        network["id"], device_key, str(device.get("name") or ""),
                        str(device.get("model") or ""), serial,
                        str(device.get("lanIp") or device.get("managementIp") or ""),
                        str(device.get("firmware") or ""), str(device.get("status") or ""),
                    ),
                )
                device_count += 1
    return device_count


class MerakiError(Exception):
    pass


class ProxmoxError(Exception):
    pass


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int,
                         message: str, headers: Any, new_url: str) -> None:
        # Never forward a Proxmox API token to a redirect target.
        return None


def proxmox_get(base_url: str, token_header: str, endpoint: str) -> dict[str, Any] | list[Any]:
    request = Request(base_url + "/api2/json/" + endpoint, headers={
        "Accept": "application/json",
        "Authorization": token_header,
        "User-Agent": "MerakiInventoryLocal/0.1",
    })
    try:
        with build_opener(NoRedirectHandler()).open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ProxmoxError("Proxmox rejected the API token. Check its ID, secret, and permissions.") from exc
        raise ProxmoxError("Proxmox returned HTTP " + str(exc.code) + ".") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProxmoxError("Could not securely reach the Proxmox API. Check the URL, certificate, and connection.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProxmoxError("Proxmox returned invalid JSON.") from exc
    if not isinstance(payload, dict) or "data" not in payload:
        raise ProxmoxError("Unexpected Proxmox API response.")
    return payload["data"]


def validate_proxmox_credentials(data: dict[str, Any]) -> tuple[str, str, str]:
    base_url = data.get("baseUrl")
    username = data.get("username")
    realm = data.get("realm")
    token_id = data.get("tokenId")
    token_secret = data.get("tokenSecret")
    if not all(isinstance(value, str) for value in (base_url, username, realm, token_id, token_secret)):
        raise ValueError("Enter the Proxmox URL and all API token fields.")
    base_url = base_url.strip().rstrip("/")
    username, realm, token_id = username.strip(), realm.strip(), token_id.strip()
    if not base_url or len(base_url) > 512 or any(char.isspace() for char in base_url):
        raise ValueError("Enter a valid HTTPS Proxmox URL.")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid HTTPS Proxmox URL.") from exc
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("Enter a valid HTTPS Proxmox URL without credentials, query, or fragment.")
    for label, value in (("username", username), ("realm", realm), ("token ID", token_id)):
        if not value or len(value) > 128 or any(char in value for char in "@!/=\r\n") or any(char.isspace() for char in value):
            raise ValueError("Enter a valid Proxmox " + label + ".")
    if not token_secret or len(token_secret) > 4096 or "\r" in token_secret or "\n" in token_secret:
        raise ValueError("Enter a valid API token secret.")
    authority = parsed.netloc
    path_prefix = parsed.path.rstrip("/")
    normalized_url = "https://" + authority + path_prefix
    token_header = "PVEAPIToken=" + username + "@" + realm + "!" + token_id + "=" + token_secret
    return normalized_url, username + "@" + realm, token_header


def meraki_get(path: str) -> list[dict[str, Any]]:
    """Fetch every page from a Meraki endpoint, following Link headers."""
    if not API_KEY:
        raise MerakiError("Connectez-vous avec une clé API Meraki.")

    next_url = urljoin(API_ROOT, path.lstrip("/"))
    results: list[dict[str, Any]] = []
    visited: set[str] = set()
    while next_url:
        parsed = urlsplit(next_url)
        if parsed.scheme != "https" or parsed.hostname != "api.meraki.com":
            raise MerakiError("Lien de pagination Meraki invalide.")
        if next_url in visited:
            raise MerakiError("Boucle détectée dans la pagination Meraki.")
        visited.add(next_url)
        request = Request(next_url, headers={
            "Accept": "application/json",
            "X-Cisco-Meraki-API-Key": API_KEY,
            "User-Agent": "MerakiInventoryLocal/0.1",
        })
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    link_header = response.headers.get("Link", "")
                break
            except HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    try:
                        delay = min(float(exc.headers.get("Retry-After", "1")), 10)
                    except ValueError:
                        delay = 1
                    time.sleep(max(delay, 0.1))
                    continue
                try:
                    details = json.loads(exc.read().decode("utf-8"))
                    message = details.get("errors", [str(details)])
                    if isinstance(message, list):
                        message = "; ".join(str(item) for item in message)
                except (ValueError, UnicodeDecodeError):
                    message = ""
                if exc.code == 401:
                    message = "Clé API invalide ou non autorisée. Vérifiez la clé et ses permissions."
                elif exc.code == 403:
                    message = "Accès refusé par Meraki. Vérifiez les permissions de la clé API."
                elif not message:
                    message = "Réponse Meraki inattendue (HTTP " + str(exc.code) + ")."
                raise MerakiError(str(message)) from exc
            except (URLError, TimeoutError) as exc:
                raise MerakiError("Impossible de joindre l’API Meraki. Vérifiez votre connexion.") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MerakiError("La réponse de l’API Meraki n’est pas un JSON valide.") from exc

        if not isinstance(payload, list):
            raise MerakiError("Format de réponse Meraki inattendu.")
        results.extend(item for item in payload if isinstance(item, dict))
        next_url = ""
        for link in re.split(r",(?=\s*<)", link_header):
            match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)', link)
            if match and match.group(2).strip() == "next":
                next_url = urljoin(urljoin(API_ROOT, path.lstrip("/")), match.group(1))
                break
    return results


def capture_one_network(network: dict[str, str]) -> dict[str, Any]:
    network_id = network["id"]
    organization_id = network["organizationId"]
    query = urlencode([("perPage", "1000"), ("networkIds[]", network_id)])
    availability = meraki_get(
        "organizations/" + quote(organization_id, safe="")
        + "/devices/availabilities?" + query
    )
    device_statuses = {
        str(item.get("serial")): str(item.get("status") or "")
        for item in availability if item.get("serial")
    }
    devices = meraki_get("networks/" + quote(network_id, safe="") + "/devices")
    for device in devices:
        device["status"] = device_statuses.get(str(device.get("serial") or ""), "")
    return {**network, "devices": devices}


class Handler(BaseHTTPRequestHandler):
    server_version = "MerakiInventory/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Access logs must not retain query parameters, which can contain secrets
        # when a browser submits a form with GET.
        if args and isinstance(args[0], str):
            method, separator, remainder = args[0].partition(" ")
            target, target_separator, version = remainder.partition(" ")
            if separator and target_separator:
                safe_request_line = method + " " + target.split("?", 1)[0] + " " + version
                args = (safe_request_line, *args[1:])
        super().log_message(format, *args)

    def is_local_request(self) -> bool:
        host_header = self.headers.get("Host", "")
        try:
            host = urlsplit("//" + host_header)
            allowed_hosts = {
                item.strip().lower()
                for item in os.environ.get(
                    "MERAKI_INVENTORY_ALLOWED_HOSTS", "127.0.0.1,localhost"
                ).split(",")
                if item.strip()
            }
            if not host.hostname or host.hostname.lower() not in allowed_hosts:
                return False
        except ValueError:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urlsplit(origin)
            return parsed_origin.scheme in ("http", "https") and parsed_origin.netloc.lower() == host_header.lower()
        return True

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1_000_000:
                raise ValueError
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Requête JSON invalide.") from exc
        if not isinstance(data, dict):
            raise ValueError("Le corps doit être un objet JSON.")
        return data

    def do_GET(self) -> None:
        if not self.is_local_request():
            self.send_json(403, {"error": "Accès local uniquement."})
            return
        if self.path.startswith("/proxmox?"):
            self.send_json(405, {
                "error": "Credentials must not be sent in the URL. Reload the page and submit the form again."
            })
            return
        if self.path == "/inventory":
            page = (APP_DIR / "inventory.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == "/proxmox":
            page = (APP_DIR / "proxmox.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == "/api/proxmox/status":
            self.send_json(200, {
                "connected": PROXMOX_SESSION is not None,
                "baseUrl": PROXMOX_SESSION.get("baseUrl", "") if PROXMOX_SESSION else "",
                "username": PROXMOX_SESSION.get("username", "") if PROXMOX_SESSION else "",
                "version": PROXMOX_INFO.get("version", {}) if PROXMOX_INFO else {},
                "nodes": PROXMOX_INFO.get("nodes", []) if PROXMOX_INFO else [],
            })
            return
        if self.path == "/api/status":
            self.send_json(200, {
                "connected": API_KEY is not None,
                "configured": CONFIGURED_API_KEY is not None,
                "organizations": ORGANIZATIONS,
            })
            return
        if self.path == "/api/inventory":
            try:
                self.send_json(200, {"networks": read_inventory()})
            except sqlite3.Error as exc:
                self.send_json(500, {"error": "Impossible de lire l’inventaire enregistré."})
            return
        if self.path in ("/", "/index.html"):
            page = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_json(404, {"error": "Page introuvable."})

    def do_POST(self) -> None:
        global API_KEY, ORGANIZATIONS, NETWORKS, PROXMOX_SESSION, PROXMOX_INFO
        if not self.is_local_request():
            self.send_json(403, {"error": "Accès local uniquement."})
            return
        try:
            data = self.read_json()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        if self.path == "/api/proxmox/connect":
            try:
                base_url, username, token_header = validate_proxmox_credentials(data)
                version = proxmox_get(base_url, token_header, "version")
                nodes = proxmox_get(base_url, token_header, "nodes")
                if not isinstance(version, dict) or not isinstance(nodes, list):
                    raise ProxmoxError("Unexpected Proxmox API response.")
                node_summary = [
                    {
                        "node": str(node.get("node") or ""),
                        "status": str(node.get("status") or "unknown"),
                        "maxcpu": node.get("maxcpu"),
                        "maxmem": node.get("maxmem"),
                    }
                    for node in nodes if isinstance(node, dict)
                ]
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except ProxmoxError as exc:
                self.send_json(502, {"error": str(exc)})
                return
            PROXMOX_SESSION = {
                "baseUrl": base_url,
                "username": username,
                "tokenHeader": token_header,
            }
            PROXMOX_INFO = {
                "version": {
                    "version": str(version.get("version") or ""),
                    "release": str(version.get("release") or ""),
                },
                "nodes": node_summary,
            }
            self.send_json(200, {
                "connected": True,
                "baseUrl": base_url,
                "username": username,
                **PROXMOX_INFO,
            })
            return
        if self.path == "/api/proxmox/disconnect":
            PROXMOX_SESSION = None
            PROXMOX_INFO = None
            self.send_json(200, {"connected": False})
            return
        if self.path == "/api/connect":
            key = data.get("apiKey")
            if CONFIGURED_API_KEY:
                API_KEY = CONFIGURED_API_KEY
            elif isinstance(key, str) and key.strip():
                API_KEY = key.strip()
            else:
                self.send_json(400, {"error": "Saisissez une clé API Meraki."})
                return
            try:
                ORGANIZATIONS = meraki_get("organizations")
                NETWORKS = {}
            except MerakiError as exc:
                API_KEY = None
                ORGANIZATIONS = []
                NETWORKS = {}
                self.send_json(502, {"error": str(exc)})
                return
            self.send_json(200, {
                "organizations": ORGANIZATIONS,
                "configured": CONFIGURED_API_KEY is not None,
            })
            return
        if self.path == "/api/networks":
            if API_KEY is None:
                self.send_json(401, {"error": "Connectez-vous d’abord à Meraki."})
                return
            org_ids = data.get("organizationIds")
            if not isinstance(org_ids, list) or not all(isinstance(item, str) for item in org_ids):
                self.send_json(400, {"error": "Sélection d’organisations invalide."})
                return
            known_ids = {str(org.get("id", "")) for org in ORGANIZATIONS}
            if any(org_id not in known_ids for org_id in org_ids):
                self.send_json(400, {"error": "Une organisation sélectionnée n’est pas disponible."})
                return
            networks: list[dict[str, Any]] = []
            try:
                for org_id in dict.fromkeys(org_ids):
                    for network in meraki_get("organizations/" + org_id + "/networks"):
                        networks.append({**network, "organizationId": org_id})
            except MerakiError as exc:
                self.send_json(502, {"error": str(exc)})
                return
            NETWORKS = {
                str(network.get("id", "")): network
                for network in networks
                if network.get("id")
            }
            self.send_json(200, {"networks": networks})
            return
        if self.path == "/api/capture":
            if API_KEY is None:
                self.send_json(401, {"error": "Connectez-vous d’abord à Meraki."})
                return
            network_ids = data.get("networkIds")
            if not isinstance(network_ids, list) or not network_ids or not all(
                isinstance(item, str) for item in network_ids
            ):
                self.send_json(400, {"error": "Sélection de réseaux invalide."})
                return
            selected_ids = list(dict.fromkeys(network_ids))
            if any(network_id not in NETWORKS for network_id in selected_ids):
                self.send_json(400, {"error": "Un réseau sélectionné n’est pas disponible. Rechargez les réseaux."})
                return
            captured_networks: list[dict[str, Any]] = []
            try:
                status_by_org: dict[str, dict[str, str]] = {}
                for network_id in selected_ids:
                    source = NETWORKS[network_id]
                    organization_id = str(source.get("organizationId", ""))
                    if organization_id in status_by_org:
                        continue
                    organization_network_ids = [
                        selected_id for selected_id in selected_ids
                        if str(NETWORKS[selected_id].get("organizationId", "")) == organization_id
                    ]
                    query = urlencode([
                        ("perPage", "1000"),
                        *[("networkIds[]", selected_id) for selected_id in organization_network_ids],
                    ])
                    availability = meraki_get(
                        "organizations/" + quote(organization_id, safe="")
                        + "/devices/availabilities?" + query
                    )
                    status_by_org[organization_id] = {
                        str(item.get("serial")): str(item.get("status") or "")
                        for item in availability if item.get("serial")
                    }
                for network_id in selected_ids:
                    source = NETWORKS[network_id]
                    devices = meraki_get(
                        "networks/" + quote(network_id, safe="") + "/devices"
                    )
                    organization_id = str(source.get("organizationId", ""))
                    device_statuses = status_by_org.get(organization_id, {})
                    for device in devices:
                        device["status"] = device_statuses.get(str(device.get("serial") or ""), "")
                    organization = next(
                        (item for item in ORGANIZATIONS if str(item.get("id", "")) == organization_id),
                        {},
                    )
                    captured_networks.append({
                        "id": network_id,
                        "name": str(source.get("name") or "Réseau sans nom"),
                        "organizationId": organization_id,
                        "organizationName": str(organization.get("name") or "Organisation"),
                        "devices": devices,
                    })
            except MerakiError as exc:
                self.send_json(502, {"error": str(exc)})
                return
            try:
                device_count = store_inventory(captured_networks)
            except sqlite3.Error as exc:
                self.send_json(500, {"error": "Impossible d’enregistrer l’inventaire."})
                return
            self.send_json(200, {
                "networkCount": len(captured_networks),
                "deviceCount": device_count,
            })
            return
        if self.path == "/api/inventory/refresh":
            if API_KEY is None:
                self.send_json(401, {"error": "Connectez-vous d’abord à Meraki."})
                return
            network_id = data.get("networkId")
            if not isinstance(network_id, str) or not network_id:
                self.send_json(400, {"error": "Sélection de réseau invalide."})
                return
            try:
                network = read_inventory_network(network_id)
            except sqlite3.Error:
                self.send_json(500, {"error": "Impossible de lire l’inventaire enregistré."})
                return
            if network is None:
                self.send_json(404, {"error": "Réseau introuvable dans l’inventaire."})
                return
            try:
                captured_network = capture_one_network(network)
                device_count = store_inventory([captured_network])
            except MerakiError as exc:
                self.send_json(502, {"error": str(exc)})
                return
            except sqlite3.Error:
                self.send_json(500, {"error": "Impossible d’enregistrer l’inventaire."})
                return
            self.send_json(200, {"networkId": network_id, "deviceCount": device_count})
            return
        if self.path == "/api/inventory/delete":
            network_id = data.get("networkId")
            if not isinstance(network_id, str) or not network_id:
                self.send_json(400, {"error": "Sélection de réseau invalide."})
                return
            try:
                deleted = delete_inventory_network(network_id)
            except sqlite3.Error:
                self.send_json(500, {"error": "Impossible de supprimer l’inventaire."})
                return
            if not deleted:
                self.send_json(404, {"error": "Réseau introuvable dans l’inventaire."})
                return
            self.send_json(200, {"networkId": network_id, "deleted": True})
            return
        if self.path == "/api/disconnect":
            API_KEY = None
            ORGANIZATIONS = []
            NETWORKS = {}
            self.send_json(200, {"connected": False})
            return
        self.send_json(404, {"error": "Route introuvable."})


def main() -> None:
    initialize_inventory_database()
    host = os.environ.get("MERAKI_INVENTORY_HOST", "127.0.0.1")
    port = int(os.environ.get("MERAKI_INVENTORY_PORT", "8086"))
    server = ThreadingHTTPServer((host, port), Handler)
    print("Inventaire Meraki disponible sur http://" + host + ":" + str(port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
