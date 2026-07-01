from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Request routing pipeline:

        1. CACHE CHECK — if self.cache is not None:
           - Call self.cache.get(prompt) → (cached_text, score)
           - If cached_text is not None, return GatewayResponse with:
             route=f"cache_hit:{score:.2f}", cache_hit=True, latency=0, cost=0

        2. PROVIDER FALLBACK CHAIN — iterate self.providers in order:
           - Get the circuit breaker: self.breakers[provider.name]
           - Try breaker.call(provider.complete, prompt)
           - On success:
             a. Store in cache: self.cache.set(prompt, response.text, {"provider": provider.name})
             b. Determine route: "primary" if first provider, else "fallback"
             c. Return GatewayResponse with provider info, latency, cost
           - On ProviderError or CircuitOpenError: save error, continue to next provider

        3. STATIC FALLBACK — if all providers fail:
           - Return GatewayResponse with:
             text="The service is temporarily degraded. Please try again soon."
             route="static_fallback", error=last_error

        A production extension could add budget-aware routing to cheaper providers.
        """
        started_at = time.perf_counter()
        if self.cache is not None:
            try:
                cached_text, score = self.cache.get(prompt)
            except Exception as exc:
                logger.warning("Cache lookup failed; continuing without cache: %s", exc)
                cached_text, score = None, 0.0
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        last_error: str | None = None
        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

            if self.cache is not None:
                try:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                except Exception as exc:
                    logger.warning("Cache write failed; provider response remains usable: %s", exc)

            return GatewayResponse(
                text=response.text,
                route="primary" if index == 0 else "fallback",
                provider=response.provider,
                cache_hit=False,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                estimated_cost=response.estimated_cost,
            )

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=(time.perf_counter() - started_at) * 1000,
            estimated_cost=0.0,
            error=last_error,
        )
