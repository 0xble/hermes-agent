# PM Runtime Wheel Fixtures

These are the unmodified `py3-none-any` wheels from the registry URLs in
`pm/uv.lock`. Each wheel retains its upstream license and metadata. The fixture
validates every archive's SHA-256 against that lock before using it.

The offline staging tests must run with an empty dependency cache and no network.
Vendoring this small PM-only graph avoids both live PyPI availability and hidden
reliance on the application's environment or a developer's cache.

When updating `pm/uv.lock`, replace the matching fixtures with the universal
wheels at the new lock's exact URLs, verify their lock hashes, and remove the
superseded archives. Do not rebuild the wheels or substitute installed packages.
