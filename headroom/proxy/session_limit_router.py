"""Session-aware OpenRouter fallback when Anthropic subscription limits are approached.

Monitors the subscription tracker's 5-hour and 7-day window utilization.
When either crosses the configured threshold, subsequent requests are routed
through OpenRouter instead of direct Anthropic to avoid on-demand surcharges.

Activation (all required):
  1. ``ProxyConfig.session_limit_fallback_enabled`` must be True
  2. At least one rate-limit window must be at or above the threshold
  3. The subscription tracker must have a valid snapshot

Integration is via ``SessionLimitRouter.get_backend()`` — call it as close to
upstream dispatch as possible after the existing ``RouteAdvice`` check so it
only overrides the direct-Anthropic path and never interferes with an
extension-driven routing decision.

Usage:
    router = SessionLimitRouter(
        config=proxy_config,
        subscription_tracker=sub_tracker,
        default_backend=None,
    )

    backend = router.get_backend(model, request_backend)
    if backend is not None:
        # Route through OpenRouter (or another backend)
    else:
        # Direct Anthropic API
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_THRESHOLD = 0.95
_DEFAULT_MODEL_MAP: dict[str, str] = {}


class SessionLimitRouter:
    """Routes requests to OpenRouter when the Anthropic session limit is hit.

    This is a per-proxy-instance object that reads the subscription tracker's
    latest snapshot on every request. When the five-hour or seven-day window
    utilization reaches the threshold, it lazily builds an OpenRouter
    LiteLLM backend and maps the model to the OpenRouter naming convention.

    Args:
        config: The proxy configuration object.
        subscription_tracker: Reference to the global subscription tracker
            singleton (may be None if tracking is disabled).
        default_backend: The native ``anthropic_backend`` (or None for
            direct API). Returned when fallback is not active.
    """

    def __init__(
        self,
        config: Any,
        subscription_tracker: Any = None,
        default_backend: Any = None,
    ) -> None:
        self._enabled: bool = getattr(config, "session_limit_fallback_enabled", False)
        self._threshold: float = getattr(
            config, "session_limit_fallback_threshold", _DEFAULT_THRESHOLD
        )
        self._subscription_tracker = subscription_tracker
        self._default_backend = default_backend

        self._model_map: dict[str, str] = dict(
            getattr(config, "session_limit_fallback_model_map", None) or _DEFAULT_MODEL_MAP
        )
        self._default_model: str | None = getattr(
            config, "session_limit_fallback_default_model", None
        ) or None

        self._backend: Any = None
        self._backend_failed: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_fallback_active(self) -> bool:
        """Whether the session limit threshold has been exceeded.

        Returns False when tracking is disabled, no snapshot is available,
        or all windows are below the configured threshold.
        """
        if not self._enabled:
            return False

        tracker = self._subscription_tracker
        if tracker is None:
            return False

        snapshot = tracker.latest_snapshot
        if snapshot is None:
            logger.debug("SessionLimitRouter: no snapshot available yet, fallback not active")
            return False

        threshold_pct = self._threshold * 100.0
        five_hour = snapshot.five_hour.utilization_pct
        seven_day = snapshot.seven_day.utilization_pct

        active = five_hour >= threshold_pct or seven_day >= threshold_pct
        if active:
            logger.info(
                "SessionLimitRouter: fallback ACTIVE "
                "(5h=%.1f%%, 7d=%.1f%%, threshold=%.0f%%)",
                five_hour,
                seven_day,
                threshold_pct,
            )
        else:
            logger.debug(
                "SessionLimitRouter: fallback not active "
                "(5h=%.1f%%, 7d=%.1f%%, threshold=%.0f%%)",
                five_hour,
                seven_day,
                threshold_pct,
            )
        return active

    def get_backend(
        self,
        model: str,
        existing_backend: Any = None,
    ) -> Any | None:
        """Return the backend to use for this request.

        Args:
            model: The Anthropic model ID from the request body.
            existing_backend: The backend chosen by ``RouteAdvice`` (or
                ``None`` for direct Anthropic). If a route-advice backend
                is already set, it wins — we only override the direct path.

        Returns:
            The OpenRouter backend when fallback is active and no other
            backend is set; ``existing_backend`` otherwise.
        """
        if existing_backend is not None:
            return existing_backend

        if not self.is_fallback_active:
            return self._default_backend

        backend = self._get_or_create_openrouter_backend()
        if backend is None:
            return self._default_backend

        return backend

    def map_model(self, anthropic_model: str) -> str:
        """Translate an Anthropic model ID to the OpenRouter format.

        Precedence:
        1. Explicit mapping from ``session_limit_fallback_model_map``
        2. ``session_limit_fallback_default_model`` (routes ALL unmatched models)
        3. ``anthropic/<model>`` prefix (OpenRouter's Anthropic convention)

        Args:
            anthropic_model: The Anthropic-native model ID (e.g.
                ``claude-sonnet-4-5-20250929``).

        Returns:
            The OpenRouter-qualified model ID (e.g.
            ``deepseek/deepseek-chat-v4`` or
            ``anthropic/claude-sonnet-4-5-20250929``).
        """
        if anthropic_model in self._model_map:
            return self._model_map[anthropic_model]
        if self._default_model:
            return self._default_model
        return f"anthropic/{anthropic_model}"

    async def start(self) -> None:
        """No-op lifecycle hook. Kept for symmetry with other proxy subsystems."""

    async def stop(self) -> None:
        """Clean up the lazily-built backend."""
        backend = self._backend
        self._backend = None
        if backend is not None and hasattr(backend, "close"):
            try:
                await backend.close()  # type: ignore[union-attr]
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create_openrouter_backend(self) -> Any | None:
        if self._backend_failed:
            return None
        if self._backend is not None:
            return self._backend

        # Fail closed (stay on direct Anthropic) when there's no key to
        # authenticate with, rather than building a backend that will only
        # discover the missing credential once a real request is dispatched
        # to OpenRouter — by then the only recourse is a 500 to the client.
        if not os.environ.get("OPENROUTER_API_KEY"):
            logger.warning(
                "SessionLimitRouter: OPENROUTER_API_KEY is not set; "
                "cannot activate OpenRouter fallback"
            )
            self._backend_failed = True
            return None

        try:
            from headroom.backends.litellm import LiteLLMBackend

            self._backend = LiteLLMBackend(provider="openrouter")
            logger.info("SessionLimitRouter: OpenRouter backend built for fallback")
        except Exception as exc:
            logger.warning("SessionLimitRouter: failed to build OpenRouter backend (%s)", exc)
            self._backend_failed = True
            return None

        return self._backend
