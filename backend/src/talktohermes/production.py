from __future__ import annotations

import argparse
import asyncio
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import uvicorn

from .app import create_app
from .provider_resilience import EndpointCircuitBreaker
from .settings import (
    LocalPiperSettings,
    LocalSTTSettings,
    OmniVoiceSettings,
    OpenAISTTSettings,
    Settings,
    WyomingPiperSettings,
    WyomingSTTSettings,
    load_settings,
)
from .stt import OpenAICompatibleSTT, LocalSTT, STTChain, WyomingSTT
from .stt.wyoming import WYOMING_STT_PYTHON, WYOMING_STT_SCRIPT
from .tts import (
    OmniVoiceTTS,
    BoundedLanguageVerifier,
    DeterministicTextPreparer,
    HermesWorkerTextNormalizer,
    PiperWorkerTTS,
    QualityOrchestrator,
    QualityThresholds,
    WyomingPiperTTS,
)

WYOMING_BASE_INTERPRETER = Path("/usr/bin/python3.13")


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProductionVoiceStack:
    stt: STTChain
    tts: QualityOrchestrator
    closeables: tuple[AsyncCloseable, ...]


def _validate_wyoming_stt_paths() -> tuple[Path, Path]:
    for label, raw_path, executable in (
        ("interpreter", WYOMING_STT_PYTHON, True),
        ("adapter", WYOMING_STT_SCRIPT, False),
    ):
        path = Path(raw_path)
        try:
            info = path.lstat()
            parent_info = path.parent.lstat()
            resolved = path.resolve(strict=True)
            resolved_info = resolved.lstat()
        except OSError as exc:
            raise ValueError(f"invalid fallback voice server {label} path") from exc
        symlink_allowed = label == "interpreter" and path.is_symlink()
        if (
            not path.is_absolute()
            or (path.is_symlink() and not symlink_allowed)
            or (not path.is_symlink() and not stat.S_ISREG(info.st_mode))
            or not stat.S_ISREG(resolved_info.st_mode)
            or info.st_uid != 0
            or resolved_info.st_uid != 0
            or parent_info.st_uid != 0
            or stat.S_IMODE(parent_info.st_mode) & 0o022
            or stat.S_IMODE(resolved_info.st_mode) & 0o022
            or (executable and not resolved_info.st_mode & stat.S_IXUSR)
            or (label == "interpreter" and resolved != WYOMING_BASE_INTERPRETER)
        ):
            raise ValueError(f"invalid fallback voice server {label} path")
    return Path(WYOMING_STT_PYTHON), Path(WYOMING_STT_SCRIPT)


def build_voice_stack(settings: Settings) -> ProductionVoiceStack:
    """Construct the production topology without making provider calls."""
    worker = settings.voice_worker
    normalizer = HermesWorkerTextNormalizer(worker.python, worker.script, worker.hermes_root)
    stt_providers = []
    tts_providers = []
    closeables: list[AsyncCloseable] = []
    for provider in settings.stt:
        if isinstance(provider, OpenAISTTSettings):
            breaker = EndpointCircuitBreaker(
                cooldown_seconds=provider.circuit_cooldown_seconds
            )
            built = OpenAICompatibleSTT(
                provider.url,
                provider.token.get_secret_value(),
                model=provider.model,
                connect_timeout_seconds=provider.connect_timeout_seconds,
                response_timeout_seconds=provider.response_timeout_seconds,
                circuit_breaker=breaker,
            )
            closeables.append(built)
        elif isinstance(provider, WyomingSTTSettings):
            wyoming_python, wyoming_script = _validate_wyoming_stt_paths()
            built = WyomingSTT(
                uri=provider.url,
                python_path=wyoming_python,
                script_path=wyoming_script,
                path_validator=_validate_wyoming_stt_paths,
            )
        elif isinstance(provider, LocalSTTSettings):
            built = LocalSTT(
                worker.python, worker.script, worker.hermes_root, model=provider.model
            )
        else:  # pragma: no cover - discriminated settings make this unreachable
            raise ValueError("unsupported STT provider")
        stt_providers.append(built)

    for provider in settings.tts:
        if isinstance(provider, OmniVoiceSettings):
            breaker = EndpointCircuitBreaker(
                cooldown_seconds=provider.circuit_cooldown_seconds
            )
            built_tts = OmniVoiceTTS(
                provider.token.get_secret_value(),
                voice=provider.voice,
                speech_url=provider.url,
                connect_timeout_seconds=provider.connect_timeout_seconds,
                response_timeout_seconds=provider.response_timeout_seconds,
                circuit_breaker=breaker,
            )
            closeables.append(built_tts)
        elif isinstance(provider, WyomingPiperSettings):
            built_tts = WyomingPiperTTS(url=provider.url, voice=provider.voice)
        elif isinstance(provider, LocalPiperSettings):
            built_tts = PiperWorkerTTS(
                worker.python, worker.script, worker.hermes_root, provider.voice
            )
        else:  # pragma: no cover
            raise ValueError("unsupported TTS provider")
        tts_providers.append(built_tts)

    thresholds = QualityThresholds()
    verifier = BoundedLanguageVerifier(
        stt_providers[0],
        timeout_seconds=thresholds.verifier_timeout_seconds,
        max_transcript_chars=thresholds.max_transcript_chars,
    )
    return ProductionVoiceStack(
        stt=STTChain(tuple(stt_providers)),
        tts=QualityOrchestrator(
            DeterministicTextPreparer(normalizer),
            *tts_providers,
            verifier,
            thresholds=thresholds,
        ),
        closeables=tuple(closeables),
    )


def _absolute_yaml_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() not in {".yaml", ".yml"}:
        raise argparse.ArgumentTypeError("INSTANCE_YAML must be an absolute YAML path")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="talktohermes-production",
        description="Run one validated TalkToHermes instance.",
    )
    parser.add_argument("instance_yaml", type=_absolute_yaml_path, metavar="INSTANCE_YAML")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    voice_stack: ProductionVoiceStack | None = None
    try:
        settings = load_settings(args.instance_yaml)
        voice_stack = build_voice_stack(settings)
        app = create_app(
            settings,
            stt=voice_stack.stt,
            tts=voice_stack.tts,
            closeables=voice_stack.closeables,
        )
        uvicorn.run(
            app,
            host=settings.listen_host,
            port=settings.listen_port,
            workers=1,
            reload=False,
            timeout_graceful_shutdown=15,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            access_log=False,
        )
    except Exception:
        if voice_stack is not None:
            try:
                asyncio.run(_close_voice_stack(voice_stack))
            except Exception:
                pass
        # Configuration, paths, and providers may contain credentials.  The
        # production boundary intentionally reports no exception detail.
        print("invalid instance configuration", file=sys.stderr)
        return 1
    return 0


async def _close_voice_stack(stack: ProductionVoiceStack) -> None:
    for closeable in reversed(stack.closeables):
        await closeable.aclose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
