"""Secret-source failure classification and bounded startup restart bookkeeping."""

from __future__ import annotations

from typing import Optional


def _own_policy_envs(platform) -> Optional[tuple]:
    """Return the platform's declared authorization environment names, if any.

    The registry lookup is intentionally resolved at call time so plugin-provided
    authorization policy remains effective even when a platform is loaded lazily.
    """
    from gateway.platform_env import authz_field
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get(platform.value)
    own = authz_field(platform.value, "own_policy_env", entry)
    if not own:
        return None
    return (*own, authz_field(platform.value, "allow_all_env", entry))


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
