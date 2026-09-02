# TalkToHermes OmniVoice service

This independently versioned service component remains at `1.0.0`; the repository and Voice Bridge release version may advance without changing this adapter when its code and contract are unchanged.

Clean-room, dedicated OpenAI-compatible REST adapter for the Apache-2.0
[k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice) public Python API. No source
from the MIT-licensed `scorbo2/ai-playground` wrapper is copied or adapted here;
TalkToHermes does not depend on that wrapper.

## Prerequisites and OmniVoice installation order

Provision the accelerator/PyTorch runtime, locked OmniVoice environment, pinned model cache, private reference assets, and private TLS ingress before installing this service. See the central [deployment prerequisites](../../deployment/README.md#prerequisites).

1. Verify the selected accelerator and install a matching PyTorch build using the [official selector](https://pytorch.org/get-started/locally/).
2. Create a clean environment compatible with `pyproject.toml` and run `uv sync --frozen` in this directory. The committed lockfile supplies the reviewed `omnivoice` package; do not add a second unconstrained installation.
3. Pre-populate the pinned [`k2-fsa/OmniVoice`](https://huggingface.co/k2-fsa/OmniVoice) model revision and create private reference WAV/transcript pairs. Use a supported OmniVoice language code such as `de` or `en` (full names are also accepted upstream); the example uses `de`.
4. Verify a real model load and synthesis with the intended accelerator and assets before installing the unit. Install `/usr/bin/ffmpeg` when MP3 output is enabled.

## Contract

- `GET /ready` — bearer-authenticated minimal readiness.
- `POST /v1/audio/speech` — bearer-authenticated JSON with exactly `model`,
  `voice`, `input`, and `response_format`; returns binary WAV or MP3.
- `model` is `omnivoice`; `response_format` is `wav` or `mp3`.
- The configured listener must be an RFC 1918, non-loopback IPv4 address and the
  fixed dedicated port `9090`. The example is `192.168.100.20:9090`.
- Voice IDs are logical allowlisted IDs. Reference WAV paths and transcripts exist
  only in server configuration and cannot be supplied by clients.

OpenAPI and interactive documentation are disabled. Inference is serialized and a
concurrent synthesis fails fast. The seed (`314`), steps (`12`), guidance scale
(`1.2`), model, and language are server controlled. MP3 conversion uses only the
fixed `/usr/bin/ffmpeg` argument vector, stdin/stdout pipes, a 30-second timeout,
and no temporary files.

## Configuration and permissions

Copy `config.example.yaml` outside the repository and replace placeholders. The
service rejects wildcard, loopback, public, documentation-only or invalid IPs,
hostnames, port `8181`, every port other than `9090`, unknown keys, unknown/unsafe voice IDs,
non-private voice files, symlinks, wrong owners, malformed WAV files, and token
files whose mode is not exactly `0600`. Every configured asset is opened with
`O_NOFOLLOW`, checked against its inode, and read through a retained descriptor;
its parent chain must be root/service-owned and non-writable by other principals.
Keep reference assets under
`/var/lib/talktohermes-omnivoice` when using the supplied hardened systemd unit.
The token is a standalone ASCII value of 32–128 non-whitespace characters, with an
optional final newline.

Example preparation (paths are illustrative; do not commit deployment values):

```sh
sudo install -d -o talktohermes-omnivoice -g talktohermes-omnivoice -m 0700 \
  /etc/talktohermes-omnivoice /var/lib/talktohermes-omnivoice
sudo install -o talktohermes-omnivoice -g talktohermes-omnivoice -m 0600 TOKEN_FILE \
  /etc/talktohermes-omnivoice/token
```

Install the locked Python project into `/opt/talktohermes-omnivoice/venv`, install
`deployment/talktohermes-omnivoice.service`, then run `systemd-analyze verify` and
enable the unit. `omnivoice==0.2.1` and its resolved dependency graph are pinned in
`uv.lock`; model loading additionally pins the immutable Hugging Face revision
`c5fdb5ccb189668d56333f77ba2629f4cd7535f4`. The real CUDA/Torch/model import and
synthesis path must be re-verified after deployment, dependency, model, GPU, or
runtime-path changes. Imports and model loading are lazy,
so local service unit tests need neither a GPU nor downloaded model weights.

## Verified target gate

An unprivileged target-host gate completed successfully with the pinned
`omnivoice==0.2.1` package and exact pinned model revision above, loaded offline
through CUDA with a private server-side reference pair. Authenticated readiness
returned `200 {"status":"ready"}` and authenticated synthesis returned a valid
PCM 16-bit mono, 24 kHz WAV. A separate STT pass recovered the exact German
words, and owner listening accepted the result without qualification. The
pre-existing unrelated service on port `8181` remained healthy during and after
the gate. The temporary process was stopped afterwards, its dedicated listener
was confirmed free, and private model, reference, and output artifacts remained
outside the repository.

No transactional installer is shipped: dependency/model/GPU provisioning cannot
be fully exercised on this local non-production host, and an unverified privileged
installer would weaken rather than harden deployment.

## Tests

Use an environment containing this project's pinned development dependencies:

```sh
pytest -q
```
