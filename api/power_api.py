#!/usr/bin/env python3
"""Small authenticated HTTP wrapper for powerctl.py.

The HTTP request can select only a configured device ID. Routes, actions,
executable paths and all other process arguments are fixed by this file or the
local configuration. subprocess is always invoked with shell=False.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

from flask import Flask, Response, jsonify, request


API_VERSION = 1
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 512
MAX_REQUEST_BYTES = 1024


class ApiConfigError(ValueError):
    pass


class ControllerExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiSettings:
    config_path: Path
    controller_path: Path
    bind_host: str
    port: int
    bearer_token: str
    allowed_clients: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    command_timeout_seconds: int
    threads: int
    log_path: Path
    enabled_device_ids: frozenset[str]


ControllerRunner = Callable[[ApiSettings, str, str | None], tuple[dict[str, Any], int]]


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiConfigError(f"{path} muss ein JSON-Objekt sein")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ApiConfigError(f"{path} muss eine nicht-leere Zeichenkette sein")
    return value


def _require_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ApiConfigError(f"{path} muss eine Ganzzahl zwischen {minimum} und {maximum} sein")
    return value


def _resolve_from_config(config_path: Path, value: str) -> Path:
    configured = Path(value).expanduser()
    return configured if configured.is_absolute() else (config_path.parent / configured).resolve()


def load_settings(config_path: Path) -> ApiSettings:
    resolved_config = config_path.expanduser().resolve()
    try:
        root = json.loads(resolved_config.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ApiConfigError(f"Konfiguration nicht gefunden: {resolved_config}") from error
    except json.JSONDecodeError as error:
        raise ApiConfigError(
            f"Ungueltiges JSON in {resolved_config}: Zeile {error.lineno}, Spalte {error.colno}"
        ) from error

    root = _require_object(root, "Konfiguration")
    api = _require_object(root.get("api"), "api")

    token = _require_string(api.get("bearerToken"), "api.bearerToken")
    if token != token.strip() or any(character.isspace() for character in token):
        raise ApiConfigError("api.bearerToken darf keine Leerzeichen enthalten")
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        raise ApiConfigError(
            f"api.bearerToken muss zwischen {MIN_TOKEN_LENGTH} und {MAX_TOKEN_LENGTH} Zeichen lang sein"
        )
    if "CHANGE_ME" in token.upper() or "REPLACE" in token.upper():
        raise ApiConfigError("api.bearerToken enthaelt noch den Platzhalter")

    raw_networks = api.get("allowedClients")
    if not isinstance(raw_networks, list) or not raw_networks:
        raise ApiConfigError("api.allowedClients muss eine nicht-leere JSON-Liste sein")
    allowed_clients: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for index, item in enumerate(raw_networks):
        text = _require_string(item, f"api.allowedClients[{index}]")
        try:
            allowed_clients.append(ipaddress.ip_network(text, strict=False))
        except ValueError as error:
            raise ApiConfigError(f"Ungueltiges Netz in api.allowedClients[{index}]: {text}") from error

    raw_devices = root.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise ApiConfigError("devices muss eine nicht-leere JSON-Liste sein")
    defaults = _require_object(root.get("defaults"), "defaults")
    shutdown_defaults = _require_object(defaults.get("shutdown"), "defaults.shutdown")
    enabled_ids: set[str] = set()
    all_ids: set[str] = set()
    for index, raw_device in enumerate(raw_devices):
        device = _require_object(raw_device, f"devices[{index}]")
        device_id = _require_string(device.get("id"), f"devices[{index}].id")
        if device_id in all_ids:
            raise ApiConfigError(f"Doppelte Device-ID: {device_id}")
        all_ids.add(device_id)
        enabled = device.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ApiConfigError(f"devices[{index}].enabled muss true oder false sein")
        if enabled:
            device_shutdown = device.get("shutdown")
            if device_shutdown is not None:
                device_shutdown = _require_object(device_shutdown, f"devices[{index}].shutdown")
            shutdown = {**shutdown_defaults, **(device_shutdown or {})}
            if shutdown.get("transport") == "windows-native":
                username = _require_string(
                    shutdown.get("username"), f"devices[{index}].shutdown.username"
                )
                password = _require_string(
                    shutdown.get("password"), f"devices[{index}].shutdown.password"
                )
                if any(marker in password.upper() for marker in ("CHANGE_ME", "EINTRAGEN", "REPLACE")):
                    raise ApiConfigError(
                        f"devices[{index}].shutdown.password enthaelt noch einen Platzhalter"
                    )
                if "\\" not in username:
                    raise ApiConfigError(
                        f"devices[{index}].shutdown.username muss HOSTNAME\\kiosk-power verwenden"
                    )
            enabled_ids.add(device_id)
    if not enabled_ids:
        raise ApiConfigError("Mindestens ein Device muss aktiviert sein")

    controller_path = Path(__file__).resolve().with_name("powerctl.py")
    if not controller_path.is_file():
        raise ApiConfigError(f"powerctl.py nicht gefunden: {controller_path}")

    bind_host = _require_string(api.get("bindHost", "0.0.0.0"), "api.bindHost")
    port = _require_int(api.get("port", 8787), "api.port", 1024, 65535)
    timeout = _require_int(
        api.get("commandTimeoutSeconds", 180),
        "api.commandTimeoutSeconds",
        5,
        900,
    )
    threads = _require_int(api.get("threads", 8), "api.threads", 1, 64)
    log_file = _require_string(api.get("logFile", "power-api.log"), "api.logFile")

    return ApiSettings(
        config_path=resolved_config,
        controller_path=controller_path,
        bind_host=bind_host,
        port=port,
        bearer_token=token,
        allowed_clients=tuple(allowed_clients),
        command_timeout_seconds=timeout,
        threads=threads,
        log_path=_resolve_from_config(resolved_config, log_file),
        enabled_device_ids=frozenset(enabled_ids),
    )


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("geekom_power_api")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing_handler in tuple(logger.handlers):
        logger.removeHandler(existing_handler)
        existing_handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def _python_launcher() -> list[str]:
    if os.name == "nt":
        launcher = shutil.which("py.exe") or shutil.which("py")
        if launcher is None:
            raise ControllerExecutionError("Windows Python Launcher 'py' wurde nicht gefunden")
        return [launcher, "-3"]
    return [sys.executable]


def run_controller(settings: ApiSettings, action: str, device_id: str | None) -> tuple[dict[str, Any], int]:
    if action not in {"validate", "status", "wake", "shutdown"}:
        raise ControllerExecutionError("Interne ungueltige Controller-Aktion")
    if action == "validate" and device_id is not None:
        raise ControllerExecutionError("validate akzeptiert keine Device-ID")
    if action != "validate" and device_id not in settings.enabled_device_ids:
        raise ControllerExecutionError("Interne ungueltige Device-ID")

    command = [
        *_python_launcher(),
        str(settings.controller_path),
        "--config",
        str(settings.config_path),
        action,
    ]
    if device_id is not None:
        command.append(device_id)

    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    try:
        completed = subprocess.run(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.command_timeout_seconds,
            cwd=settings.controller_path.parent,
            env=environment,
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ControllerExecutionError("Power-Controller hat das API-Zeitlimit ueberschritten") from error
    except OSError as error:
        raise ControllerExecutionError(f"Power-Controller konnte nicht gestartet werden: {error}") from error

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        diagnostic = completed.stderr.strip()[-500:]
        raise ControllerExecutionError(
            f"Power-Controller lieferte kein gueltiges JSON; Diagnose: {diagnostic or 'keine'}"
        ) from error
    if not isinstance(payload, dict):
        raise ControllerExecutionError("Power-Controller lieferte kein JSON-Objekt")
    return payload, completed.returncode


def _json_error(error_type: str, message: str, status: int) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": {"type": error_type, "message": message}}), status


def create_app(config_path: Path, controller_runner: ControllerRunner = run_controller) -> Flask:
    settings = load_settings(config_path)
    logger = configure_logging(settings.log_path)
    locks = {device_id: threading.Lock() for device_id in settings.enabled_device_ids}

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.extensions["geekom_power_api_settings"] = settings

    def client_allowed(remote_address: str | None) -> bool:
        if remote_address is None:
            return False
        try:
            address = ipaddress.ip_address(remote_address)
        except ValueError:
            return False
        return any(address in network for network in settings.allowed_clients)

    @app.before_request
    def authenticate() -> tuple[Response, int] | None:
        if not request.path.startswith("/api/"):
            return None
        if not client_allowed(request.remote_addr):
            logger.warning("request_denied remote=%s reason=client_not_allowed", request.remote_addr)
            return _json_error("forbidden", "Client-IP ist nicht freigegeben", 403)

        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not supplied_token:
            response, status = _json_error("unauthorized", "Bearer-Token fehlt oder ist ungueltig", 401)
            response.headers["WWW-Authenticate"] = "Bearer"
            return response, status
        if not hmac.compare_digest(supplied_token.encode("utf-8"), settings.bearer_token.encode("utf-8")):
            logger.warning("request_denied remote=%s reason=invalid_token", request.remote_addr)
            response, status = _json_error("unauthorized", "Bearer-Token fehlt oder ist ungueltig", 401)
            response.headers["WWW-Authenticate"] = "Bearer"
            return response, status
        return None

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/healthz")
    def health() -> Response:
        return jsonify({"ok": True, "service": "geekom-power-api", "apiVersion": API_VERSION})

    def execute(requested_action: str) -> tuple[Response, int]:
        if set(request.args.keys()) != {"id"} or len(request.args.getlist("id")) != 1:
            return _json_error("invalid_request", "Genau ein Query-Parameter 'id' ist erforderlich", 400)
        device_id = request.args.get("id", "")
        if device_id not in settings.enabled_device_ids:
            return _json_error("unknown_device", "Unbekannte oder deaktivierte Device-ID", 404)

        controller_action = "wake" if requested_action == "startup" else requested_action
        lock = locks[device_id]
        if not lock.acquire(blocking=False):
            return _json_error("device_busy", "Fuer dieses Device laeuft bereits eine Aktion", 409)

        try:
            payload, exit_code = controller_runner(settings, controller_action, device_id)
        except ControllerExecutionError as error:
            logger.error(
                "controller_error remote=%s device=%s action=%s error=%s",
                request.remote_addr,
                device_id,
                requested_action,
                error,
            )
            status = 504 if "Zeitlimit" in str(error) else 500
            return _json_error("controller_error", str(error), status)
        finally:
            lock.release()

        logger.info(
            "action remote=%s device=%s action=%s exitCode=%s ok=%s",
            request.remote_addr,
            device_id,
            requested_action,
            exit_code,
            payload.get("ok"),
        )

        if exit_code == 0:
            status = 200
        elif requested_action == "status" and exit_code == 10:
            status = 200
        elif exit_code == 10:
            status = 502
        elif exit_code == 21:
            status = 404
        else:
            status = 500
        return jsonify(payload), status

    @app.post("/api/v1/status")
    def status() -> tuple[Response, int]:
        return execute("status")

    @app.post("/api/v1/startup")
    def startup() -> tuple[Response, int]:
        return execute("startup")

    @app.post("/api/v1/shutdown")
    def shutdown() -> tuple[Response, int]:
        return execute("shutdown")

    @app.errorhandler(404)
    def not_found(_: Exception) -> tuple[Response, int]:
        return _json_error("not_found", "Route nicht gefunden", 404)

    @app.errorhandler(405)
    def method_not_allowed(_: Exception) -> tuple[Response, int]:
        return _json_error("method_not_allowed", "HTTP-Methode nicht erlaubt", 405)

    @app.errorhandler(413)
    def request_too_large(_: Exception) -> tuple[Response, int]:
        return _json_error("request_too_large", "Request ist zu gross", 413)

    logger.info(
        "api_initialized bind=%s port=%s devices=%s",
        settings.bind_host,
        settings.port,
        len(settings.enabled_device_ids),
    )
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GEEKOM Power Controller HTTP API")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().with_name("config.json"),
        help="Pfad zur gemeinsamen powerctl/API-Konfiguration",
    )
    parser.add_argument("--check", action="store_true", help="Konfiguration pruefen und beenden")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(args.config)
        if args.check:
            payload, exit_code = run_controller(settings, "validate", None)
            print(json.dumps({"api": {"ok": True}, "controller": payload}, ensure_ascii=False, indent=2))
            return exit_code

        app = create_app(args.config)
        from waitress import serve

        serve(
            app,
            host=settings.bind_host,
            port=settings.port,
            threads=settings.threads,
        )
        return 0
    except (ApiConfigError, ControllerExecutionError) as error:
        print(f"FEHLER: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
