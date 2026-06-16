"""
rate_limit — GPT-OSS API 호출 레이트 제한(전역 토큰버킷)

배경:
  GPT-OSS 엔드포인트는 5회/sec 의 호출 cap 이 있다. 안전하게 4회/sec(기본)로 제한한다.
  gpt-researcher 는 하위질의를 asyncio.gather 로 동시 호출하므로, 동기/비동기 양쪽에서
  공유되는 프로세스-전역 리미터가 필요하다.

설계:
  - "최소 간격 스페이싱" 방식(엄격): 연속 두 호출 사이 최소 1/rps 초를 보장한다.
    버스트를 허용하지 않으므로 실측 호출률이 절대 rps 를 넘지 않는다.
  - 상태(_next_time)는 threading.Lock 으로 보호 → 스레드/이벤트루프 혼재에도 안전.
  - 락 안에서는 "대기 시간 계산 + 슬롯 예약"만 하고, 실제 sleep 은 락 밖에서 수행
    (동기=time.sleep, 비동기=asyncio.sleep → 이벤트루프 비차단).

사용:
  from rate_limit import get_limiter
  lim = get_limiter()              # env LLM_MAX_RPS (기본 4) 1회 반영
  lim.acquire()                    # 동기 경로
  await lim.acquire_async()        # 비동기 경로

env:
  LLM_MAX_RPS   초당 최대 호출 수 (기본 4). 0 이하면 제한 비활성.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time


class RateLimiter:
    """최소-간격 스페이싱 리미터(프로세스 전역 단일 인스턴스 권장)."""

    def __init__(self, rps: float):
        self.rps = float(rps)
        self.enabled = self.rps > 0
        self.min_interval = (1.0 / self.rps) if self.enabled else 0.0
        self._lock = threading.Lock()
        self._next_time = 0.0  # 다음 호출이 나갈 수 있는 가장 이른 monotonic 시각

    def _reserve(self) -> float:
        """슬롯을 예약하고, 그때까지 기다려야 하는 초를 반환(락으로 보호)."""
        if not self.enabled:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = now if now >= self._next_time else self._next_time
            self._next_time = start + self.min_interval
            return start - now if start > now else 0.0

    def acquire(self) -> None:
        wait = self._reserve()
        if wait > 0:
            time.sleep(wait)

    async def acquire_async(self) -> None:
        wait = self._reserve()
        if wait > 0:
            await asyncio.sleep(wait)


_LIMITER: RateLimiter | None = None
_INIT_LOCK = threading.Lock()


def _read_rps() -> float:
    raw = os.getenv("LLM_MAX_RPS", "4")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 4.0


def get_limiter() -> RateLimiter:
    """프로세스 전역 단일 리미터. env LLM_MAX_RPS 는 최초 1회만 반영."""
    global _LIMITER
    if _LIMITER is None:
        with _INIT_LOCK:
            if _LIMITER is None:
                _LIMITER = RateLimiter(_read_rps())
    return _LIMITER


def reset_for_test(rps: float) -> RateLimiter:
    """테스트 전용: 리미터를 명시 rps 로 재생성."""
    global _LIMITER
    with _INIT_LOCK:
        _LIMITER = RateLimiter(rps)
    return _LIMITER
