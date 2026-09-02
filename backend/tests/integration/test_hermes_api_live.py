from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("TALKTOHERMES_HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
ENV_PATH = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env"
_LIVE_API_KEY = ""


def _env_secret(name: str) -> str:
    if not ENV_PATH.is_file():
        return ""
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def _request(path: str, *, authenticated: bool = False) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + _LIVE_API_KEY
    request = Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}
    except URLError as exc:
        raise AssertionError(f"Hermes API is not reachable at {BASE_URL}: {exc.reason}") from exc


@unittest.skipUnless(os.environ.get("TALKTOHERMES_LIVE") == "1", "live Hermes API test")
class HermesApiLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global _LIVE_API_KEY
        _LIVE_API_KEY = _env_secret("API_SERVER_KEY")
        if len(_LIVE_API_KEY) < 16:
            raise AssertionError("A strong API_SERVER_KEY is required for the live test")

    @classmethod
    def tearDownClass(cls) -> None:
        global _LIVE_API_KEY
        _LIVE_API_KEY = ""

    def test_unauthorized_request_is_rejected(self) -> None:
        status, _ = _request("/v1/capabilities")
        self.assertEqual(401, status)

    def test_authenticated_health_and_capabilities(self) -> None:
        health_status, health = _request("/health", authenticated=True)
        self.assertEqual(200, health_status)
        self.assertIsInstance(health, dict)

        status, capabilities = _request("/v1/capabilities", authenticated=True)
        self.assertEqual(200, status)
        self.assertIsInstance(capabilities, dict)
        self.assertTrue(capabilities)


if __name__ == "__main__":
    unittest.main()
