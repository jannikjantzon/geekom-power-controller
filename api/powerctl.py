#!/usr/bin/env python3
"""Backend-neutraler Controller fuer Wake-on-LAN und agentlosen RPC-Shutdown.

Stdout enthaelt immer genau ein JSON-Dokument. Diagnosen gehoeren nach stderr.
Das Programm benoetigt Python >= 3.11 sowie Windows shutdown.exe oder Samba net.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_DEVICE_FAILURE = 10
EXIT_CONFIG_ERROR = 20
EXIT_SELECTION_ERROR = 21
EXIT_INTERNAL_ERROR = 30

MAC_PATTERN = re.compile(r"^[0-9A-F]{12}$")


class ConfigError(ValueError):
    pass


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class HealthCheck:
    port: int
    connect_timeout_seconds: float


@dataclass(frozen=True)
class ShutdownSettings:
    transport: str
    executable: str
    username: str | None
    password: str | None
    credential_file: Path | None
    command_timeout_seconds: int


@dataclass(frozen=True)
class Device:
    id: str
    mac: str
    host: str
    broadcast: str
    wol_port: int
    enabled: bool
    health_check: HealthCheck
    shutdown: ShutdownSettings


@dataclass(frozen=True)
class Settings:
    wol_packets: int
    wol_packet_interval_seconds: float
    wake_timeout_seconds: float
    shutdown_timeout_seconds: float
    poll_interval_seconds: float
    required_consecutive_checks: int
    max_concurrency: int


@dataclass(frozen=True)
class Configuration:
    settings: Settings
    devices: tuple[Device, ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", value.strip()).upper()
    if not MAC_PATTERN.fullmatch(compact):
        raise ConfigError(f"Ungueltige MAC-Adresse: {value!r}")
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} muss ein JSON-Objekt sein")
    return value


def require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} muss eine nicht-leere Zeichenkette sein")
    return value.strip()


def require_secret(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path} muss eine nicht-leere Zeichenkette sein")
    return value


def require_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{path} muss eine Ganzzahl zwischen {minimum} und {maximum} sein")
    return value


def require_number(value: Any, path: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        raise ConfigError(f"{path} muss eine Zahl zwischen {minimum} und {maximum} sein")
    return float(value)


def merged(defaults: dict[str, Any], override: Any, path: str) -> dict[str, Any]:
    override_object = require_object(override, path) if override is not None else {}
    return {**defaults, **override_object}


def resolve_config_path(config_directory: Path, value: Any, path: str) -> Path:
    configured = Path(require_string(value, path)).expanduser()
    return configured if configured.is_absolute() else (config_directory / configured).resolve()


def load_config(path: Path) -> Configuration:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Konfiguration nicht gefunden: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"Ungueltiges JSON in {path}: Zeile {error.lineno}, Spalte {error.colno}") from error

    root = require_object(raw, "Konfiguration")
    if root.get("version") != 1:
        raise ConfigError("Konfiguration.version muss 1 sein")

    defaults = require_object(root.get("defaults"), "defaults")
    health_defaults = require_object(defaults.get("healthCheck"), "defaults.healthCheck")
    shutdown_defaults = require_object(defaults.get("shutdown"), "defaults.shutdown")
    config_directory = path.resolve().parent

    settings = Settings(
        wol_packets=require_int(defaults.get("wolPackets", 3), "defaults.wolPackets", 1, 20),
        wol_packet_interval_seconds=require_number(
            defaults.get("wolPacketIntervalMs", 250), "defaults.wolPacketIntervalMs", 0, 10_000
        ) / 1000,
        wake_timeout_seconds=require_number(
            defaults.get("wakeTimeoutSeconds", 90), "defaults.wakeTimeoutSeconds", 1, 900
        ),
        shutdown_timeout_seconds=require_number(
            defaults.get("shutdownTimeoutSeconds", 90), "defaults.shutdownTimeoutSeconds", 1, 900
        ),
        poll_interval_seconds=require_number(
            defaults.get("pollIntervalSeconds", 2), "defaults.pollIntervalSeconds", 0.2, 60
        ),
        required_consecutive_checks=require_int(
            defaults.get("requiredConsecutiveChecks", 2), "defaults.requiredConsecutiveChecks", 1, 10
        ),
        max_concurrency=require_int(defaults.get("maxConcurrency", 16), "defaults.maxConcurrency", 1, 256),
    )

    raw_devices = root.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise ConfigError("devices muss eine nicht-leere JSON-Liste sein")

    devices: list[Device] = []
    ids: set[str] = set()
    macs: set[str] = set()

    for index, raw_device in enumerate(raw_devices):
        item_path = f"devices[{index}]"
        item = require_object(raw_device, item_path)
        device_id = require_string(item.get("id"), f"{item_path}.id")
        mac = normalize_mac(require_string(item.get("mac"), f"{item_path}.mac"))
        if device_id in ids:
            raise ConfigError(f"Doppelte Device-ID: {device_id}")
        if mac in macs:
            raise ConfigError(f"Doppelte MAC-Adresse: {mac}")
        ids.add(device_id)
        macs.add(mac)

        health_raw = merged(health_defaults, item.get("healthCheck"), f"{item_path}.healthCheck")
        shutdown_raw = merged(shutdown_defaults, item.get("shutdown"), f"{item_path}.shutdown")
        shutdown_transport = require_string(
            shutdown_raw.get("transport"), f"{item_path}.shutdown.transport"
        )
        if shutdown_transport not in {"windows-native", "samba-rpc"}:
            raise ConfigError(
                f"{item_path}.shutdown.transport muss 'windows-native' oder 'samba-rpc' sein"
            )
        username = None
        password = None
        credential_file = None
        if shutdown_transport == "windows-native":
            raw_username = shutdown_raw.get("username")
            raw_password = shutdown_raw.get("password")
            if (raw_username is None) != (raw_password is None):
                raise ConfigError(
                    f"{item_path}.shutdown.username und password muessen gemeinsam gesetzt werden"
                )
            if raw_username is not None:
                username = require_string(raw_username, f"{item_path}.shutdown.username")
                password = require_secret(raw_password, f"{item_path}.shutdown.password")
        if shutdown_transport == "samba-rpc":
            credential_file = resolve_config_path(
                config_directory,
                shutdown_raw.get("credentialFile"),
                f"{item_path}.shutdown.credentialFile",
            )
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{item_path}.enabled muss true oder false sein")

        devices.append(Device(
            id=device_id,
            mac=mac,
            host=require_string(item.get("host"), f"{item_path}.host"),
            broadcast=require_string(
                item.get("broadcast", defaults.get("broadcast")), f"{item_path}.broadcast"
            ),
            wol_port=require_int(
                item.get("wolPort", defaults.get("wolPort", 9)), f"{item_path}.wolPort", 1, 65535
            ),
            enabled=enabled,
            health_check=HealthCheck(
                port=require_int(health_raw.get("port"), f"{item_path}.healthCheck.port", 1, 65535),
                connect_timeout_seconds=require_number(
                    health_raw.get("connectTimeoutSeconds", 2),
                    f"{item_path}.healthCheck.connectTimeoutSeconds",
                    0.1,
                    60,
                ),
            ),
            shutdown=ShutdownSettings(
                transport=shutdown_transport,
                executable=require_string(
                    shutdown_raw.get("executable"), f"{item_path}.shutdown.executable"
                ),
                username=username,
                password=password,
                credential_file=credential_file,
                command_timeout_seconds=require_int(
                    shutdown_raw.get("commandTimeoutSeconds", 30),
                    f"{item_path}.shutdown.commandTimeoutSeconds",
                    1,
                    300,
                ),
            ),
        ))

    return Configuration(settings=settings, devices=tuple(devices))


def select_devices(configuration: Configuration, requested_ids: list[str]) -> tuple[Device, ...]:
    enabled = {device.id: device for device in configuration.devices if device.enabled}
    if not requested_ids:
        selected = tuple(enabled.values())
        if not selected:
            raise SelectionError("Keine aktivierten Geraete vorhanden")
        return selected

    duplicates = sorted({item for item in requested_ids if requested_ids.count(item) > 1})
    if duplicates:
        raise SelectionError(f"Device-IDs mehrfach angegeben: {', '.join(duplicates)}")
    unknown = sorted(set(requested_ids) - set(enabled))
    if unknown:
        raise SelectionError(f"Unbekannte oder deaktivierte Device-IDs: {', '.join(unknown)}")
    return tuple(enabled[item] for item in requested_ids)


def magic_packet(mac: str) -> bytes:
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + mac_bytes * 16


async def send_wol(device: Device, settings: Settings) -> None:
    packet = magic_packet(device.mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for packet_index in range(settings.wol_packets):
            sock.sendto(packet, (device.broadcast, device.wol_port))
            if packet_index + 1 < settings.wol_packets:
                await asyncio.sleep(settings.wol_packet_interval_seconds)
    finally:
        sock.close()


async def is_online(device: Device) -> bool:
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(device.host, device.health_check.port),
            timeout=device.health_check.connect_timeout_seconds,
        )
        return True
    except (TimeoutError, OSError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def wait_for_state(device: Device, expected_online: bool, timeout_seconds: float, settings: Settings) -> bool:
    deadline = time.monotonic() + timeout_seconds
    consecutive = 0
    while True:
        online = await is_online(device)
        consecutive = consecutive + 1 if online is expected_online else 0
        if consecutive >= settings.required_consecutive_checks:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(settings.poll_interval_seconds)


def build_shutdown_command(device: Device) -> list[str]:
    shutdown = device.shutdown
    if shutdown.transport == "windows-native":
        return [
            shutdown.executable,
            "/m", f"\\\\{device.host}",
            "/s",
            "/t", "0",
            "/f",
        ]
    if shutdown.credential_file is None:
        raise ConfigError("samba-rpc benoetigt eine credentialFile")
    return [
        shutdown.executable,
        "rpc", "shutdown",
        "-I", device.host,
        "-A", str(shutdown.credential_file),
        "-f",
        "-t", "0",
    ]


def build_windows_ipc_command(device: Device) -> list[str] | None:
    shutdown = device.shutdown
    if shutdown.transport != "windows-native" or shutdown.username is None or shutdown.password is None:
        return None
    return [
        "net.exe",
        "use",
        f"\\\\{device.host}\\IPC$",
        shutdown.password,
        f"/user:{shutdown.username}",
        "/persistent:no",
    ]


async def run_process(arguments: list[str], timeout_seconds: int) -> tuple[int | None, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return None, str(error)

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return None, "Remote-Shutdown-Prozess hat das Zeitlimit ueberschritten"

    output = b"\n".join(part for part in (stdout, stderr) if part)
    return process.returncode, output.decode("utf-8", errors="replace").strip()[-500:]


async def run_remote_shutdown(device: Device) -> tuple[int | None, str, bool]:
    ipc_command = build_windows_ipc_command(device)
    if ipc_command is not None:
        ipc_target = f"\\\\{device.host}\\IPC$"
        await run_process(
            ["net.exe", "use", ipc_target, "/delete", "/y"],
            device.shutdown.command_timeout_seconds,
        )
        connection_exit_code, connection_error = await run_process(
            ipc_command,
            device.shutdown.command_timeout_seconds,
        )
        if connection_exit_code != 0:
            return (
                connection_exit_code,
                f"IPC-Authentifizierung fehlgeschlagen: {connection_error or 'keine Diagnose'}",
                False,
            )

    exit_code, error = await run_process(
        build_shutdown_command(device),
        device.shutdown.command_timeout_seconds,
    )
    return exit_code, error, True


def device_result(device: Device, ok: bool, state: str, reason: str | None, started: float, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": device.id,
        "mac": device.mac,
        "host": device.host,
        "ok": ok,
        "state": state,
        "durationMs": round((time.monotonic() - started) * 1000),
    }
    if reason:
        result["reason"] = reason
    result.update(extra)
    return result


async def wake_device(device: Device, settings: Settings) -> dict[str, Any]:
    started = time.monotonic()
    if await is_online(device):
        return device_result(device, True, "already_online", None, started)
    try:
        await send_wol(device, settings)
    except OSError as error:
        return device_result(device, False, "wol_send_failed", str(error), started)
    if await wait_for_state(device, True, settings.wake_timeout_seconds, settings):
        return device_result(device, True, "online", None, started)
    return device_result(
        device,
        False,
        "wake_verification_timeout",
        f"Magic Packet gesendet, aber TCP-Port {device.health_check.port} wurde nicht erreichbar",
        started,
    )


async def shutdown_device(device: Device, settings: Settings) -> dict[str, Any]:
    started = time.monotonic()
    if not await is_online(device):
        return device_result(device, True, "already_offline", None, started)

    command_exit_code, command_error, shutdown_attempted = await run_remote_shutdown(device)
    if not shutdown_attempted:
        return device_result(
            device,
            False,
            "shutdown_authentication_failed",
            command_error,
            started,
            shutdownTransport=device.shutdown.transport,
            shutdownCommandExitCode=command_exit_code,
        )
    if await wait_for_state(device, False, settings.shutdown_timeout_seconds, settings):
        return device_result(
            device,
            True,
            "offline",
            None,
            started,
            shutdownTransport=device.shutdown.transport,
            shutdownCommandExitCode=command_exit_code,
        )
    reason = f"Geraet blieb erreichbar; Remote-Shutdown-Exit-Code: {command_exit_code}"
    if command_error:
        reason += f"; Remote-Shutdown: {command_error}"
    return device_result(
        device,
        False,
        "shutdown_verification_timeout",
        reason,
        started,
        shutdownTransport=device.shutdown.transport,
        shutdownCommandExitCode=command_exit_code,
    )


async def status_device(device: Device) -> dict[str, Any]:
    started = time.monotonic()
    online = await is_online(device)
    return device_result(
        device,
        online,
        "online" if online else "offline",
        None if online else f"TCP-Port {device.health_check.port} nicht erreichbar",
        started,
    )


async def run_action(action: str, devices: tuple[Device, ...], settings: Settings) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def execute(device: Device) -> dict[str, Any]:
        async with semaphore:
            if action == "wake":
                return await wake_device(device, settings)
            if action == "shutdown":
                return await shutdown_device(device, settings)
            return await status_device(device)

    return list(await asyncio.gather(*(execute(device) for device in devices)))


def response(action: str, results: list[dict[str, Any]], started_at: str, started: float) -> tuple[dict[str, Any], int]:
    failed = [item for item in results if not item["ok"]]
    exit_code = EXIT_OK if not failed else EXIT_DEVICE_FAILURE
    payload = {
        "schemaVersion": 1,
        "action": action,
        "ok": not failed,
        "exitCode": exit_code,
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "durationMs": round((time.monotonic() - started) * 1000),
        "summary": {
            "total": len(results),
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
        },
        "failedDeviceIds": [item["id"] for item in failed],
        "failedMacs": [item["mac"] for item in failed],
        "results": results,
    }
    return payload, exit_code


def error_response(action: str | None, exit_code: int, error_type: str, message: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "action": action,
        "ok": False,
        "exitCode": exit_code,
        "error": {"type": error_type, "message": message},
        "failedDeviceIds": [],
        "failedMacs": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GEEKOM Power Controller")
    parser.add_argument("--config", required=True, type=Path, help="Pfad zur JSON-Konfiguration")
    parser.add_argument("--pretty", action="store_true", help="JSON eingerueckt ausgeben")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("validate", help="Konfiguration validieren")
    for name in ("wake", "status", "shutdown"):
        command = subparsers.add_parser(name)
        command.add_argument("targets", nargs="*", help="Device-IDs; ohne Angabe alle aktivierten Geraete")
    return parser.parse_args()


def emit(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, separators=None if pretty else (",", ":")))


def main() -> int:
    args = parse_args()
    try:
        configuration = load_config(args.config)
        if args.action == "validate":
            emit({
                "schemaVersion": 1,
                "action": "validate",
                "ok": True,
                "exitCode": EXIT_OK,
                "deviceCount": len(configuration.devices),
                "enabledDeviceCount": sum(device.enabled for device in configuration.devices),
            }, args.pretty)
            return EXIT_OK

        devices = select_devices(configuration, args.targets)
        started = time.monotonic()
        started_at = utc_now()
        results = asyncio.run(run_action(args.action, devices, configuration.settings))
        payload, exit_code = response(args.action, results, started_at, started)
        emit(payload, args.pretty)
        return exit_code
    except ConfigError as error:
        emit(error_response(args.action, EXIT_CONFIG_ERROR, "config_error", str(error)), args.pretty)
        return EXIT_CONFIG_ERROR
    except SelectionError as error:
        emit(error_response(args.action, EXIT_SELECTION_ERROR, "selection_error", str(error)), args.pretty)
        return EXIT_SELECTION_ERROR
    except Exception as error:
        emit(error_response(args.action, EXIT_INTERNAL_ERROR, "internal_error", str(error)), args.pretty)
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
