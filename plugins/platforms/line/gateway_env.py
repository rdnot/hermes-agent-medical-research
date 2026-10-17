"""LINE env -> gateway config bridge (``PlatformEntry.env_overrides``).

Fork: ``Platform.LINE`` is a static member on this fork (``gateway/config.py``), and every static
member served by a bundled plugin must declare its env rows
(``tests/gateway/test_platform_env_overrides.py``). Mirrors the adapter's ``_env_enablement`` seed.
"""

from gateway.config import Platform
from gateway.platform_env import INT, EnvCred, home_step

ENV_ENABLE_CREDENTIALS = ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET")
ENV_OVERRIDES = (
    EnvCred(
        Platform.LINE, ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"),
        optional=(("port", "LINE_PORT", INT), ("host", "LINE_HOST"), ("public_url", "LINE_PUBLIC_URL")),
    ),
    home_step(Platform.LINE, "LINE_HOME_CHANNEL"),
)

# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "LINE_ALLOWED_USERS",
    "allow_all_env": "LINE_ALLOW_ALL_USERS",
}
