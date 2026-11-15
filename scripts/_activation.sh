# Sourced, never executed: how a POSIX shell gets the Hermes dev environment.
# `activate` (interactive, reversible) and `scripts/run-in-hermes-env` (a child
# process, one-way) both build on it; neither owns any of it.
#
#   hermes_sync REPO [FLAG]         bring the install up to date (setup-hermes.sh)
#   hermes_compose_env REPO DIALECT print the composed environment, `sh` or `fish`
#   hermes_apply_env SCRIPT         evaluate a composed `sh` script into this shell
#   hermes_activation_current REPO  is the inherited environment still REPO's?
#   hermes_ensure_env REPO          sync, compose and apply unless it is current
#   hermes_checkout_env REPO        REPO/.venv for tools when setup is refused
#
# pm records, beside the installed-state file named by __HERMES_ACTIVATED, one
# stamp per dependency input carrying the exact mtime that input had when the
# install was last verified against it (pm.environments.record_activation_inputs).
# Any input whose mtime DIFFERS from its stamp (newer or older, since a branch
# switch can move it either way) means the inherited environment may not match
# its inputs. `-nt`/`-ot` are bash builtins, so the check costs no process
# spawn, and every successful sync re-records, so one re-sync settles it.
# Missing stamps or inputs, a dangling sentinel or a literal "1" all read as
# stale, and so does an install that did not build the test environment:
# every environment this library composes includes it.

# hermes_sync REPO [--test-environment[=EXTRAS]]: run setup in a child, so a
# failure cannot exit or half-change the caller's shell and setup never
# republishes launchers or shell config. Progress goes to stderr.
hermes_sync() {
    local repo="$1" test_environment="${2:---test-environment}" python="" setup_python=()
    # setup-hermes.sh changes to this directory and sanitizes these variables.
    # Resolve the interpreter there too, then pass it through: admission and PM
    # must not silently select different runtimes.
    repo="$(cd "$repo" && pwd -P)" || return 1
    # shellcheck source=scripts/_setup_guard.sh
    . "$repo/scripts/_setup_guard.sh" || return 1
    python="$(cd "$repo" && hermes_bootstrap_python "$repo")" || python=""
    hermes_require_safe_setup "$repo" "$python" || return 1
    [ -z "$python" ] || setup_python=("--bootstrap-python=$python")
    (
        cd "$repo" || return 1
        unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
        bash "$repo/setup-hermes.sh" --runtime-only "$test_environment" ${setup_python[@]+"${setup_python[@]}"}
    ) >&2
}

# hermes_bootstrap_python REPO: print the interpreter that can run pm before
# any dependency is importable. It only emits the environment; it installs nothing.
hermes_bootstrap_python() {
    local repo="$1" store candidate
    for candidate in "$repo/.venv/bin/python" "$repo/.venv/Scripts/python.exe" \
                     "$repo/venv/bin/python" "$repo/venv/Scripts/python.exe"; do
        [ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
    for store in "${HERMES_RUNTIME_DIR:-}" "$repo/../tools" "${HERMES_HOME:-$HOME/.hermes}/tools"; do
        [ -n "$store" ] || continue
        for candidate in "$store"/python-*/bin/python3 "$store"/python-*/python.exe \
                         "$store"/python-*/bin/python "$store"/python-*/bin/python.exe; do
            [ -x "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
        done
    done
    return 1
}

# hermes_compose_env REPO DIALECT: print the environment of the installed state
# as a script for a shell of DIALECT (`sh` or `fish`).
hermes_compose_env() {
    local repo="$1" dialect="$2" python script
    python="$(hermes_bootstrap_python "$repo")" || {
        echo "no bootstrap Python found; run setup-hermes.sh" >&2
        return 1
    }
    script="$(PYTHONHOME= PYTHONPATH="$repo" "$python" -m pm.environments --format "$dialect")" &&
        [ -n "$script" ] || {
        echo "could not read pm env (run ./setup-hermes.sh first)" >&2
        return 1
    }
    printf '%s\n' "$script"
}

# hermes_apply_env SCRIPT: evaluate a script from `hermes_compose_env REPO sh`.
hermes_apply_env() {
    # Resolved before the eval replaces PATH.
    local cygpath name
    cygpath="$(command -v cygpath 2>/dev/null || :)"
    eval "$1"
    # The MSYS/Cygwin runtime hands a native Windows Python these variables in
    # Windows form (`C:\a;C:\b`) and converts them back only for its own
    # children. Exported verbatim, bash splits PATH on ':' and finds no commands.
    if [ -n "$cygpath" ]; then
        PATH="$("$cygpath" -u -p "$PATH")"
        for name in HOME TMPDIR TMP TEMP; do
            if [ -n "${!name-}" ]; then
                printf -v "$name" '%s' "$("$cygpath" -u "${!name}")"
            fi
        done
    fi
}

# hermes_activation_current REPO: status 0 when the inherited environment
# matches REPO's current inputs.
hermes_activation_current() {
    local repo="$1" sentinel stamps stamp input stamped=0
    [ -n "${__HERMES_ACTIVATED:-}" ] && [ -e "${__HERMES_ACTIVATED}" ] || return 1
    # activate.ps1 records a Windows path; Git Bash accepts it with slashes.
    sentinel="${__HERMES_ACTIVATED//\\//}"
    stamps="${sentinel%/*}/inputs"
    [ -f "$stamps/.project-root" ] && [ -f "$stamps/.test-environment" ] || return 1
    local owner="$repo" recorded
    recorded="$(< "$stamps/.project-root")"
    if command -v cygpath >/dev/null 2>&1; then
        owner="$(cygpath -am "$repo")" || return 1
        recorded="${recorded//\\//}"
        [ "${owner,,}" = "${recorded,,}" ] || return 1
    else
        [ "$(cd "$owner" && pwd -P)" = "$recorded" ] || return 1
    fi
    for stamp in "$stamps"/* "$stamps"/*/*; do
        [ -f "$stamp" ] || continue
        stamped=1
        input="$repo/${stamp#"$stamps"/}"
        if [ ! -e "$input" ] || [ "$input" -nt "$stamp" ] || [ "$input" -ot "$stamp" ]; then
            return 1
        fi
    done
    [ "$stamped" = 1 ]
}

# hermes_checkout_env REPO: give a tool REPO/.venv instead of setup when setup
# would write into a live runtime home (scripts/_setup_guard.sh refuses it).
# An existing .venv is only used. A missing one, or one this function built
# whose inputs have since changed, is built by PM with every PM write (install
# state, uv, interpreter, cache) scoped to a tooling home outside the runtime
# homes, so the live home is only read. scripts/run_tests.sh refuses before it
# could reach this, and the environment carries no activation sentinel.
hermes_checkout_env() {
    local repo="$1" venv stamps bin
    repo="$(cd "$repo" && pwd -P)" || return 1
    venv="$repo/.venv" stamps="$repo/.venv/.hermes-tooling-inputs"
    if [ -d "$stamps" ] && { ! hermes_checkout_venv_bin "$venv" >/dev/null ||
                             ! hermes_checkout_inputs_current "$repo" "$stamps"; }; then
        rm -rf "$venv"
    fi
    bin="$(hermes_checkout_venv_bin "$venv")" || {
        hermes_build_checkout_env "$repo" && bin="$(hermes_checkout_venv_bin "$venv")"
    } || return 1
    unset PYTHONHOME __HERMES_ACTIVATED __HERMES_TEST_PYTHON
    export VIRTUAL_ENV="$venv" PATH="$bin:$PATH" PYTHONPATH="$repo"
}

# The checkout dependency inputs a built .venv records (mtime equality, as above).
_HERMES_CHECKOUT_INPUTS="uv.lock pyproject.toml setup.py"

hermes_checkout_venv_bin() {
    local dir
    for dir in "$1/bin" "$1/Scripts"; do
        { [ -x "$dir/python" ] || [ -x "$dir/python.exe" ]; } && { printf '%s\n' "$dir"; return 0; }
    done
    return 1
}

hermes_checkout_inputs_current() {
    local repo="$1" stamps="$2" name
    for name in $_HERMES_CHECKOUT_INPUTS; do
        [ -e "$repo/$name" ] || [ -e "$stamps/$name" ] || continue
        [ -e "$repo/$name" ] && [ -e "$stamps/$name" ] || return 1
        if [ "$repo/$name" -nt "$stamps/$name" ] || [ "$repo/$name" -ot "$stamps/$name" ]; then
            return 1
        fi
    done
}

hermes_build_checkout_env() {
    local repo="$1" home python records reason name
    home="${XDG_CACHE_HOME:-$HOME/.cache}/hermes-agent/tooling"
    python="$(cd "$repo" && hermes_bootstrap_python "$repo")" || {
        echo "no bootstrap Python found to build $repo/.venv; run setup-hermes.sh" >&2
        return 1
    }
    # The tooling home is admitted by the same runtime resolution as setup.
    # shellcheck source=scripts/_setup_guard.sh
    . "$repo/scripts/_setup_guard.sh" || return 1
    records="$(hermes_guard_query "$repo" "$python" paths "$home")" || {
        echo "error: could not resolve the tooling home $home" >&2
        return 1
    }
    if reason="$(hermes_guard_records_live_reason "$records")"; then
        echo "error: the tooling home $home is a live runtime home ($reason)" >&2
        return 1
    fi
    echo "▶ setup would write into a live runtime home; building $repo/.venv for tools (PM state in $home)" >&2
    (
        cd "$repo" || exit 1
        unset PYTHONHOME PYTHONPATH VIRTUAL_ENV HERMES_RUNTIME_DIR
        HERMES_HOME="$home" "$python" -m pm.build_env --source "$repo" --out "$repo/.venv" \
            --group dev --group test
    ) >&2 || return 1
    mkdir -p "$repo/.venv/.hermes-tooling-inputs" || return 1
    for name in $_HERMES_CHECKOUT_INPUTS; do
        [ -e "$repo/$name" ] || continue
        touch -r "$repo/$name" "$repo/.venv/.hermes-tooling-inputs/$name" || return 1
    done
}

# hermes_ensure_env REPO: leave this shell with REPO's environment, doing
# nothing when the inherited one is current.
hermes_ensure_env() {
    local repo="$1" script
    hermes_activation_current "$repo" && return 0
    hermes_sync "$repo" || {
        echo "setup failed" >&2
        return 1
    }
    script="$(hermes_compose_env "$repo" sh)" || return 1
    hermes_apply_env "$script"
}
