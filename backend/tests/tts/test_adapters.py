from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from talktohermes.provider_resilience import EndpointCircuitBreaker
from talktohermes.tts.omnivoice import OMNIVOICE_SPEECH_URL, MAX_RESPONSE_BYTES, OmniVoiceTTS
from talktohermes.tts.base import TTSProviderUnavailable, TTSTechnicalError
from talktohermes.tts.worker import HermesWorkerTextNormalizer, PiperWorkerTTS


def wav_bytes(frames: int = 16) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * frames)
    return stream.getvalue()


def private_file(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    output = tmp_path / "speech.wav"
    output.touch(mode=0o600)
    output.chmod(0o600)
    return output


@pytest.mark.asyncio
async def test_omnivoice_uses_exact_authenticated_json_contract_and_streams_wav(tmp_path: Path) -> None:
    token = "A" * 48
    output = private_file(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OMNIVOICE_SPEECH_URL
        assert request.method == "POST"
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.headers["Accept"] == "audio/wav"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "model": "omnivoice",
            "voice": "voice-02",
            "input": "Hallo",
            "response_format": "wav",
        }
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav_bytes())

    now = [0.0]
    breaker = EndpointCircuitBreaker(cooldown_seconds=5, clock=lambda: now[0])
    failed = await breaker.acquire()
    await failed.unavailable()
    now[0] = 5
    adapter = OmniVoiceTTS(
        token, voice="voice-02", transport=httpx.MockTransport(handler), timeout=1,
        circuit_breaker=breaker,
    )
    try:
        synthesized = await adapter.synthesize("Hallo", output)
        assert isinstance(synthesized, Path)
        assert synthesized == output
        assert synthesized.read_bytes() == wav_bytes()
        assert synthesized.circuit_state == "half_open"
    finally:
        await adapter.aclose()
    assert output.read_bytes() == wav_bytes()


def test_omnivoice_owns_proxy_isolated_client_and_validates_token_target_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    ssl_context = object()

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("talktohermes.tts.omnivoice.httpx.AsyncClient", Client)
    monkeypatch.setattr(
        "talktohermes.tts.omnivoice.ssl.create_default_context",
        lambda *, cafile: ssl_context
        if cafile == "/etc/ssl/certs/ca-certificates.crt"
        else None,
    )
    adapter = OmniVoiceTTS("a" * 48, voice="voice-02")
    assert captured == {
        "trust_env": False,
        "verify": ssl_context,
    }
    assert adapter.name == "omnivoice" and adapter.voice == "voice-02"
    assert OMNIVOICE_SPEECH_URL.startswith("https://")

    with pytest.raises(ValueError, match="unsupported OmniVoice voice"):
        OmniVoiceTTS("a" * 48, voice="private-path")

    for token in ("short", "a" * 31, "a" * 257, "a" * 47 + "!"):
        with pytest.raises(ValueError, match="token"):
            OmniVoiceTTS(token, transport=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout"):
        OmniVoiceTTS("a" * 48, transport=object(), timeout=0)  # type: ignore[arg-type]
    for url in (
        "http://primary-voice-server.home.arpa:9443/v1/audio/speech",
        "https://primary-voice-server.home.arpa:9443/other",
        "https://user@primary-voice-server.home.arpa:9443/v1/audio/speech",
    ):
        with pytest.raises(ValueError, match="URL"):
            OmniVoiceTTS("a" * 48, speech_url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500, text="secret server text"), "omnivoice_http_failed"),
        (httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"{}"), "omnivoice_invalid_content_type"),
        (httpx.Response(200, headers={"Content-Type": "audio/wav", "Content-Length": str(MAX_RESPONSE_BYTES + 1)}), "omnivoice_response_too_large"),
        (httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"x" * (MAX_RESPONSE_BYTES + 1)), "omnivoice_response_too_large"),
    ],
)
async def test_omnivoice_rejects_http_content_type_and_response_bounds_without_leaks(
    tmp_path: Path, response: httpx.Response, code: str
) -> None:
    secret = "s" * 48
    text = "private spoken content"

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    adapter = OmniVoiceTTS(secret, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TTSTechnicalError, match=code) as caught:
            await adapter.synthesize(text, private_file(tmp_path))
    finally:
        await adapter.aclose()
    assert secret not in str(caught.value)
    assert text not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ConnectTimeout, httpx.ConnectError])
async def test_omnivoice_connect_failures_open_circuit_and_skip_transport(
    tmp_path: Path, failure_type: type[httpx.HTTPError]
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure_type("private details", request=request)

    adapter = OmniVoiceTTS(
        "a" * 48, transport=httpx.MockTransport(handler),
        connect_timeout_seconds=0.5, response_timeout_seconds=120,
    )
    try:
        assert adapter._timeout == httpx.Timeout(connect=0.5, read=120, write=120, pool=0.5)
        with pytest.raises(TTSProviderUnavailable, match="omnivoice_provider_unavailable"):
            await adapter.synthesize("Hallo", private_file(tmp_path))
        with pytest.raises(TTSProviderUnavailable) as skipped:
            await adapter.synthesize("Hallo", private_file(tmp_path))
    finally:
        await adapter.aclose()
    assert skipped.value.circuit_state == "open"
    assert calls == 1


@pytest.mark.asyncio
async def test_omnivoice_read_timeout_remains_retriable_without_opening_circuit(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private", request=request)

    adapter = OmniVoiceTTS("a" * 48, transport=httpx.MockTransport(handler))
    try:
        for _ in range(2):
            with pytest.raises(asyncio.TimeoutError):
                await adapter.synthesize("Hallo", private_file(tmp_path))
    finally:
        await adapter.aclose()
    assert calls == 2


class FakeStdin:
    def __init__(self, on_write: Any = None) -> None:
        self.on_write = on_write

    def write(self, data: bytes) -> None:
        if self.on_write:
            self.on_write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStdout:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class FakeProcess:
    def __init__(self, stdout: bytes = b"", *, returncode: int | None = 0, on_write: Any = None) -> None:
        self.returncode = returncode
        self.pid = 4242
        self.stdin = FakeStdin(on_write)
        self.stdout = FakeStdout(stdout)
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def worker_paths(tmp_path: Path, *, real_python: bool = False) -> tuple[Path, Path, Path]:
    script = tmp_path / "worker.py"
    root = tmp_path / "hermes"
    script.write_bytes(b"worker")
    script.chmod(0o600)
    root.mkdir(mode=0o700)
    venv = root / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True, mode=0o700)
    python = bin_dir / "talktohermes-python"
    base_dir = tmp_path / "base-python" / "bin"
    base_dir.mkdir(parents=True, mode=0o700)
    base_python = base_dir / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if real_python:
        shutil.copy2(Path(sys.executable).resolve(), base_python)
    else:
        base_python.write_bytes(b"python")
    base_python.chmod(0o700)
    shutil.copy2(base_python, python)
    (bin_dir / "python").symlink_to(base_python)
    (venv / "pyvenv.cfg").write_text(
        f"home = {base_dir}\nversion = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    (venv / "pyvenv.cfg").chmod(0o600)
    venv.chmod(0o700)
    return python, script, root


def test_regular_worker_interpreter_preserves_virtualenv_semantics(tmp_path: Path) -> None:
    python, _, root = worker_paths(tmp_path, real_python=True)
    completed = subprocess.run(
        [str(python), "-I", "-c", "import sys;print(sys.prefix);print(sys.base_prefix)"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    prefix, base_prefix = completed.stdout.splitlines()
    assert prefix == str(root / "venv")
    assert prefix != base_prefix


def test_worker_accepts_uv_version_info_pyvenv_metadata(tmp_path: Path) -> None:
    python, script, root = worker_paths(tmp_path)
    cfg = root / "venv" / "pyvenv.cfg"
    cfg.write_text(
        f"home = {(tmp_path / 'base-python' / 'bin')}\n"
        f"version_info = {sys.version_info.major}.{sys.version_info.minor}\n",
        encoding="utf-8",
    )
    cfg.chmod(0o600)

    adapter = PiperWorkerTTS(python, script, root, "de_DE-thorsten-high")
    assert adapter.voice == "de_DE-thorsten-high"


@pytest.mark.asyncio
async def test_worker_uses_fixed_exec_and_exact_bounded_piper_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    output = private_file(tmp_path)
    captured: dict[str, Any] = {}
    signals: list[tuple[int, signal.Signals]] = []

    def on_write(raw: bytes) -> None:
        captured["request"] = json.loads(raw)
        output.write_bytes(wav_bytes())

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess(
            json.dumps({"ok": True, "provider": "piper", "voice": "de_DE-thorsten-high", "file_path": str(output)}).encode(),
            on_write=on_write,
        )

    monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "talktohermes.tts.worker.os.killpg",
        lambda group, sig: signals.append((group, sig)),
    )
    adapter = PiperWorkerTTS(python, script, root, "de_DE-thorsten-high", timeout=1)
    assert await adapter.synthesize("Hallo", output) == output
    assert captured["argv"][0:3] == (str(python), "-I", "-c")
    assert captured["argv"][3] == "import runpy,sys;sys.path.insert(0,sys.argv[1]);script=sys.argv[2];sys.argv=[script];runpy.run_path(script,run_name='__main__')"
    assert captured["argv"][4] == str(root.resolve())
    assert captured["argv"][5].startswith("/proc/self/fd/")
    assert captured["request"] == {
        "operation": "tts", "provider": "piper", "voice": "de_DE-thorsten-high",
        "text": "Hallo", "output_path": str(output),
    }
    worker_env = captured["kwargs"].pop("env")
    pass_fds = captured["kwargs"].pop("pass_fds")
    executable = captured["kwargs"].pop("executable")
    assert len(pass_fds) == 2
    assert executable == f"/proc/self/fd/{pass_fds[0]}"
    assert captured["argv"][5] == f"/proc/self/fd/{pass_fds[1]}"
    assert captured["kwargs"] == {
        "cwd": str(root.resolve()), "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.DEVNULL,
        "start_new_session": True,
    }
    assert worker_env["HERMES_SESSION_PLATFORM"] == ""
    assert worker_env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in worker_env
    assert "VIRTUAL_ENV" not in worker_env
    assert not any("PROXY" in key or "TOKEN" in key or "KEY" in key for key in worker_env)
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_worker_normalizer_is_callable_through_the_same_bounded_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    captured: dict[str, Any] = {}

    def on_write(raw: bytes) -> None:
        captured["request"] = json.loads(raw)

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess(b'{"ok":true,"text":"Hallo Welt."}', on_write=on_write)

    monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
    monkeypatch.setattr("talktohermes.tts.worker.os.killpg", lambda *_args: None)

    normalize = HermesWorkerTextNormalizer(python, script, root, timeout=1)
    assert await normalize("**Hallo** Welt") == "Hallo Welt."
    assert captured["request"] == {"operation": "normalize", "text": "**Hallo** Welt"}
    assert captured["kwargs"]["executable"].startswith("/proc/self/fd/")
    assert len(captured["kwargs"]["pass_fds"]) == 2


@pytest.mark.asyncio
async def test_worker_timeout_or_cancellation_kills_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    for failure in (asyncio.TimeoutError(), asyncio.CancelledError()):
        process = FakeProcess(returncode=None)
        signals: list[tuple[int, signal.Signals]] = []
        output = private_file(tmp_path)
        staging = output.with_name(f".tts-worker-{output.name}")

        async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
            return process

        async def broken_exchange(*args: Any, **kwargs: Any) -> bytes:
            staging.write_bytes(b"partial private audio")
            staging.chmod(0o600)
            raise failure

        monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
        monkeypatch.setattr("talktohermes.tts.worker._bounded_exchange", broken_exchange)
        monkeypatch.setattr(
            "talktohermes.tts.worker.os.killpg",
            lambda group, sig: signals.append((group, sig)),
        )
        with pytest.raises(type(failure)):
            await PiperWorkerTTS(python, script, root, "de_DE-ramona-low").synthesize(
                "Hallo", output
            )
        assert process.waited
        assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
        assert not staging.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process-group regression")
async def test_real_worker_timeout_kills_descendant_and_removes_staging(tmp_path: Path) -> None:
    python, _, root = worker_paths(tmp_path, real_python=True)
    script = tmp_path / "slow-worker.py"
    script.write_text(
        "import json,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        "output=Path(request['output_path'])\n"
        "staging=output.with_name('.tts-worker-'+output.name)\n"
        "staging.write_bytes(b'partial private audio')\n"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "output.with_suffix('.childpid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o600)
    output = private_file(tmp_path)
    pid_path = output.with_suffix(".childpid")
    adapter = PiperWorkerTTS(
        python, script, root, "de_DE-thorsten-high", timeout=0.2
    )
    with pytest.raises(asyncio.TimeoutError):
        await adapter.synthesize("Hallo", output)

    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    pid_path.unlink()
    staging = output.with_name(f".tts-worker-{output.name}")
    assert not staging.exists()
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
        pytest.fail("Piper worker descendant survived timeout cleanup")


def test_worker_rejects_voice_not_on_exact_allowlist_and_unsafe_paths(tmp_path: Path) -> None:
    python, script, root = worker_paths(tmp_path)
    assert PiperWorkerTTS(python, script, root, "en_US-lessac-medium").voice == (
        "en_US-lessac-medium"
    )
    for voice in ("Thorsten-high", "en-US-amy-low", "de_DE-thorsten-high;sh"):
        with pytest.raises(ValueError, match="voice"):
            PiperWorkerTTS(python, script, root, voice)
    python_link = tmp_path / "python-link"
    python_link.symlink_to(python)
    with pytest.raises(ValueError, match="python"):
        PiperWorkerTTS(python_link, script, root, "de_DE-thorsten-high")
    external_python = tmp_path / "external-python"
    external_python.write_bytes(b"python")
    external_python.chmod(0o700)
    with pytest.raises(ValueError, match="python"):
        PiperWorkerTTS(external_python, script, root, "de_DE-thorsten-high")
    wrong_name = python.with_name("arbitrary-shell")
    wrong_name.write_bytes(python.read_bytes())
    wrong_name.chmod(0o700)
    with pytest.raises(ValueError, match="python"):
        PiperWorkerTTS(wrong_name, script, root, "de_DE-thorsten-high")
    script_link = tmp_path / "script-link"
    script_link.symlink_to(script)
    with pytest.raises(ValueError, match="script"):
        PiperWorkerTTS(python, script_link, root, "de_DE-thorsten-high")
    with pytest.raises(ValueError, match="script"):
        PiperWorkerTTS(python, Path("worker.py"), root, "de_DE-thorsten-high")

    for path, mode, label in (
        (python, 0o722, "python"),
        (script, 0o622, "script"),
        (root, 0o722, "hermes_root"),
    ):
        path.chmod(mode)
        with pytest.raises(ValueError, match=label):
            PiperWorkerTTS(python, script, root, "de_DE-thorsten-high")
        path.chmod(0o700 if path != script else 0o600)

    (root / "venv" / "pyvenv.cfg").write_text("malformed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pyvenv"):
        PiperWorkerTTS(python, script, root, "de_DE-thorsten-high")


def test_worker_rejects_group_writable_script_parent(tmp_path: Path) -> None:
    python, script, root = worker_paths(tmp_path)
    writable = tmp_path / "writable-scripts"
    writable.mkdir(mode=0o770)
    moved_script = writable / script.name
    script.replace(moved_script)
    with pytest.raises(ValueError, match="script"):
        PiperWorkerTTS(python, moved_script, root, "de_DE-thorsten-high")


@pytest.mark.asyncio
async def test_worker_fails_closed_if_validated_interpreter_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    adapter = PiperWorkerTTS(python, script, root, "de_DE-thorsten-high")
    replacement = python.with_suffix(".new")
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o700)
    os.replace(replacement, python)
    called = False

    async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
        nonlocal called
        called = True
        return FakeProcess()

    monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
    with pytest.raises(TTSTechnicalError, match="worker_path_changed"):
        await adapter.synthesize("Hallo", private_file(tmp_path))
    assert not called


@pytest.mark.asyncio
async def test_worker_rejects_mismatched_output_path_and_oversized_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    output = private_file(tmp_path)
    responses = [
        json.dumps({"ok": True, "provider": "piper", "voice": "de_DE-thorsten-high", "file_path": "/tmp/wrong.wav"}).encode(),
        b"x" * 70_000,
    ]
    for response in responses:
        process = FakeProcess(response, returncode=None)

        async def create_process(*argv: str, **kwargs: Any) -> FakeProcess:
            return process

        monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
        with pytest.raises(TTSTechnicalError):
            await PiperWorkerTTS(python, script, root, "de_DE-thorsten-high").synthesize(
                "Hallo", output
            )
        assert process.waited


@pytest.mark.asyncio
async def test_worker_cleans_process_group_after_completed_exchange_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, script, root = worker_paths(tmp_path)
    process = FakeProcess(b"not-json", returncode=None)
    signals: list[tuple[int, signal.Signals]] = []

    async def create_process(*_argv: str, **_kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr("talktohermes.tts.worker.asyncio.create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "talktohermes.tts.worker.os.killpg",
        lambda group, sig: signals.append((group, sig)),
    )
    with pytest.raises(TTSTechnicalError, match="worker_invalid_response"):
        await PiperWorkerTTS(python, script, root, "de_DE-thorsten-high").synthesize(
            "Hallo", private_file(tmp_path)
        )
    assert process.waited
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


def test_package_exports_chain_and_adapters() -> None:
    from talktohermes.tts import OmniVoiceTTS as ExportedOmniVoice
    from talktohermes.tts import PiperWorkerTTS as ExportedPiper
    from talktohermes.tts import TTSChain
    from talktohermes.tts import HermesWorkerTextNormalizer as ExportedNormalizer

    assert ExportedOmniVoice is OmniVoiceTTS
    assert ExportedPiper is PiperWorkerTTS
    assert TTSChain.__name__ == "TTSChain"
    assert ExportedNormalizer is HermesWorkerTextNormalizer
