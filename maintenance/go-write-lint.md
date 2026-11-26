# Go Write Lint Scope

Fork patch identity: `go-write-lint-syntax`.

## Decision and provenance

The post-write lint for `.go` files checks syntax only, with `gofmt -e FILE`. It does not type-check. Upstream runs `go vet FILE` (`tools/file_operations_lint.py`, unchanged at `bf0203a794a07aac3ff764972ab9087baa4e4f4e`). That type-checks the one file without the rest of its package, so valid split-file code, such as a test using a type declared in a sibling file, reports `undefined: <Type>` after every write while the package's own `golangci-lint`, `go vet ./...` and tests pass. Agents learn to ignore a check that fires on correct code, which hides genuine failures.

Type, vet and lint checking stay with the package-level gates the repository runs, and with the Go LSP tier, which still supersedes this shell linter when a server claims the file (`_SHELL_LINTER_LSP_REDUNDANT`).

Delivery route: an owned-fork PR only. No upstream publication. Activation comes with the next normal promotion.

## Proof

`tests/agent/lsp/test_shell_linter_lsp_skip.py`:

- `test_go_file_using_a_sibling_type_is_not_flagged`: valid split-file package code passes. It fails on upstream's `go vet FILE`.
- `test_go_syntax_error_is_reported`: a genuine syntax error still fails lint and names its line.

## Adoption and retirement

On upstream sync, keep this patch while upstream's per-file Go linter type-checks a single file. Retire it if upstream switches to a syntax-only or package-aware check that passes both tests.
