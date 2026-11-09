"""Development server settings. Recovery tools require an explicit opt-in."""

import os

from .development import *  # noqa: F403

LOCAL_RECOVERY_ENABLED = os.environ.get("LOCAL_RECOVERY_ENABLED") == "1"
