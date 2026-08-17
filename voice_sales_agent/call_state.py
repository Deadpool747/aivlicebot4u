"""Shared call-state store used by telephony orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - fallback when redis dependency is absent
    Redis = None  # type: ignore[assignment]


def _safe_json_loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


@dataclass(slots=True)
class InMemoryCallStateStore:
    """Simple in-process store used for local development."""

    _pending_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    _contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    _call_deadlines: dict[str, float] = field(default_factory=dict)
    _webhook_claims: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    call_state_ttl_seconds: int = 7200

    def _deadline(self) -> float:
        return time.time() + max(60, self.call_state_ttl_seconds)

    def _cleanup_expired_unlocked(self) -> None:
        now = time.time()
        for event_key in list(self._webhook_claims.keys()):
            if self._webhook_claims[event_key] <= now:
                self._webhook_claims.pop(event_key, None)
        for pending_id in list(self._call_deadlines.keys()):
            if self._call_deadlines[pending_id] <= now:
                self._call_deadlines.pop(pending_id, None)
                self._pending_calls.pop(pending_id, None)
                self._contexts.pop(pending_id, None)

    async def put_call(self, pending_id: str, pending_payload: dict[str, Any], context: dict[str, Any]) -> None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            self._pending_calls[pending_id] = dict(pending_payload)
            self._contexts[pending_id] = dict(context)
            self._call_deadlines[pending_id] = self._deadline()

    async def get_call(self, pending_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            payload = self._pending_calls.get(pending_id)
            if payload is not None:
                self._call_deadlines[pending_id] = self._deadline()
            return dict(payload) if payload is not None else None

    async def pop_call(self, pending_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            payload = self._pending_calls.pop(pending_id, None)
            self._contexts.pop(pending_id, None)
            self._call_deadlines.pop(pending_id, None)
            return dict(payload) if payload is not None else None

    async def find_call_by_provider_sid(self, provider: str, provider_sid: str) -> tuple[str, dict[str, Any]] | None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            for pending_id, payload in self._pending_calls.items():
                if str(payload.get("provider", "")) != provider:
                    continue
                if str(payload.get("provider_call_sid", "")) == provider_sid:
                    self._call_deadlines[pending_id] = self._deadline()
                    return pending_id, dict(payload)
                aliases = payload.get("provider_call_sids")
                if isinstance(aliases, list) and provider_sid in {str(item or "").strip() for item in aliases}:
                    self._call_deadlines[pending_id] = self._deadline()
                    return pending_id, dict(payload)
        return None

    async def list_pending_ids(self, provider: str | None = None) -> list[str]:
        async with self._lock:
            self._cleanup_expired_unlocked()
            if provider is None:
                return list(self._pending_calls.keys())
            return [
                pending_id
                for pending_id, payload in self._pending_calls.items()
                if str(payload.get("provider", "")) == provider
            ]

    async def get_context(self, pending_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            payload = self._contexts.get(pending_id)
            if payload is not None:
                self._call_deadlines[pending_id] = self._deadline()
            return dict(payload) if payload is not None else None

    async def set_context(self, pending_id: str, context: dict[str, Any]) -> None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            self._contexts[pending_id] = dict(context)
            self._call_deadlines[pending_id] = self._deadline()

    async def set_call(self, pending_id: str, call_payload: dict[str, Any]) -> None:
        async with self._lock:
            self._cleanup_expired_unlocked()
            self._pending_calls[pending_id] = dict(call_payload)
            self._call_deadlines[pending_id] = self._deadline()

    async def claim_webhook_event(self, event_key: str, ttl_seconds: int) -> bool:
        async with self._lock:
            self._cleanup_expired_unlocked()
            if event_key in self._webhook_claims:
                return False
            self._webhook_claims[event_key] = time.time() + max(30, ttl_seconds)
            return True


@dataclass(slots=True)
class RedisCallStateStore:
    """Redis-backed shared call-state store for multi-worker deployments."""

    redis_url: str
    prefix: str = "voice_agent"
    call_state_ttl_seconds: int = 7200

    def __post_init__(self) -> None:
        if Redis is None:
            raise RuntimeError("redis package is not installed. Add 'redis>=5.0.0' to requirements.")
        self._redis = Redis.from_url(self.redis_url, decode_responses=True)

    def _key_pending_hash(self) -> str:
        return f"{self.prefix}:pending_calls"

    def _key_context_hash(self) -> str:
        return f"{self.prefix}:call_contexts"

    def _key_pending_by_provider(self, provider: str) -> str:
        return f"{self.prefix}:pending_by_provider:{provider}"

    def _key_pending_sid_index(self, provider: str) -> str:
        return f"{self.prefix}:pending_by_provider_sid:{provider}"

    def _key_pending_liveness(self, pending_id: str) -> str:
        return f"{self.prefix}:pending_live:{pending_id}"

    def _key_webhook_claim(self, event_key: str) -> str:
        return f"{self.prefix}:webhook_claim:{event_key}"

    async def _touch_pending_liveness(self, pending_id: str) -> None:
        await self._redis.set(
            self._key_pending_liveness(pending_id),
            "1",
            ex=max(60, self.call_state_ttl_seconds),
        )

    async def _delete_pending_id(self, pending_id: str, payload: dict[str, Any] | None = None) -> None:
        resolved_payload = payload or await self.get_call(pending_id)
        provider = str((resolved_payload or {}).get("provider", "")).strip()
        provider_sid = str((resolved_payload or {}).get("provider_call_sid", "")).strip()
        provider_sids = resolved_payload.get("provider_call_sids") if isinstance(resolved_payload, dict) else []
        if not isinstance(provider_sids, list):
            provider_sids = []
        pipe = self._redis.pipeline()
        pipe.hdel(self._key_pending_hash(), pending_id)
        pipe.hdel(self._key_context_hash(), pending_id)
        pipe.delete(self._key_pending_liveness(pending_id))
        if provider:
            pipe.srem(self._key_pending_by_provider(provider), pending_id)
            if provider_sid:
                pipe.hdel(self._key_pending_sid_index(provider), provider_sid)
            for alias in provider_sids:
                alias_value = str(alias or "").strip()
                if alias_value:
                    pipe.hdel(self._key_pending_sid_index(provider), alias_value)
        await pipe.execute()

    async def put_call(self, pending_id: str, pending_payload: dict[str, Any], context: dict[str, Any]) -> None:
        provider = str(pending_payload.get("provider", "")).strip()
        provider_sid = str(pending_payload.get("provider_call_sid", "")).strip()
        provider_sids = pending_payload.get("provider_call_sids")
        if not isinstance(provider_sids, list):
            provider_sids = [provider_sid] if provider_sid else []
        pending_json = json.dumps(pending_payload, ensure_ascii=True)
        context_json = json.dumps(context, ensure_ascii=True)
        pipe = self._redis.pipeline()
        pipe.hset(self._key_pending_hash(), pending_id, pending_json)
        pipe.hset(self._key_context_hash(), pending_id, context_json)
        if provider:
            pipe.sadd(self._key_pending_by_provider(provider), pending_id)
            if provider_sid:
                pipe.hset(self._key_pending_sid_index(provider), provider_sid, pending_id)
            for alias in provider_sids:
                alias_value = str(alias or "").strip()
                if alias_value:
                    pipe.hset(self._key_pending_sid_index(provider), alias_value, pending_id)
        await pipe.execute()
        await self._touch_pending_liveness(pending_id)

    async def get_call(self, pending_id: str) -> dict[str, Any] | None:
        if not await self._redis.exists(self._key_pending_liveness(pending_id)):
            await self._delete_pending_id(pending_id)
            return None
        raw = await self._redis.hget(self._key_pending_hash(), pending_id)
        payload = _safe_json_loads(raw)
        if payload is None:
            return None
        await self._touch_pending_liveness(pending_id)
        return payload

    async def pop_call(self, pending_id: str) -> dict[str, Any] | None:
        payload = await self.get_call(pending_id)
        if payload is None:
            return None
        await self._delete_pending_id(pending_id, payload=payload)
        return payload

    async def find_call_by_provider_sid(self, provider: str, provider_sid: str) -> tuple[str, dict[str, Any]] | None:
        pending_id = await self._redis.hget(self._key_pending_sid_index(provider), provider_sid)
        if not pending_id:
            candidates = [str(item) for item in await self._redis.smembers(self._key_pending_by_provider(provider))]
            for candidate_pending_id in candidates:
                if not await self._redis.exists(self._key_pending_liveness(candidate_pending_id)):
                    await self._delete_pending_id(candidate_pending_id)
                    continue
                payload = await self.get_call(candidate_pending_id)
                if payload is None:
                    continue
                aliases = payload.get("provider_call_sids")
                if isinstance(aliases, list) and provider_sid in {str(item or "").strip() for item in aliases}:
                    return candidate_pending_id, payload
                if str(payload.get("provider_call_sid", "")).strip() == provider_sid:
                    return candidate_pending_id, payload
            return None
        if not await self._redis.exists(self._key_pending_liveness(pending_id)):
            await self._redis.hdel(self._key_pending_sid_index(provider), provider_sid)
            await self._delete_pending_id(pending_id)
            return None
        payload = await self.get_call(pending_id)
        if payload is None:
            await self._redis.hdel(self._key_pending_sid_index(provider), provider_sid)
            return None
        return pending_id, payload

    async def list_pending_ids(self, provider: str | None = None) -> list[str]:
        if provider is None:
            candidates = [str(item) for item in await self._redis.hkeys(self._key_pending_hash())]
        else:
            candidates = [str(item) for item in await self._redis.smembers(self._key_pending_by_provider(provider))]

        if not candidates:
            return []
        pipe = self._redis.pipeline()
        for pending_id in candidates:
            pipe.exists(self._key_pending_liveness(pending_id))
        liveness = await pipe.execute()
        alive: list[str] = []
        stale: list[str] = []
        for pending_id, exists in zip(candidates, liveness):
            if exists:
                alive.append(pending_id)
            else:
                stale.append(pending_id)
        for pending_id in stale:
            await self._delete_pending_id(pending_id)
        return alive

    async def get_context(self, pending_id: str) -> dict[str, Any] | None:
        if not await self._redis.exists(self._key_pending_liveness(pending_id)):
            await self._delete_pending_id(pending_id)
            return None
        raw = await self._redis.hget(self._key_context_hash(), pending_id)
        payload = _safe_json_loads(raw)
        if payload is None:
            return None
        await self._touch_pending_liveness(pending_id)
        return payload

    async def set_context(self, pending_id: str, context: dict[str, Any]) -> None:
        await self._redis.hset(
            self._key_context_hash(),
            pending_id,
            json.dumps(context, ensure_ascii=True),
        )
        await self._touch_pending_liveness(pending_id)

    async def set_call(self, pending_id: str, call_payload: dict[str, Any]) -> None:
        provider = str(call_payload.get("provider", "")).strip()
        provider_sid = str(call_payload.get("provider_call_sid", "")).strip()
        provider_sids = call_payload.get("provider_call_sids")
        if not isinstance(provider_sids, list):
            provider_sids = [provider_sid] if provider_sid else []
        existing = await self.get_call(pending_id)
        old_sid = str((existing or {}).get("provider_call_sid", "")).strip()
        old_sids = existing.get("provider_call_sids") if isinstance(existing, dict) else []
        if not isinstance(old_sids, list):
            old_sids = []
        pipe = self._redis.pipeline()
        pipe.hset(
            self._key_pending_hash(),
            pending_id,
            json.dumps(call_payload, ensure_ascii=True),
        )
        if provider:
            pipe.sadd(self._key_pending_by_provider(provider), pending_id)
            if old_sid and old_sid != provider_sid:
                pipe.hdel(self._key_pending_sid_index(provider), old_sid)
            for alias in old_sids:
                alias_value = str(alias or "").strip()
                if alias_value and alias_value != provider_sid:
                    pipe.hdel(self._key_pending_sid_index(provider), alias_value)
            if provider_sid:
                pipe.hset(self._key_pending_sid_index(provider), provider_sid, pending_id)
            for alias in provider_sids:
                alias_value = str(alias or "").strip()
                if alias_value:
                    pipe.hset(self._key_pending_sid_index(provider), alias_value, pending_id)
        await pipe.execute()
        await self._touch_pending_liveness(pending_id)

    async def claim_webhook_event(self, event_key: str, ttl_seconds: int) -> bool:
        result = await self._redis.set(
            self._key_webhook_claim(event_key),
            "1",
            nx=True,
            ex=max(30, ttl_seconds),
        )
        return bool(result)
