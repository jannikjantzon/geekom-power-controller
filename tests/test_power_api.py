import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIRECTORY = ROOT / "api"
sys.path.insert(0, str(API_DIRECTORY))

import power_api  # noqa: E402


def api_config(directory: Path, *, password: str = "valid-kiosk-password") -> Path:
    config = {
        "version": 1,
        "api": {
            "bindHost": "127.0.0.1",
            "port": 8787,
            "bearerToken": "a" * 32,
            "allowedClients": ["127.0.0.1/32"],
            "commandTimeoutSeconds": 30,
            "threads": 2,
            "logFile": str(directory / "api-test.log"),
        },
        "defaults": {
            "broadcast": "192.168.100.255",
            "healthCheck": {"port": 445},
            "shutdown": {
                "transport": "windows-native",
                "executable": "shutdown.exe",
                "commandTimeoutSeconds": 30,
            },
        },
        "devices": [
            {
                "id": "screen3",
                "mac": "00:11:22:33:44:55",
                "host": "192.168.100.53",
                "shutdown": {
                    "username": "KIOSK-03\\kiosk-power",
                    "password": password,
                },
            }
        ],
    }
    path = directory / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class PowerApiTest(unittest.TestCase):
    def test_valid_config_and_authenticated_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = api_config(Path(temporary_directory))

            def runner(settings, action, device_id):
                self.assertEqual("status", action)
                self.assertEqual("screen3", device_id)
                return {"ok": True, "exitCode": 0}, 0

            app = power_api.create_app(config_path, controller_runner=runner)
            client = app.test_client()
            response = client.post(
                "/api/v1/status?id=screen3",
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )

            self.assertEqual(200, response.status_code)
            self.assertTrue(response.get_json()["ok"])

    def test_rejects_password_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = api_config(Path(temporary_directory), password="PASSWORD_EINTRAGEN")
            with self.assertRaises(power_api.ApiConfigError):
                power_api.load_settings(config_path)

    def test_rejects_unknown_device_before_controller_execution(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = api_config(Path(temporary_directory))

            def runner(settings, action, device_id):
                raise AssertionError("Controller darf fuer unbekannte IDs nicht gestartet werden")

            app = power_api.create_app(config_path, controller_runner=runner)
            client = app.test_client()
            response = client.post(
                "/api/v1/shutdown?id=not-configured",
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )

            self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
