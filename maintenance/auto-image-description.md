# English Auto-Image Descriptions

Load when changing gateway auto-image enrichment, its prompt, or the vision tool
call boundary. Fork patch identity: `auto-image-description-english`.

## Required Divergence

Gateway-generated image descriptions use English prose while preserving quoted
visible text and identifiers in their original language. Keep the existing
concise subject/text/data/context coverage, user caption, re-examination path,
error notes, and auxiliary-client token handling. Explicit user-invoked vision
tool language behavior is outside this patch.

## Provenance and Adoption

Fork base: `5a9c9ce6b1f5ce0bdcc3b24a2d350fef3e66b873`.
Upstream design preflight pinned `NousResearch/hermes-agent` main
`c73cbcc4cbbdbfc7371a86287db1ff729f23837b`, including root and gateway agent
guidance and `CONTRIBUTING.md`. Read-only issue/PR searches for vision language,
English images, and auto vision found no equivalent landed repair.
[Issue #141738](https://github.com/NousResearch/hermes-agent/issues/141738)
reports this exact dual-language prompt and proposes broader language/prompt
configuration. It was open with no comments or matching PR at preflight.

Selected route: a fork-only prompt contract, not the issue's configuration
proposal. No upstream writes, provider/model changes, translation layer, or
runtime activation are part of this unit. The bilingual length hint originated
in `e6b168855b40` on 2026-08-08 to keep preprocessing concise. Retain that intent.
The prompt's age and a current generation do not establish the cause of any
historical language change.

## Proof and Retirement

`tests/gateway/test_vision_preprocess.py` imports the real gateway enrichment
boundary and captures its actual outgoing vision request. It protects English
prose guidance and original-language quotes/identifiers across absent, English,
and non-English captions, along with exact enrichment text and uncapped tool
arguments. Real synthetic-image generations are observations, not deterministic
proof of a model's output language or historical provider routing.

Retire when an adopted upstream release provides this gateway behavior without
changing explicit vision requests or the surrounding enrichment contract.
Rollback is a source revert. Runtime deployment remains separately authorized.
