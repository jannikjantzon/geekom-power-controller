import json
import tempfile
import unittest
from pathlib import Path

import powerctl


class PowerControllerTest(unittest.TestCase):
    def test_normalize_mac(self):
        self.assertEqual("AA:BB:CC:DD:EE:FF", powerctl.normalize_mac("aa-bb-cc-dd-ee-ff"))
        self.assertEqual("AA:BB:CC:DD:EE:FF", powerctl.normalize_mac("aabbccddeeff"))

    def test_invalid_mac(self):
        with self.assertRaises(powerctl.ConfigError):
            powerctl.normalize_mac("invalid")

    def test_magic_packet(self):
        packet = powerctl.magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(102, len(packet))
        self.assertEqual(b"\xff" * 6, packet[:6])
        self.assertEqual(bytes.fromhex("AABBCCDDEEFF") * 16, packet[6:])

    def test_load_config_and_select(self):
        config = {
            "version": 1,
            "defaults": {
                "broadcast": "192.168.1.255",
                "healthCheck": {"port": 445},
                "shutdown": {"transport": "windows-native", "executable": "shutdown.exe"}
            },
            "devices": [
                {"id": "one", "mac": "00:11:22:33:44:55", "host": "192.168.1.10"},
                {"id": "two", "mac": "00:11:22:33:44:66", "host": "192.168.1.11", "enabled": False}
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = powerctl.load_config(path)
            selected = powerctl.select_devices(loaded, [])
            self.assertEqual(["one"], [device.id for device in selected])
            self.assertEqual("windows-native", loaded.devices[0].shutdown.transport)
            self.assertEqual(
                ["shutdown.exe", "/m", "\\\\192.168.1.10", "/s", "/t", "0", "/f"],
                powerctl.build_shutdown_command(loaded.devices[0]),
            )

    def test_samba_rpc_command_uses_credential_file(self):
        config = {
            "version": 1,
            "defaults": {
                "broadcast": "192.168.1.255",
                "healthCheck": {"port": 445},
                "shutdown": {
                    "transport": "samba-rpc",
                    "executable": "/usr/bin/net",
                    "credentialFile": "secrets/samba-auth.conf"
                }
            },
            "devices": [
                {"id": "one", "mac": "00:11:22:33:44:55", "host": "192.168.1.10"}
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = powerctl.load_config(path)
            credential_file = Path(directory) / "secrets" / "samba-auth.conf"
            self.assertEqual(
                [
                    "/usr/bin/net", "rpc", "shutdown",
                    "-I", "192.168.1.10",
                    "-A", str(credential_file),
                    "-f", "-t", "0",
                ],
                powerctl.build_shutdown_command(loaded.devices[0]),
            )

    def test_duplicate_mac_rejected(self):
        config = {
            "version": 1,
            "defaults": {
                "broadcast": "192.168.1.255",
                "healthCheck": {"port": 445},
                "shutdown": {"transport": "windows-native", "executable": "shutdown.exe"}
            },
            "devices": [
                {"id": "one", "mac": "00:11:22:33:44:55", "host": "192.168.1.10"},
                {"id": "two", "mac": "00-11-22-33-44-55", "host": "192.168.1.11"}
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(powerctl.ConfigError):
                powerctl.load_config(path)

    def test_unknown_shutdown_transport_rejected(self):
        config = {
            "version": 1,
            "defaults": {
                "broadcast": "192.168.1.255",
                "healthCheck": {"port": 445},
                "shutdown": {"transport": "custom-agent", "executable": "agent"}
            },
            "devices": [
                {"id": "one", "mac": "00:11:22:33:44:55", "host": "192.168.1.10"}
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(powerctl.ConfigError):
                powerctl.load_config(path)


if __name__ == "__main__":
    unittest.main()
