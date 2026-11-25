"""Container and scheduler defaults shared by the CLI config data."""

DEFAULT_SANDBOX_IMAGE = "nousresearch/hermes-sandbox:desktop"
LEGACY_SANDBOX_IMAGES = (
    "nikolaik/python-nodejs:python3.11-nodejs20",
    "nikolaik/python-nodejs:python3.14-nodejs22",
)
LEGACY_SANDBOX_IMAGE = LEGACY_SANDBOX_IMAGES[0]
DEFAULT_VERCEL_IMAGE = "vercel/sandbox/universal:latest"
LEGACY_VERCEL_RUNTIME = "node24"
DEFAULT_CRON_MAX_PARALLEL_JOBS = 4
