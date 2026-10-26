# KittenTTS Wheel Integrity

## Required Behavior

The optional `kittentts` extra must install the declared KittenTTS wheel only from its pinned release URL and verified SHA-256. Preserve the existing platform markers so unsupported Windows ARM64 and Intel macOS targets do not attempt installation, and keep the lockfile consistent with the declaration.

The hash protects a direct wheel dependency from silently changing after the source was reviewed. This unit owns the dependency declaration and lock refresh, not TTS capability discovery or lazy-install policy.

## Provenance And Disposition

Fork patch identity: `kittentts-wheel-integrity`.

Fork-Patch-Backfill: 739ced4676c82324042e5b0b8d7e3ba1ed7bf432; kittentts-wheel-integrity

The source surfaces are `pyproject.toml` and `uv.lock`. The declaration uses the verified SHA-256 for the KittenTTS `0.8.1` wheel and retains the existing platform exclusions. The lock refresh records the same resolved artifact without broadening the extra's availability.

Retire this patch when the dependency is published through a trusted package index with equivalent artifact integrity and platform coverage. Rollback removes the direct wheel hash and the corresponding lock refresh together.

## Verification

- `pyproject.toml` contains the direct wheel URL, exact SHA-256, and unchanged platform markers.
- `uv.lock` resolves the declared KittenTTS artifact consistently.
- The optional-extra dependency checks reject a changed wheel hash before installation.
