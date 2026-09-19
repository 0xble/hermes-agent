# Candidate Hindsight bank contract

The candidate keeps Hindsight data partitioned by Hermes profile. Production
bank names are not activated by this fixture documentation. A profile's
`hindsight/config.json` uses a stable `bank_id_template`, for example:

```json
{
  "mode": "cloud",
  "bank_id": "hermes",
  "bank_id_template": "hermes-{profile}"
}
```

The `{profile}` placeholder is sanitized before it reaches Hindsight. Personal,
LPG, and Meridian activation must each choose and read back their own profile
identity during cutover. A bank name is an isolation boundary, not an access
control mechanism, so credentials, endpoint, and profile `HERMES_HOME` remain
separate as well.

Fixture acceptance uses two disposable profiles and a mocked Hindsight client:
each profile retains and recalls only through its derived bank, and a recall
against the other profile's bank returns no fixture records. No production bank,
credential, or Hindsight endpoint is used by these tests.
