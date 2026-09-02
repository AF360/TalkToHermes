from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from talktohermes.provider_resilience import EndpointCircuitBreaker
from talktohermes.stt.openai import MAX_RESPONSE_BYTES, SYSTEM_CA_BUNDLE, OpenAICompatibleSTT
from talktohermes.stt.base import STTProviderUnavailable, STTTechnicalError, STTValidationError
from talktohermes.stt.local import LocalSTT
from talktohermes.stt.wyoming import (
    WYOMING_STT_PYTHON,
    WYOMING_STT_SCRIPT,
    WyomingSTT,
    _terminate_process_group,
)


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: bytes = b"",
        on_communicate: Any = None,
    ) -> None:
        self.returncode = returncode
        self.stdin = FakeStdin(on_communicate)
        self.stdout = FakeStdout(stdout)
        self._stdout_bytes = stdout
        self.on_communicate = on_communicate
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self.on_communicate is not None:
            value = self.on_communicate(input)
            if asyncio.iscoroutine(value):
                await value
        return self._stdout_bytes, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeStdin:
    def __init__(self, on_write: Any) -> None:
        self._on_write = on_write

    def write(self, request: bytes) -> None:
        if self._on_write is not None:
            self._on_write(request)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStdout:
    def __init__(self, response: bytes) -> None:
        self._response = response

    async def read(self, size: int) -> bytes:
        chunk, self._response = self._response[:size], self._response[size:]
        return chunk


def audio_file(tmp_path: Path, suffix: str = ".wav", content: bytes = b"RIFFspeech") -> Path:
    path = tmp_path / f"private-audio{suffix}"
    path.write_bytes(content)
    return path


def test_openai_default_client_uses_debian_system_ca_without_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    ssl_context = object()

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("talktohermes.stt.openai.httpx.AsyncClient", Client)
    monkeypatch.setattr(
        "talktohermes.stt.openai.ssl.create_default_context",
        lambda *, cafile: ssl_context if cafile == SYSTEM_CA_BUNDLE else None,
    )
    OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", "a" * 48)
    assert captured == {"trust_env": False, "verify": ssl_context}


@pytest.mark.asyncio
async def test_wyoming_invokes_packaged_adapter_with_fixed_argv_and_private_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = audio_file(tmp_path)
    captured: dict[str, Any] = {}

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        output = Path(argv[argv.index("--output") + 1])
        assert not output.exists()
        assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
        output.write_text(" Hallo von fallback voice server \n", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("talktohermes.stt.wyoming.asyncio.create_subprocess_exec", create_process)
    validations: list[bool] = []
    text = await WyomingSTT(
        timeout=1, path_validator=lambda: validations.append(True)
    ).transcribe(audio, "de")

    assert text == "Hallo von fallback voice server"
    assert validations == [True]
    argv = captured["argv"]
    assert argv == (
        WYOMING_STT_PYTHON,
        WYOMING_STT_SCRIPT,
        "--input",
        str(audio.resolve()),
        "--output",
        argv[5],
        "--language",
        "de",
        "--uri",
        "tcp://127.0.0.1:10300",
    )
    assert "fallback-voice-server.home.arpa" not in " ".join(argv)
    assert "192.168.100.30" not in " ".join(argv)
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["start_new_session"] is True


@pytest.mark.asyncio
async def test_wyoming_timeout_kills_process_without_exposing_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess(returncode=None)

    async def never_finishes(_: bytes | None) -> None:
        await asyncio.sleep(10)

    process.on_communicate = never_finishes

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr("talktohermes.stt.wyoming.asyncio.create_subprocess_exec", create_process)
    with pytest.raises(asyncio.TimeoutError):
        await WyomingSTT(timeout=0.001).transcribe(audio_file(tmp_path), "de")
    assert process.killed is True


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process-group regression")
async def test_wyoming_process_group_termination_does_not_leave_live_descendant() -> None:
    child_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        "time.sleep(0.2);print(p.pid,flush=True);time.sleep(60)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int((await asyncio.wait_for(process.stdout.readline(), timeout=2)).decode())

    await _terminate_process_group(process, grace_seconds=0.05)

    assert process.returncode is not None
    for _ in range(100):
        stat_path = Path(f"/proc/{child_pid}/stat")
        if not stat_path.exists():
            break
        state = stat_path.read_text(encoding="utf-8").split()[2]
        if state == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("fallback voice server descendant survived process-group termination")


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process-group regression")
async def test_wyoming_cleanup_kills_descendant_after_group_leader_exits() -> None:
    child_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "time.sleep(0.2);print(p.pid,flush=True)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int((await asyncio.wait_for(process.stdout.readline(), timeout=2)).decode())
    await process.wait()
    assert process.returncode == 0

    await _terminate_process_group(process, grace_seconds=0.01)

    for _ in range(100):
        stat_path = Path(f"/proc/{child_pid}/stat")
        try:
            state = stat_path.read_text(encoding="utf-8").split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("fallback voice server descendant survived cleanup after leader exit")


@pytest.mark.asyncio
async def test_openai_sends_authenticated_openai_multipart_contract(tmp_path: Path) -> None:
    audio = audio_file(tmp_path)
    token = "a" * 48

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions"
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.headers["Accept"] == "application/json"
        body = request.content
        assert b'name="file"; filename="audio.wav"' in body
        assert b'name="model"' in body and b"large-v3-turbo" in body
        assert b'name="language"' in body and b"de" in body
        assert b'name="response_format"' in body and b"json" in body
        assert audio.name.encode() not in body
        return httpx.Response(200, json={"text": " primary voice server text ", "time_ms": 42})

    now = [0.0]
    breaker = EndpointCircuitBreaker(cooldown_seconds=5, clock=lambda: now[0])
    failed = await breaker.acquire()
    await failed.unavailable()
    now[0] = 5
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT(
            "https://primary-voice-server.home.arpa:9444", token, client=client,
            timeout=1, circuit_breaker=breaker,
        )
        transcript = await adapter.transcribe(audio, "de")
        assert transcript == "primary voice server text"
        assert transcript.circuit_state == "half_open"


@pytest.mark.asyncio
async def test_openai_rejects_unapproved_target_weak_token_and_oversize_input(tmp_path: Path) -> None:
    for target in (
        "http://primary-voice-server.home.arpa:9444",
        "https://primary-voice-server.home.arpa:9444/other",
        "https://user@primary-voice-server.home.arpa:9444/v1/audio/transcriptions",
    ):
        with pytest.raises(ValueError, match="primary voice server target"):
            OpenAICompatibleSTT(target, "a" * 48)
    with pytest.raises(ValueError, match="token"):
        OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", "short")

    large = audio_file(tmp_path, content=b"x" * (10 * 1024 * 1024 + 1))
    adapter = OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", "a" * 48)
    with pytest.raises(STTValidationError, match="audio_too_large"):
        await adapter.transcribe(large, "de")
    await adapter.aclose()


@pytest.mark.asyncio
async def test_openai_rejects_unapproved_audio_container_without_network(tmp_path: Path) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"text": "must not happen"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", "a" * 48, client=client)
        with pytest.raises(STTValidationError, match="unsupported_audio_type"):
            await adapter.transcribe(audio_file(tmp_path, suffix=".txt"), "de")
    assert called is False


@pytest.mark.asyncio
async def test_openai_errors_are_controlled_and_do_not_echo_server_or_secret(tmp_path: Path) -> None:
    secret = "s" * 48
    leaked = "provider leaked transcript and secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=leaked)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", secret, client=client)
        with pytest.raises(STTTechnicalError) as caught:
            await adapter.transcribe(audio_file(tmp_path), "de")
    assert leaked not in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_openai_rejects_oversized_response_body(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"text":"' + b"x" * MAX_RESPONSE_BYTES + b'"}',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT("https://primary-voice-server.home.arpa:9444", "a" * 48, client=client)
        with pytest.raises(STTTechnicalError, match="openai_response_too_large"):
            await adapter.transcribe(audio_file(tmp_path), "de")


@pytest.mark.asyncio
async def test_local_worker_uses_fixed_argv_and_bounded_json_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = audio_file(tmp_path)
    python = tmp_path / "python"
    script = tmp_path / "hermes_voice_worker.py"
    root = tmp_path / "hermes-root"
    python.write_bytes(b"python")
    script.write_bytes(b"worker")
    root.mkdir()
    captured: dict[str, Any] = {}

    def on_communicate(request: bytes | None) -> None:
        assert request is not None
        captured["request"] = json.loads(request)

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess(
            stdout=b'{"ok":true,"provider":"local","text":" Lokal text "}',
            on_communicate=on_communicate,
        )

    monkeypatch.setattr("talktohermes.stt.local.asyncio.create_subprocess_exec", create_process)
    adapter = LocalSTT(python, script, root, timeout=1)
    assert await adapter.transcribe(audio, "en-US") == "Lokal text"

    assert captured["argv"] == (str(python.resolve()), str(script.resolve()))
    assert captured["request"] == {
        "operation": "stt-local", "input_path": str(audio.resolve()), "model": "small",
        "language": "en-US",
    }
    assert captured["kwargs"]["cwd"] == str(root.resolve())
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_local_worker_rejects_invalid_or_oversized_json_without_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python"
    script = tmp_path / "worker.py"
    root = tmp_path / "root"
    python.write_bytes(b"python")
    script.write_bytes(b"worker")
    root.mkdir()
    secret = "do-not-echo"

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(stdout=(b'{"ok":true,"text":"' + secret.encode() + b'x' * 70_000))

    monkeypatch.setattr("talktohermes.stt.local.asyncio.create_subprocess_exec", create_process)
    with pytest.raises(STTTechnicalError) as caught:
        await LocalSTT(python, script, root).transcribe(audio_file(tmp_path), "de")
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_local_worker_reaps_process_on_controlled_exchange_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python"
    script = tmp_path / "worker.py"
    root = tmp_path / "root"
    python.write_bytes(b"python")
    script.write_bytes(b"worker")
    root.mkdir()
    process = FakeProcess(returncode=None)

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    async def broken_exchange(*args: Any, **kwargs: Any) -> bytes:
        raise STTTechnicalError("local_process_failed")

    monkeypatch.setattr("talktohermes.stt.local.asyncio.create_subprocess_exec", create_process)
    monkeypatch.setattr("talktohermes.stt.local._bounded_exchange", broken_exchange)

    with pytest.raises(STTTechnicalError, match="local_process_failed"):
        await LocalSTT(python, script, root).transcribe(audio_file(tmp_path), "de")
    assert process.killed is True
    assert process.returncode == -9


def test_local_worker_rejects_symlinked_executable_paths(tmp_path: Path) -> None:
    real_python = tmp_path / "python-real"
    real_python.write_bytes(b"python")
    python = tmp_path / "python"
    python.symlink_to(real_python)
    script = tmp_path / "worker.py"
    script.write_bytes(b"worker")
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="python"):
        LocalSTT(python, script, root)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ConnectTimeout, httpx.ConnectError])
async def test_openai_connect_failures_open_circuit_and_skip_transport(
    tmp_path: Path, failure_type: type[httpx.HTTPError]
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure_type("private details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT(
            "https://primary-voice-server.home.arpa:9444", "a" * 48, client=client,
            connect_timeout_seconds=0.5, response_timeout_seconds=120,
        )
        assert adapter._timeout == httpx.Timeout(connect=0.5, read=120, write=120, pool=0.5)
        with pytest.raises(STTProviderUnavailable, match="openai_provider_unavailable") as first:
            await adapter.transcribe(audio_file(tmp_path), "de")
        with pytest.raises(STTProviderUnavailable) as skipped:
            await adapter.transcribe(audio_file(tmp_path), "de")
    assert first.value.circuit_state == "closed"
    assert skipped.value.circuit_state == "open"
    assert calls == 1


@pytest.mark.asyncio
async def test_openai_read_timeout_does_not_open_connectivity_circuit(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleSTT(
            "https://primary-voice-server.home.arpa:9444", "a" * 48, client=client
        )
        for _ in range(2):
            with pytest.raises(asyncio.TimeoutError):
                await adapter.transcribe(audio_file(tmp_path), "de")
    assert calls == 2


def test_package_exports_concrete_adapters() -> None:
    from talktohermes.stt import OpenAICompatibleSTT as ExportedOpenAICompatibleSTT
    from talktohermes.stt import LocalSTT as ExportedLocalSTT
    from talktohermes.stt import WyomingSTT as ExportedWyomingSTT

    assert (ExportedWyomingSTT, ExportedOpenAICompatibleSTT, ExportedLocalSTT) == (WyomingSTT, OpenAICompatibleSTT, LocalSTT)
