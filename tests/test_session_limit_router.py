"""Tests for the session-limit-based OpenRouter fallback router."""

from types import SimpleNamespace
from unittest.mock import patch

from headroom.proxy.session_limit_router import SessionLimitRouter
from headroom.subscription.models import RateLimitWindow, SubscriptionSnapshot


def _config(**overrides):
    defaults = {
        "session_limit_fallback_enabled": True,
        "session_limit_fallback_threshold": 0.95,
        "session_limit_fallback_model_map": None,
        "session_limit_fallback_default_model": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _tracker_with_snapshot(five_hour_pct: float = 0.0, seven_day_pct: float = 0.0):
    snapshot = SubscriptionSnapshot(
        five_hour=RateLimitWindow(utilization_pct=five_hour_pct),
        seven_day=RateLimitWindow(utilization_pct=seven_day_pct),
    )
    return SimpleNamespace(latest_snapshot=snapshot)


class TestIsFallbackActive:
    def test_inactive_when_disabled(self):
        router = SessionLimitRouter(
            config=_config(session_limit_fallback_enabled=False),
            subscription_tracker=_tracker_with_snapshot(99.0, 99.0),
        )
        assert router.is_fallback_active is False

    def test_inactive_when_no_tracker(self):
        router = SessionLimitRouter(config=_config(), subscription_tracker=None)
        assert router.is_fallback_active is False

    def test_inactive_when_no_snapshot(self):
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=SimpleNamespace(latest_snapshot=None)
        )
        assert router.is_fallback_active is False

    def test_inactive_below_threshold(self):
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(50.0, 50.0)
        )
        assert router.is_fallback_active is False

    def test_active_when_five_hour_at_threshold(self):
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(95.0, 0.0)
        )
        assert router.is_fallback_active is True

    def test_active_when_seven_day_over_threshold(self):
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(0.0, 99.0)
        )
        assert router.is_fallback_active is True


class TestMapModel:
    def test_explicit_map_wins(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"claude-x": "deepseek/deepseek-chat-v4"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-x") == "deepseek/deepseek-chat-v4"

    def test_default_model_used_when_unmapped(self):
        router = SessionLimitRouter(
            config=_config(session_limit_fallback_default_model="openai/gpt-4o"),
            subscription_tracker=None,
        )
        assert router.map_model("claude-x") == "openai/gpt-4o"

    def test_auto_prefix_fallback(self):
        router = SessionLimitRouter(config=_config(), subscription_tracker=None)
        assert router.map_model("claude-x") == "anthropic/claude-x"

    # -- family-keyed matching --

    def test_family_match_sonnet_5_with_1m_marker(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"sonnet-5": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-5[1m]") == "openai/gpt-4o"

    def test_family_match_dated_sonnet_4_5(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"sonnet-4-5": "deepseek/deepseek-chat-v4"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-4-5-20250929") == "deepseek/deepseek-chat-v4"

    def test_family_match_opus_4_8(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"opus-4-8": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-opus-4-8") == "openai/gpt-4o"

    def test_family_match_fable_5(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"fable-5": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-fable-5") == "openai/gpt-4o"

    def test_family_match_haiku_4_5_dated(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"haiku-4-5": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-haiku-4-5-20251001") == "openai/gpt-4o"

    def test_exact_id_wins_over_family(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={
                    "sonnet-4-5": "openai/gpt-4o",
                    "claude-sonnet-4-5-20250929": "deepseek/deepseek-chat-v4",
                }
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-4-5-20250929") == "deepseek/deepseek-chat-v4"

    def test_family_not_in_map_falls_through_to_default(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"sonnet-5": "openai/gpt-4o"},
                session_limit_fallback_default_model="deepseek/deepseek-chat-v4",
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-opus-4-8") == "deepseek/deepseek-chat-v4"

    def test_family_not_in_map_falls_through_to_prefix(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"sonnet-5": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-opus-4-8") == "anthropic/claude-opus-4-8"

    # -- tolerant key matching --

    def test_family_key_case_insensitive(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"Sonnet-5": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-5[1m]") == "openai/gpt-4o"

    def test_family_key_whitespace_tolerant(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={" sonnet-5 ": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-5[1m]") == "openai/gpt-4o"

    def test_exact_id_case_insensitive(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={
                    "Claude-Sonnet-4-5-20250929": "deepseek/deepseek-chat-v4"
                }
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-4-5-20250929") == "deepseek/deepseek-chat-v4"

    # -- undated claude- pins act as family keys --

    def test_undated_claude_pin_is_a_family_key(self):
        """claude-sonnet-5 (no date) must catch dated/aliased sonnet-5 variants."""
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={
                    "claude-sonnet-5": "openai/gpt-4o"
                }
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-5") == "openai/gpt-4o"
        assert router.map_model("claude-sonnet-5[1m]") == "openai/gpt-4o"
        assert router.map_model("claude-sonnet-5-20260101") == "openai/gpt-4o"

    def test_dated_claude_id_is_exact_only(self):
        """A dated ID maps only the exact version, not its family."""
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={
                    "claude-sonnet-4-5-20250929": "deepseek/deepseek-chat-v4"
                }
            ),
            subscription_tracker=None,
        )
        # Exact match works…
        assert router.map_model("claude-sonnet-4-5-20250929") == "deepseek/deepseek-chat-v4"
        # …but the family does NOT route via the dated entry.
        assert router.map_model("claude-sonnet-4-5-20251101") == "anthropic/claude-sonnet-4-5-20251101"

    def test_undated_claude_and_slug_both_family_keys(self):
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={
                    "claude-opus-4-8": "z-ai/glm-5.2"
                }
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-opus-4-8") == "z-ai/glm-5.2"
        assert router.map_model("claude-opus-4-8[1m]") == "z-ai/glm-5.2"

    def test_arbitrary_key_is_not_a_family_key(self):
        """Non-Claude custom keys must not act as catch-all family mappings."""
        router = SessionLimitRouter(
            config=_config(
                session_limit_fallback_model_map={"gpt-4o": "openai/gpt-4o"}
            ),
            subscription_tracker=None,
        )
        assert router.map_model("claude-sonnet-5") == "anthropic/claude-sonnet-5"


class TestFamilyExtraction:
    """Unit-test the family helpers directly."""

    def test_known_families(self):
        from headroom.proxy.session_limit_router import _anthropic_model_family as fam

        assert fam("claude-sonnet-5") == "sonnet-5"
        assert fam("claude-sonnet-5[1m]") == "sonnet-5"
        assert fam("claude-sonnet-4-6") == "sonnet-4-6"
        assert fam("claude-sonnet-4-5") == "sonnet-4-5"
        assert fam("claude-sonnet-4-5-20250929") == "sonnet-4-5"
        assert fam("claude-opus-4-8") == "opus-4-8"
        assert fam("claude-opus-4-5-20251101") == "opus-4-5"
        assert fam("claude-fable-5") == "fable-5"
        assert fam("claude-haiku-4-5-20251001") == "haiku-4-5"

    def test_non_claude_returns_none(self):
        from headroom.proxy.session_limit_router import _anthropic_model_family as fam

        assert fam("gpt-4o") is None

    def test_family_lookup_key_shapes(self):
        from headroom.proxy.session_limit_router import _family_lookup_key as lk

        # Bare slugs are family keys.
        assert lk("sonnet-5") == "sonnet-5"
        assert lk("SONNET-5") == "sonnet-5"
        # Undated claude- pins are family keys, normalized to the slug.
        assert lk("claude-sonnet-5") == "sonnet-5"
        assert lk("Claude-Opus-4-8") == "opus-4-8"
        # Dated IDs are exact-only (not family keys).
        assert lk("claude-sonnet-4-5-20250929") is None
        # [1m]/dated claude- variants are exact-only.
        assert lk("claude-sonnet-5[1m]") is None
        # Arbitrary keys are not family keys.
        assert lk("gpt-4o") is None
        assert lk("deepseek/deepseek-chat-v4") is None


class TestGetBackendCredentialGuard:
    def test_no_backend_without_openrouter_api_key(self, monkeypatch):
        """Missing OPENROUTER_API_KEY must stay on direct Anthropic, not build

        a backend that only fails once a real request hits OpenRouter.
        """
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(99.0, 0.0)
        )

        backend = router.get_backend("claude-x", existing_backend=None)

        assert backend is None

    def test_backend_built_when_api_key_present(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(99.0, 0.0)
        )

        with patch("headroom.backends.litellm.LiteLLMBackend") as mock_backend_cls:
            mock_backend_cls.return_value = SimpleNamespace(name="litellm-openrouter")
            backend = router.get_backend("claude-x", existing_backend=None)

        assert backend is not None
        mock_backend_cls.assert_called_once_with(provider="openrouter")

    def test_existing_backend_always_wins(self):
        router = SessionLimitRouter(
            config=_config(), subscription_tracker=_tracker_with_snapshot(99.0, 0.0)
        )
        sentinel = object()
        assert router.get_backend("claude-x", existing_backend=sentinel) is sentinel

    def test_default_backend_returned_when_not_active(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        default_backend = object()
        router = SessionLimitRouter(
            config=_config(),
            subscription_tracker=_tracker_with_snapshot(0.0, 0.0),
            default_backend=default_backend,
        )
        assert router.get_backend("claude-x", existing_backend=None) is default_backend
