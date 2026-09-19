# request-update

Registers the parent-only `request_update` tool. It runs the native read-only update check,
refuses when no update is available or another request is pending, and starts the same detached
`hermes update --gateway` process used by the gateway slash command.
