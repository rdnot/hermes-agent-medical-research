"""Fork: LINE's chat-scoped group allowlist env survives upstream's PlatformEntry move.

Upstream replaced ``gateway/authz_mixin._GROUP_CHAT_ENV`` with
``PlatformEntry.group_allowed_chats_env`` (read through ``_group_chat_env``). ``Platform.LINE`` is a
static member only on this fork, so its ``LINE_ALLOWED_GROUPS`` row now lives in
``plugins/platforms/line/gateway_env.py`` ``AUTHZ`` (passed to ``register_platform(**AUTHZ)``).
"""

from gateway.authz_mixin import _group_chat_env
from gateway.config import Platform
from gateway.platform_env import bundled_authz


def test_line_gateway_env_declares_group_allowlist_env():
    assert bundled_authz("line").get("group_allowed_chats_env") == "LINE_ALLOWED_GROUPS"


def test_authz_resolves_line_group_allowlist_env():
    assert _group_chat_env(Platform.LINE) == "LINE_ALLOWED_GROUPS"


def test_line_group_allowlist_does_not_suppress_dm_pairing():
    # Upstream semantics: a group allowlist alone must not silence DM pairing unless the platform
    # opts in. LINE never did (it was never in the old _ALLOWLIST_SUPPRESSES_PAIRING set).
    assert not bundled_authz("line").get("allowlist_suppresses_pairing")
