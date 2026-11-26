# Loop completion display

Identity: `loop-complete-display`

## Required behavior

`LOOP_COMPLETE` is a loop-control sentinel, not assistant content. Strip a trailing standalone top-level marker, including a marker-only reply, at every user-visible gateway, TUI, and CLI delivery boundary. Preserve marker text in prose and fenced code, and hold partial trailing markers during streaming so they never flash. Keep raw final text available to loop post-turn hooks so completion detection and loop shutdown remain unchanged.

## Proof surface

Focused tests cover fence-aware filtering, gateway streaming and final delivery, TUI payload and streaming deltas, CLI streaming, loop completion hooks, and existing silence-marker behavior. Run affected tests with `scripts/run_tests.sh`; run the exact-head gate before publication.

## Upstream disposition and retirement

Prepare the same behavior for upstream contribution without fork-only maintenance artifacts. Retire this fork record when equivalent upstream behavior is released and the fork no longer carries the display-boundary adaptation.
