"""Browser section of the default configuration.

Kept separate from the top-level defaults facade so browser settings can grow without
exceeding the facade's line-count ratchet.
"""

DEFAULT_BROWSER_CONFIG = {
    "backend": "",
    "snapshot_threshold": 15000,
    "record_sessions": False,
    "allow_unsafe_evaluate": False,
    "restrict_evaluate": False,
    "inactivity_timeout": 120,
    # Seconds a local Chromium launch may take to open its DevTools port; a launch always waits
    # at least 120 s (a cold first start unpacks the profile).
    "command_timeout": 30,
    # headed: visible Chromium window (local); skips per-turn cleanup, idle reaper still applies
    "headed": False,
    "allow_private_urls": False,  # legacy security.allow_private_urls; local/sidecar Chromium ignore it
    # Local browser engine. "auto" = Chromium; "lightpanda" = `lightpanda serve` per session
    # (faster navigation, no screenshots); "chrome" is read as "auto". Ignored while a cloud
    # provider, cdp_url or use_real_profile is active.
    "engine": "auto",
    # Chromium binary for local sessions. "" = pm's chromium (`hermes pm install chromium`), the
    # Docker image's build, else a system chromium where pm builds none (Termux).
    "executable_path": "",
    # Persistent Chromium profile directory shared by local sessions; "" = a fresh throwaway
    # profile per session.
    "user_data_dir": "",
    "chromium_args": [],  # extra Chromium flags for local sessions, e.g. ["--lang=de"]
    # Cloud provider: LAN/localhost URLs open in a local Chromium (ignores allow_private_urls; False refuses)
    "auto_local_for_private_urls": True,
    "cdp_url": "",  # persistent CDP endpoint for attaching to an existing Chromium/Chrome
    # Fail-closed browser selection for business-agent profiles. target is an HTTP(S)/WS(S)
    # endpoint (Unix sockets must be fronted by a loopback proxy); fallback defaults to legacy behavior.
    "target": "",
    "fallback": "allow",  # allow = legacy provider/local fallback; refuse = never select another browser
    # Consent to browse with the user's REAL logins locally: runs on a Hermes-managed SNAPSHOT
    # of the ACTIVE default-Chromium profile (Local State -> profile.last_used; cookies, logins,
    # prefs copied and re-synced per fresh session) driven by Hermes' packaged Chromium. The
    # snapshot dir sidesteps Chrome 136+'s default-profile debugging block and never contends
    # with the running browser. Turning off deletes ~/.hermes/browser-profile/ so credentials
    # don't outlive consent. Chromium-family only; a profile that can't be copied (Firefox default,
    # Windows browser holding its cookies) gives a fresh, signed-out browser plus a note saying why.
    # Also gates the browser_exec `local` arg (real-profile session under a cloud backend). Desktop Settings -> Browser.
    "use_real_profile": False,
    # Windows only: a running Chrome/Edge/Brave locks its cookie DB (the last snapshot is reused;
    # with none yet, a fresh browser runs). When on, the agent ASKS first; on approval it runs
    # `hermes browser close-profile` (kills that profile's browser tree, unsaved tabs lost) so a
    # new session can copy it; no auto-kill. No effect on macOS/Linux, where a running
    # browser only locks the password/autofill DBs, which are skipped (cookies still copy).
    "real_profile_autoclose": False,
    # Pin WHICH source profile directory is snapshotted for real-profile browsing (e.g. "Profile
    # 2"). Empty = browser's last-used profile, which on multi-profile machines can hand the
    # agent the wrong identity. A pin naming a missing directory never falls back to another
    # profile: the session gets a fresh browser without the user's logins.
    "real_profile_pin": "",
    # CDP supervisor: dialog + frame detection over a persistent WebSocket; active only with a
    # CDP-capable backend (Browserbase, or local Chrome via /browser connect). See
    # website/docs/developer-guide/browser-supervisor.md.
    "dialog_policy": "must_respond",  # must_respond | auto_dismiss | auto_accept
    "dialog_timeout_s": 300,  # safety auto-dismiss after N seconds under must_respond
    "camofox": {
        # true = send a stable profile-scoped userId so Camofox maps it to a persistent Firefox
        # profile; false = random ephemeral userId per session.
        "managed_persistence": False,
        # Externally managed Camofox identity, for when another app owns the visible browser.
        "user_id": "",
        "session_key": "",
        # External user_id only: reuse its existing tab before creating one. Hermes-managed
        # identities (accounts, managed_persistence) always reuse their own tabs.
        "adopt_existing_tab": False,
        # Camofox's CAMOFOX_UPLOADS_DIR as seen from this host; browser_upload stages files here.
        # Empty = Camofox's default ~/.camofox/uploads.
        "uploads_dir": "",
        # Docker Camofox opens page URLs from inside the container: rewrite loopback page URLs
        # (localhost/127.0.0.1/::1) to the host alias; CAMOFOX_URL itself is unchanged.
        "rewrite_loopback_urls": False,
        "loopback_host_alias": "host.docker.internal",
    },
    # Authenticated browser-extension controller lane: a registered extension can become the
    # exact controller for a session's browser_* tools (fail-closed once bound). Local API
    # registration also requires the API server bearer key. developer_mode gates the privileged
    # browser_cdp / browser_evaluate capabilities.
    "extension_control": {"enabled": False, "developer_mode": False},
}
