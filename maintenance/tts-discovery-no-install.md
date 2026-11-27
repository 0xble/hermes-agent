# Retired TTS discovery without installation

Fork patch identity: `tts-discovery-no-install`.

Retired on 2026-11-27. Upstream commit `398867326db` ("fix(voice): requirement checks answer from state, never install"), released in `v0.23.2`, answers TTS capability checks from installed state through `_BUILTIN_REQUIREMENTS` and `_pm_extra_available`, and leaves SDK installation to actual TTS use. That is the complete contract of this unit. The fork's `tools/tts_tool.py` already equals the `v0.23.2` file, so no private implementation remains.

The fork's own regression was already dropped in a sync. Upstream's `tests/tools/test_tts_requirements_passive.py`, unchanged in the fork, covers the behavior.

This record stays so published `Fork-Patch: tts-discovery-no-install` trailers keep an accountable owner.

Contract tests: none, retired. The behavior and its regression are upstream-owned.
