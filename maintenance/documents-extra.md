# Documents Extra

Patch identity: `documents-extra`.

The optional `documents` extra pins the document toolchain that document skills call: python-docx, python-pptx, openpyxl, pypdf, pdfplumber, pypdfium2, lxml, pandas, matplotlib, beautifulsoup4, fonttools, Playwright, PyMuPDF and reportlab, which the bundled PDF skill's create, form and stamp scripts import. Versions reuse the lock's existing lxml, pandas and Playwright pins so the resolution keeps one copy of each. The extra is listed in `[tool.hermes] opt-in-extras`, so all-extras bundles leave it out and only a host that selects it installs it. PyMuPDF is AGPL-3.0 or commercial, which is another reason it must never ship by default. `[tool.hermes.extras-platforms]` and each requirement's marker gate it off Android, Termux (which reports `linux` with an Android kernel release and Bionic libc, so no manylinux wheel fits) and Windows ARM64, where PyMuPDF has no wheel and its sdist builds MuPDF from C. `extra_supported` passes `platform_release` so that guard evaluates at runtime. `pm.extras.ANCHORS` names its fourteen imports so `available("documents")` reports a real installation.

Reproduction: without the opt-in entry, `build_environment(all_extras=True)` installs the whole toolchain into every native bundle. Without the anchors, `pm.extras.available("documents")` looks for a module named `documents` and always returns false.

Proof surfaces: `tests/pm/test_extras.py` checks that declared gates match dependency selection on every target and that anchors name declared extras. `tests/pm/test_environment_build.py` checks that all-extras builds skip opt-in extras. `uv lock --check` verifies the lock against `pyproject.toml`.

Retire when upstream ships an equivalent opt-in document extra, or when no deployed host selects `documents`. Rollback removes the extra, its exclude-newer exemptions, its opt-in and platform entries, its anchors and the packages it added to the lock.
