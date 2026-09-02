from __future__ import annotations

import importlib
import random
import struct
import wave
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

FIXED_SEED = 314
FIXED_STEPS = 12
FIXED_GUIDANCE_SCALE = 1.2
SAMPLE_RATE = 24_000
MODEL_REVISION = "c5fdb5ccb189668d56333f77ba2629f4cd7535f4"


class OmniVoiceBackend:
    """Lazy adapter for the Apache-2.0 k2-fsa/OmniVoice public API."""

    def __init__(self, model_name: str = "k2-fsa/OmniVoice") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._generation_config: Any = None
        self._load_lock = Lock()

    def _load(self) -> tuple[Any, Any]:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    module = importlib.import_module("omnivoice")
                    torch = importlib.import_module("torch")
                    model_class = getattr(module, "OmniVoice")
                    config_class = getattr(module, "OmniVoiceGenerationConfig")
                    model = model_class.from_pretrained(
                        self._model_name,
                        device_map="cuda:0",
                        dtype=torch.float16,
                        revision=MODEL_REVISION,
                    )
                    generation_config = config_class.from_dict({
                        "num_step": FIXED_STEPS,
                        "guidance_scale": FIXED_GUIDANCE_SCALE,
                    })
                    # Publish only a complete pair; readiness must never observe
                    # a model whose generation configuration failed to build.
                    self._model = model
                    self._generation_config = generation_config
        return self._model, self._generation_config

    def ready(self) -> None:
        self._load()

    def synthesize(
        self,
        *,
        text: str,
        reference_audio: Path,
        reference_text: str,
        language: str,
        max_output_bytes: int,
    ) -> bytes:
        random.seed(FIXED_SEED)
        try:
            torch = importlib.import_module("torch")
            torch.manual_seed(FIXED_SEED)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(FIXED_SEED)
        except ModuleNotFoundError:
            # Model loading below provides the controlled production failure.
            pass
        model, generation_config = self._load()
        generated = model.generate(
            text=text,
            ref_audio=str(reference_audio),
            ref_text=reference_text,
            language=language,
            generation_config=generation_config,
        )
        return _as_pcm16_wav(generated, max_output_bytes=max_output_bytes)


def _as_pcm16_wav(generated: Any, *, max_output_bytes: int) -> bytes:
    sample_rate = SAMPLE_RATE
    waveform = generated
    if isinstance(generated, list):
        if len(generated) != 1:
            raise RuntimeError("invalid model output")
        waveform = generated[0]
    elif isinstance(generated, tuple) and len(generated) == 2:
        waveform, sample_rate = generated
    elif hasattr(generated, "audio"):
        waveform = generated.audio
        sample_rate = getattr(generated, "sample_rate", SAMPLE_RATE)
    if hasattr(waveform, "detach"):
        waveform = waveform.detach().cpu()
    if hasattr(waveform, "numpy"):
        waveform = waveform.numpy()
    if hasattr(waveform, "reshape"):
        waveform = waveform.reshape(-1)
    if max_output_bytes < 46:
        raise RuntimeError("invalid output limit")
    maximum_samples = (max_output_bytes - 44) // 2
    sample_count = getattr(waveform, "size", None)
    if sample_count is None:
        try:
            sample_count = len(waveform)
        except TypeError as exc:
            raise RuntimeError("invalid model output") from exc
    try:
        sample_count = int(sample_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("invalid model output") from exc
    if (
        sample_count < 1
        or sample_count > maximum_samples
        or not isinstance(sample_rate, int)
        or not 8_000 <= sample_rate <= 192_000
    ):
        raise RuntimeError("invalid model output")
    pcm = bytearray()
    written_samples = 0
    for sample in waveform:
        if written_samples >= maximum_samples:
            raise RuntimeError("invalid model output")
        value = float(sample)
        value = max(-1.0, min(1.0, value))
        pcm.extend(struct.pack("<h", int(round(value * 32767))))
        written_samples += 1
    if written_samples != sample_count:
        raise RuntimeError("invalid model output")
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()
