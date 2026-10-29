# Credits and third-party code

Mercury is built on **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** by Nous Research, which inspired the project and provides Mercury's agent engine.

- `engine/` is the Hermes Agent source at release **v0.22.5** (commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e`), copied with one small patch (below). To keep the repository small, these folders were left out because Mercury does not use them at runtime: `.git`, `tests`, `tests-js`, `apps` (desktop apps), `website` (docs site), `contributors`, `evals`, `nix`, `.github`, and upstream's development-only files (contributor and security-process docs, translations, tooling config).
- Hermes Agent is released under the MIT License, Copyright (c) 2025 Nous Research. Its full license text is in [`engine/LICENSE`](engine/LICENSE) and applies to everything under `engine/`.

**Patch to the engine** (marked `Mercury patch` in the source): `engine/pm/artifact_mirror.py` and `engine/setup-hermes.sh` accept `HERMES_PREFER_ARTIFACT_MIRROR=1` to download pinned binaries from the engine's own checksum-verified mirror before GitHub. Some networks throttle GitHub release downloads. Every file is still verified against its pinned SHA-256.

Everything outside `engine/` is Mercury's own code, under the MIT License in [`LICENSE`](LICENSE).
