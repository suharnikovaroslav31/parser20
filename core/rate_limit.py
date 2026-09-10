"""
Соблюдение rate-limit: asyncio.Semaphore + экспоненциальный backoff.

Модуль не «обходит» лимиты Telegram / TON RPC — он останавливается,
ждёт FloodWait и повторяет запрос с нарастающей паузой.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

LOGGER = logging.getLogger("tg_gifts.http")
T = TypeVar("T")


class RateLimitedError(RuntimeError):
    """HTTP 429 / 5xx после исчерпания ретраев."""


class AsyncRateLimiter:
    """Ограничивает параллелизм и добавляет джиттер между вызовами."""

    def __init__(self, concurrency: int, min_interval: float = 0.05) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_ts = 0.0

    async def slot(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            wait_for = self._next_ts - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            jitter = random.uniform(0.0, self._min_interval)
            self._next_ts = loop.time() + self._min_interval + jitter

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            await self.slot()
            return await factory()


def compute_backoff(attempt: int, *, base: float = 0.8, cap: float = 45.0) -> float:
    """Классический exponential backoff с full jitter (AWS architecture blog)."""
    ceiling = min(cap, base * (2 ** max(0, attempt)))
    return random.uniform(0.0, ceiling)


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    retries: int,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(retries):
        try:
            return await factory()
        except retry_on as exc:
            last_error = exc
            if attempt >= retries - 1:
                break
            delay = compute_backoff(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


class HttpClient:
    """Общий aiohttp-клиент с ретраями на 429/5xx и сетевые сбои."""

    def __init__(
        self,
        *,
        timeout_sec: float,
        max_retries: int,
        concurrency: int,
        user_agent: str = "TG-Gifts-Analytics/1.0 (research; +https://t.me)",
    ) -> None:
        self._timeout = ClientTimeout(total=timeout_sec, connect=min(10.0, timeout_sec))
        self._max_retries = max_retries
        self._limiter = AsyncRateLimiter(concurrency=concurrency, min_interval=0.04)
        self._user_agent = user_agent
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                raise_for_status=False,
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("HttpClient не запущен. Вызовите await start().")
        return self._session

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int | float] | None = None,
        json_body: object | None = None,
        accept_text: bool = False,
    ) -> object:
        """GET/POST JSON. 429 и 5xx ретраятся; 404 возвращает None."""

        async def _once() -> object:
            async def _call() -> object:
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                ) as response:
                    if response.status == 404:
                        return None
                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else compute_backoff(3)
                        LOGGER.warning("HTTP 429 %s — пауза %.1fs", url, delay)
                        await asyncio.sleep(delay)
                        raise RateLimitedError(f"429 {url}")
                    if response.status >= 500:
                        body = await response.text()
                        raise ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=body[:300],
                            headers=response.headers,
                        )
                    if response.status >= 400:
                        body = await response.text()
                        LOGGER.warning("HTTP %s %s: %s", response.status, url, body[:200])
                        return None
                    if accept_text:
                        return await response.text()
                    if response.content_type and "json" not in response.content_type:
                        return await response.text()
                    return await response.json(content_type=None)

            return await self._limiter.run(_call)

        def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            LOGGER.warning(
                "HTTP retry %s %s attempt=%s delay=%.2fs error=%s",
                method,
                url,
                attempt + 1,
                delay,
                exc,
            )

        return await retry_async(
            _once,
            retries=self._max_retries,
            retry_on=(ClientError, RateLimitedError, asyncio.TimeoutError, OSError),
            on_retry=_on_retry,
        )
