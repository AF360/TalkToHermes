from __future__ import annotations

from pathlib import Path
import tomllib

import yaml

from talktohermes.app import create_app
from talktohermes.settings import load_settings
from tests.test_settings import write_instance
from tests.test_text_routes import FakeHermes
from talktohermes.storage import Storage

ROOT = Path(__file__).resolve().parents[3]


def _methods(spec: dict) -> dict[str, set[str]]:
    methods = {"get", "post", "put", "patch", "delete"}
    return {
        path: {method for method in item if method in methods}
        for path, item in spec["paths"].items()
    }


def test_static_openapi_matches_development_app_surface(tmp_path: Path) -> None:
    settings = load_settings(write_instance(tmp_path))
    app = create_app(
        settings,
        storage=Storage(settings.state_dir / "contract.sqlite3"),
        hermes=FakeHermes(),
    )
    generated = app.openapi()
    static = yaml.safe_load((ROOT / "api/openapi.yaml").read_text(encoding="utf-8"))
    assert _methods(static) == _methods(generated)
    package = tomllib.loads(
        (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    )
    assert static["info"]["version"] == generated["info"]["version"]
    assert generated["info"]["version"] == package["project"]["version"]
    assert generated["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }


def test_approval_enum_is_once_and_deny_only() -> None:
    static = yaml.safe_load((ROOT / "api/openapi.yaml").read_text(encoding="utf-8"))
    assert static["components"]["schemas"]["ApprovalRequest"]["properties"]["decision"]["enum"] == [
        "once",
        "deny",
    ]


def test_text_route_is_absent_when_development_is_false(tmp_path: Path) -> None:
    config = write_instance(tmp_path)
    config.write_text(config.read_text().replace("development: true", "development: false"))
    settings = load_settings(config)
    app = create_app(
        settings,
        storage=Storage(settings.state_dir / "production.sqlite3"),
        hermes=FakeHermes(),
    )
    assert "/v1/conversations/{conversation_id}/turns/text" not in app.openapi()["paths"]
