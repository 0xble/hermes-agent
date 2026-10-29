# Direct web extraction and local docs

Patch identity: `web-extract-direct`.

## Required behavior

After the existing secret-URL, SSRF, provider-resolution and website-policy gates, fetch plain files directly with redirect-hop and connect-time SSRF checks, a 15-second timeout and 5 MB cap. Serve Hermes docs from `website/docs` without escaping that root. Preserve provider fallback, cache isolation, batch ordering, truncation and `web.extract_direct: false`.

## Provenance and divergence

The maintained fork adds this route to avoid paid extraction for plain files (about 19% of roughly 6,050 recent fetches) and checked-in docs pages (another 10%). The fork base is `origin/main` at `6260848788a6a0f0c52b6b3fe8a952fbb2daaf97`; upstream `main` was `449fae030aa6b51105db3e440610c867e6b91024` at development. Related upstream issue [#115826](https://github.com/NousResearch/hermes-agent/issues/115826) describes stale Exa extracts; no equivalent direct/local route was found in the inspected upstream PR and issue search. No upstream contribution was requested.

## Proof and retirement

Run `scripts/run_tests.sh tests/tools/test_web_*.py -q`, `ruff check tools/web_extract_direct.py tools/web_tools_extract.py tests/tools/test_web_extract_direct.py`, and `python3 scripts/check-windows-footguns.py tools/web_extract_direct.py tools/web_tools_extract.py tests/tools/test_web_extract_direct.py`. Retire the fork implementation when a released upstream version passes this contract's safety and routing tests without it. Revert this patch's four behavior/config/test/doc files and this maintenance-unit row only; preserve other web extraction changes and the remaining maintenance units.
