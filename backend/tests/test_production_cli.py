from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from talktohermes import production
from talktohermes.settings import SettingsError


class Closeable:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def test_main_loads_one_absolute_config_and_runs_locked_down_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Path("/etc/talktohermes/instance-a.yaml")
    settings = SimpleNamespace(listen_host="127.0.0.1", listen_port=18081)
    loaded: list[Path] = []
    sentinel_app = object()
    stack = SimpleNamespace(stt=object(), tts=object(), closeables=(object(), object()))
    calls: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setattr(production, "load_settings", lambda path: loaded.append(path) or settings)
    monkeypatch.setattr(production, "build_voice_stack", lambda value: stack if value is settings else None)
    monkeypatch.setattr(
        production,
        "create_app",
        lambda value, **kwargs: sentinel_app
        if value is settings
        and kwargs == {"stt": stack.stt, "tts": stack.tts, "closeables": stack.closeables}
        else None,
    )
    monkeypatch.setattr(production.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert production.main([str(config)]) == 0
    assert loaded == [config]
    assert calls == [
        (
            sentinel_app,
            {
                "host": "127.0.0.1",
                "port": 18081,
                "workers": 1,
                "reload": False,
                "timeout_graceful_shutdown": 15,
                "proxy_headers": True,
                "forwarded_allow_ips": "127.0.0.1",
                "access_log": False,
            },
        )
    ]


@pytest.mark.parametrize("argv", [[], ["relative.yaml"], ["/one.yaml", "/two.yaml"]])
def test_main_rejects_everything_except_exactly_one_absolute_yaml_path(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        production.main(argv)
    assert raised.value.code == 2


def test_main_fails_closed_without_printing_settings_error_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "top-secret-app-token"
    monkeypatch.setattr(
        production,
        "load_settings",
        lambda _path: (_ for _ in ()).throw(SettingsError(f"invalid {secret}")),
    )

    assert production.main(["/etc/talktohermes/instance-a.yaml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid instance configuration\n"
    assert secret not in captured.err


def test_main_fails_before_uvicorn_when_voice_stack_is_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(listen_host="127.0.0.1", listen_port=18081)
    ran: list[object] = []
    monkeypatch.setattr(production, "load_settings", lambda _path: settings)
    monkeypatch.setattr(
        production,
        "build_voice_stack",
        lambda _settings: (_ for _ in ()).throw(ValueError("private provider target")),
    )
    monkeypatch.setattr(production.uvicorn, "run", lambda app, **kwargs: ran.append(app))

    assert production.main(["/etc/talktohermes/instance-a.yaml"]) == 1
    assert ran == []
    assert capsys.readouterr().err == "invalid instance configuration\n"


def test_main_closes_constructed_voice_clients_when_app_startup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = SimpleNamespace(listen_host="127.0.0.1", listen_port=18081)
    first = Closeable()
    second = Closeable()
    stack = SimpleNamespace(stt=object(), tts=object(), closeables=(first, second))
    monkeypatch.setattr(production, "load_settings", lambda _path: settings)
    monkeypatch.setattr(production, "build_voice_stack", lambda _settings: stack)
    monkeypatch.setattr(
        production,
        "create_app",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("startup failed")),
    )

    assert production.main(["/etc/talktohermes/instance-a.yaml"]) == 1
    assert first.closed == 1
    assert second.closed == 1
    assert capsys.readouterr().err == "invalid instance configuration\n"
