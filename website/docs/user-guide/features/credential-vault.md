---
title: Passwords & Logins
description: The agent signs into sites, pays and fills addresses for you without ever seeing a password.
---

# Passwords & Logins

Say **"log into GitHub"** and the agent signs in for you. The first time it
reaches a sign-in page it has no login for, it asks you, right there, in a
masked prompt. After that it just works. Passwords are encrypted on this
machine and injected straight into the page; the model never sees them.

There is nothing to set up.

## What it looks like

**CLI / TUI**

```
🔐 Save login for github.com
   The agent reached a sign-in page with no saved login for this site.
   Type the email / username you sign in with (shown), then Enter.
   ...
   Now the password (hidden). It is encrypted on this machine, bound to
   https://github.com, and filled into the page without the model ever seeing it.
```

**Desktop** — a "Save your github.com login?" card with an identifier field and
a masked password field. *Save & sign in* stores it and continues; *Don't save*
tells the agent to stop asking for this turn.

From then on the agent lists your saved logins, types the identifier itself and
fills the password through Hermes. The tool result it sees is
`{filled_fields: 1, origin: "https://github.com"}`; the password is also
registered with the redactor so a later page read cannot echo it back.

### Protected date fields

Configured date-of-birth fields are filled only on their exact saved origin **in a
private per-task local Chromium browser**. The fill refuses Camofox (including
managed identities and named accounts), attached `/browser connect` or CDP
browsers, real-profile, Bot Desktop shared browsers, Lightpanda, cloud browsers,
and sessions without verified task ownership. The named brianle/lpg/meridian
Camofox account browsers cannot fill birth dates; navigate with a fresh local
per-task browser instead. After a protected-date fill, every browser-derived
result in that browser session,
including snapshots, evaluations, console/CDP output and iframe output,
redacts the full date and its exact year/month/day components. This is intentionally
session-scoped: unrelated standalone numbers equal to a component (for example `4`,
`12`, or `1990`) are also masked, on every tab, until the browser session closes.
Navigating or focusing another tab does not lift it, because the filled tab may still
be open. Screenshots remain refused for the same lifetime.

## Two-factor codes

Sites that ask for a code after the password are handled the same way:

- **Authenticator key saved with the login** (the "setup key" or `otpauth://`
  link a site shows when you enable 2FA; 1Password and Bitwarden items that
  hold a TOTP seed count too): Hermes generates the current code and enters
  it. Nobody is asked. Add the key in **Settings → Passwords & Logins → Add**
  or `hermes vault add`; the item shows a *2FA auto* badge. Like the
  password, the code is only generated for a page on the login's saved site.
- **Code sent to your phone or email**: a small prompt appears in your
  surface ("Verification code for github.com"), you type the code, Hermes
  enters it into the page. The code never enters the conversation either.
- **Passkeys, hardware keys, app approvals** ("tap Approve in Duo"): nothing
  to type. The agent tells you to complete it on your device and waits for
  the page to move on.

## Already using 1Password or Bitwarden?

Nothing to enable. If the `op` or `bw` command-line tool is installed and signed
in, Hermes picks it up automatically and its website logins become fillable
alongside the local ones. The first time the agent needs one of those logins it
asks you to unlock the manager with your master password (masked prompt; once
per session, 30 minutes idle). Hermes hands the master password to the manager's
CLI through its non-interactive channel (`op signin` on stdin, `bw unlock
--passwordenv` in the child's environment) and keeps only the session token in
memory. The agent never sees the master password, the token, or any login.
A manager item that lists several websites (say `amazon.co.uk`,
`www.amazon.co.uk` and `eu.account.amazon.com`) fills on each of those exact
origins; nothing is inferred beyond the URLs saved on the item.

Prefer not to use a detected manager? `hermes vault sources --disable bitwarden`,
or the switch in **Settings → Passwords & Logins**.

## Paying and filling addresses

Cards and addresses work the same way as logins: saved once (**Settings →
Passwords & Logins → Add**, or `hermes vault add`), bound to the checkout site,
and filled by the agent on that site only. **Every card fill asks you first**,
with the same approval prompt as a dangerous command; declining writes nothing.
Headless sessions (cron, webhooks, the API server) cannot confirm and are
refused, so a prompt injection that reaches a checkout page can ask, but it
cannot spend. Address fills need no confirmation.

## Managing what's saved

- **Desktop → Settings → Passwords & Logins**: everything saved, the detected
  password managers with Unlock/Lock, Add, Remove.
- **CLI**: `hermes vault list`, `hermes vault add`, `hermes vault rm <handle>`,
  `hermes vault sources`, `hermes vault key`.

Items live encrypted under `~/.hermes/vault/`, scoped to the profile. Labels,
site origins and login identifiers are visible metadata; passwords and card
values never leave the vault except into the page.

## Where the vault key is kept

By default the encryption key sits in `~/.hermes/vault/vault.key` (`0600`),
next to the encrypted `vault.json.enc`. You can keep it in your operating
system's keychain instead (macOS Keychain, Windows Credential Manager, the
Secret Service / KWallet on Linux):

```bash
hermes vault key                          # where the key is now, and whether a keychain is reachable
hermes vault key migrate --to keychain    # move it (installs the small `keychain` feature if needed)
hermes vault key migrate --to file        # move it back
```

The move writes the key to the keychain, checks that the keychain copy decrypts
your vault, and only then deletes `vault.key`, so an interrupted move leaves a
working vault and running the command again finishes it. It also sets
`vault.key_storage`, which decides where a **new** vault's key goes:

```yaml
vault:
  key_storage: file        # default; or: keychain
```

With `keychain` and no reachable keychain (a headless Linux server, an SSH
session without a desktop keyring), a new vault's key goes to `vault.key` as
before, with a warning in the log. Each profile gets its own keychain entry,
named in `~/.hermes/vault/vault.keyref` (which holds no secret). A profile
made with `hermes profile create --clone-all` gets an entry of its own while it
is being created, so the original moving its key back to a file later cannot
leave the copy without a key; if the keychain cannot be reached at that moment,
the clone is refused with a message instead of sharing the original's entry. A
renamed profile switches to an entry of its own the first time it opens its
vault, and never deletes the entry it came from.

What changes in keychain mode:

- **Backups and copies hold the vault without its key.** `hermes backup`, a
  synced or copied `~/.hermes`, or a disk image no longer carries everything
  needed to open the vault. Before moving a profile to another machine, run
  `hermes vault key migrate --to file`.
- **The keychain may ask.** macOS can ask whether Hermes may use the entry
  (for example after an update replaces Hermes' Python), and a locked Linux
  keyring asks to be unlocked. Cron jobs that run while you are logged out
  cannot read the key, so their local logins fail with a clear error.
- **A lost entry means lost items.** If the keychain entry is deleted, the
  vault cannot be opened and Hermes says so instead of creating a new key over
  it. Restore the entry from a keychain backup, or delete `vault.json.enc` to
  start over.

That is why keychain storage is opt-in rather than the default.

### Protected fields from 1Password

An operator can expose one sensitive 1Password field to browser autofill without
exposing its value to the model. Each entry binds an `op://` reference to one
supported semantic and an exact HTTPS origin:

```yaml
vault:
  onepassword:
    protected_fields:
      - label: Traveler date of birth
        reference: op://Personal/Traveler/birthdate
        semantic: bday
        value_type: date
        origins: [https://www.example-airline.com]
```

`browser_vault_list` returns only an opaque handle, label, semantic and origin.
`browser_vault_fill` resolves the field server-side and fills only a matching
birth-date control on that exact origin, rechecked inside the page immediately
before the write. Protected references require a CLI session or service-account
token; Connect-only profiles do not advertise them because Connect cannot resolve
arbitrary `op://` references. Current protected-field support is limited to birth
dates; unsupported semantics or malformed entries are ignored.

The fill path also protects model-visible browser output. Labels (such as `April`),
full date strings and four-digit years remain redacted as exact registered values.
Every browser result in the session, whether a structured snapshot or an
arbitrary-code channel (`browser_eval`, `browser_exec`, CDP `Runtime.evaluate`,
including OOPIF evaluation), passes one boundary that masks standalone components.
That includes unrelated values such as `{"bookings": 4}`. This registry is in memory
and does not infer provenance from page structure or JavaScript source text.
Screenshots and CDP pixel captures are unavailable in that session; do not
handoff a protected page into a shared browser. The registry clears only after
a confirmed browser close. If close fails, reads stay masked and pixels blocked;
raw CDP (both target and frame routing) is refused while any protected browser remains open.

### Login origin aliases

A 1Password Login can be bound to more than one exact sign-in origin without
rewriting the item. When a fill returns `origin_mismatch`, the agent may add the
exact page origin under the item handle or raw 1Password item ID with
`hermes config set`:

```shell
hermes config set vault.origin_aliases.<item-id> '["https://signin.example.com"]'
```

`set` replaces the entire value for that item, so include existing aliases when
adding another origin. The config is reread for each vault list/fill call; no
restart is required. Aliases are HTTPS-only, exact-origin matches. Entries with
a path, an `http` scheme, or a wildcard are ignored. Hermes does not infer
parent or subdomain origins and applies an alias only to the named login. After
adding an alias, retry the fill. The first alias-only fill asks the user to
confirm the full origin and item label unless `vault.confirm_alias_fills` is
false. A
cross-registrable-domain alias is logged as an advisory warning when prompts
are disabled, and otherwise warns in the confirmation detail when the advisory
fallback recognizes those suffixes. The warning is not an authorization check
and has partial suffix coverage; the full origin is always shown. Saved-origin
fills do not need that extra prompt. Never edit or rewrite an existing shared
1Password item to add a website URL, because template rewrites can remove
passkeys.

## Headless sessions

Cron jobs, webhooks, the API server and `hermes chat -q` have nobody to answer a
prompt. Saved local logins keep working there; a locked password manager reports
`unavailable_in_this_session` and a missing login reports `prompt_unavailable`.
Unlock or save from an interactive session first, or give 1Password a service
account token (`OP_SERVICE_ACCOUNT_TOKEN`).

```yaml
vault:
  onepassword:
    enabled: false          # opt OUT of a detected manager (default: on when installed)
    account: ""             # `op --account` shorthand; empty = default
    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN
  bitwarden:
    enabled: false
```

### Several 1Password accounts

A service-account token reads every vault it was granted, but only in its own
account. To add logins from another account (a business account next to a
personal one), list it under `accounts` with its own service-account token:

```yaml
vault:
  onepassword:
    accounts:
      - alias: business                       # handles become op@business:<item-id>
        account: business.1password.com
        service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN_BUSINESS
        browser_account: work                 # optional: fill only in this Camofox account
```

Each additional account authenticates only with its own token, never with
1Password Connect or an unlocked desktop session, so a handle can never resolve
under another account's credential. An entry without a unique alias, an account,
and a token variable of its own is ignored. `browser_account` limits fills and
one-time codes to tasks whose browser uses that named account.

## What this does and does not guarantee

**Does:** the password never enters the model's context through Hermes: not in
tool results, logs, the session database, or the CLI arguments of any process.
Fills happen over the supervised browser session's direct CDP socket and are
refused unless the page origin exactly matches the saved origin, checked again
inside the page immediately before the write.

**Does not:** protect against the page itself. Once a password is typed into a
site, that site (and any script it runs) has it, exactly as when you type it
yourself. On a cloud browser backend the vendor's browser sees the page like any
other. The origin binding is the guard against filling on the wrong site, not
against a compromised right one.

Nor does it isolate the browser from this machine. Masking and screenshot refusal
apply to Hermes' browser tools. A local process that connects straight to the
browser's debugging endpoint, including a shell command run through the terminal
tool, bypasses them and can read the page like any other client. This applies
equally to filled passwords and protected dates.

### Who can get at the vault

| Who | `vault.key` file (default) | Keychain storage |
|---|---|---|
| Other users on this computer (not administrators) | Blocked: both files are `0600` | Blocked |
| Someone with a backup, sync copy or image of `~/.hermes` | Can decrypt: key and vault travel together | Gets ciphertext only |
| Someone with the powered-off disk and no full-disk encryption | Can decrypt | Needs whatever protects that keychain (on macOS and Windows, your login password; a Linux keyring left unencrypted protects nothing) |
| A program running as you, or a plugin running inside Hermes | Can decrypt | Can still ask the keychain, or call the vault code directly |
| An administrator / root, or malware with your privileges | Can decrypt | Can decrypt |

Password-manager session tokens (1Password, Bitwarden) stay in Hermes' memory
only, are dropped after 30 idle minutes or on Lock, and are handed only to the
single `op`/`bw` command that needs them, never to Hermes' own environment, the
terminal tool or any other child process. They are kept out of reach of a plain
import, which stops casual access by other code in the process, but not a
plugin determined to read Hermes' memory.

Plugins are the boundary that matters: a third-party plugin that runs inside
the Hermes process can do anything Hermes can, including filling a saved login.
If you save logins and install plugins you have not reviewed, run them out of
process with [`plugins.isolation: host`](plugins.md#running-plugins-out-of-process-pluginsisolation).
A plugin host still runs as your user and can read files you can read, so for
a hard boundary give it a sandbox with `plugins.host.launcher`.
