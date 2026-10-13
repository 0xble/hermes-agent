# Immutable-release PM extras

Patch identity: `immutable-release-pm-extras`.

The immutable release builder carries enabled leaf extras into the candidate
`.venv`. The first PM generation for that release replaces the payload and
must derive the same declared optional extras from its installed anchors before
writing the new `facts.json`; otherwise a user-enabled provider can disappear
on the first gateway boot. `hindsight_client` is the anchor for the declared
`hindsight` extra and the active memory provider must remain available after a
release promotion.

Regression coverage: `tests/pm/test_extras.py::test_immutable_release_promotion_preserves_hindsight_in_new_facts`
creates the previous install's `facts.json`, promotes a candidate payload with
`hindsight_client`, and verifies the candidate facts retain `hindsight`.
The focused guard is `HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/pm/test_extras.py`.

Retire this record when the upstream immutable-release/PM handoff persists all
declared active optional extras across the first generation without this fork
anchor mapping and the regression remains covered there.
