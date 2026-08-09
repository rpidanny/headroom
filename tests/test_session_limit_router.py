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
