"""Secret-source failure classification and bounded startup restart bookkeeping."""

from __future__ import annotations


class GatewayStartupSecretsMixin:
    """Keep transient credential-backend failures from restart-looping forever."""

    @staticmethod
    def _transient_exit_streak_path():
        from hermes_cli.config import get_hermes_home

        from gateway.gateway_slot import process_slot_filename
        return get_hermes_home() / process_slot_filename(".transient_startup_exits")

    def _reset_transient_exit_streak(self) -> None:
        """A platform connected, so the backend recovered; forget the retry budget."""
        try:
            self._transient_exit_streak_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — bookkeeping never blocks startup
            pass

    def _transient_exits_exhausted(self) -> bool:
        """Has the restartable escape hatch been used too many times in a row?

        Exiting restartably is right for a backend that is briefly slow, and wrong for one
        that is permanently gone: the generated systemd unit disables the generic start
        limiter and leans on the fatal-config exit code as its only backstop, so an
        unbounded restartable exit would loop every RestartSec forever with no parked
        state for an operator to find. After a few consecutive tries, park instead.
        """
        from gateway.run_startup import _TRANSIENT_EXIT_STREAK_LIMIT, logger

        path = self._transient_exit_streak_path()
        try:
            previous = int(path.read_text(encoding="utf-8-sig").strip() or 0)
        except Exception:  # noqa: BLE001 — absent or unreadable means no streak yet
            previous = 0
        # Clamp: a negative or absurd value in the file (corruption, a stray write, a
        # directory in the way) must not buy an unbounded number of restarts, which is
        # precisely the loop this bound exists to stop.
        streak = min(max(previous, 0), _TRANSIENT_EXIT_STREAK_LIMIT) + 1
        try:
            path.write_text(str(streak), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            # Unwritable means the streak can never advance, so the bound silently fails
            # open. Say so once rather than looping forever with no explanation.
            logger.warning(
                "Cannot record the secret-failure restart streak at %s (%s) — the restart "
                "bound cannot advance and this may loop until the backend recovers.",
                path, exc,
            )
        if streak < _TRANSIENT_EXIT_STREAK_LIMIT:
            return False
        logger.error(
            "Secret-source failures have blocked startup %d times in a row — treating this "
            "as a real configuration fault so the service parks visibly instead of "
            "restart-looping. Fix the secrets backend, then restart.", streak,
        )
        return True

    @staticmethod
    def _is_missing_credential_code(code: str) -> bool:
        """Does this fatal code mean "the credential never arrived"?

        Matched as a FAMILY, like :func:`is_global_startup_conflict`. Adapters spell it
        differently — ``missing_credentials``, ``MISSING_CREDENTIALS``, per-platform
        prefixes such as ``yuanbao_missing_credentials``, and Slack's, which is BUILT from
        the env-var name (``f"missing_{env_name.lower()}"`` → ``missing_slack_bot_token``)
        and so cannot be enumerated at all. An exact literal covered two adapters, and any
        narrower substring still misses the generated ones.

        "Missing" plus a word naming a secret is the discriminator. It deliberately
        excludes ``missing_dependency`` and ``MISSING_SDK`` (a real config fault: the code
        isn't installed) and the ``*-bot-token_lock`` family (an ownership conflict, which
        names a token but is not missing anything), both of which must stay fatal.
        """
        lowered = (code or "").lower()
        return "missing" in lowered and ("credential" in lowered or "token" in lowered)

    def _missing_credentials_blamed_on_secrets(self) -> bool:
        """True when every non-retryable startup failure is an absent credential AND this
        home's last secret-source apply lost secrets to a slow or unreachable backend.

        Both halves are required. An ownership conflict (a live foreign token holder, a
        polling lock) is a real single-writer conflict that restarting cannot resolve, so
        it must keep the fatal-config exit even when the secrets backend was also unwell —
        that conflation is what sank the previous attempt at this fix upstream.
        """
        codes = getattr(self, "_startup_nonretryable_codes", set())
        if not codes or not all(self._is_missing_credential_code(c) for c in codes):
            return False
        try:
            from agent.secret_sources.registry import last_apply_had_transient_failure
            from hermes_cli.config import get_hermes_home

            # This gateway's OWN home: secondary-profile hydration runs between the connect
            # results and this decision, and answers only for the home it hydrated.
            return last_apply_had_transient_failure(get_hermes_home())
        except Exception:  # noqa: BLE001 — never let this probe block the exit path
            return False
