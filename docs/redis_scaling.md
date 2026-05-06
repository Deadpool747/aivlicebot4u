# Redis Shared State For Parallel Calling

## Why this was added

The original telephony flow kept pending calls and call context in process memory:

- `pending_id -> PendingCall`
- `pending_id -> call context metadata`

That works in a single-process local setup, but fails in multi-worker deployment where:

- outbound call creation may happen on instance A
- webhook/status/media callbacks may land on instance B

Without shared state, callbacks fail to resolve the call and session mapping.

## Decision summary

We introduced a pluggable call-state storage layer:

- `InMemoryCallStateStore` for local/dev
- `RedisCallStateStore` for shared, multi-worker deployments

Selection is config-driven:

- `SHARED_STATE_BACKEND=memory` -> force in-memory
- `SHARED_STATE_BACKEND=redis` -> force Redis (`REDIS_URL` required)
- `SHARED_STATE_BACKEND=auto` -> Redis when `REDIS_URL` is set, otherwise in-memory

Timing controls:

- `WEBHOOK_IDEMPOTENCY_TTL_SECONDS` controls duplicate callback dedupe window
- `CALL_STATE_TTL_SECONDS` controls pending-call liveness TTL

## What is stored in Redis

Using key prefix `REDIS_PREFIX` (default `voice_agent`):

- `<prefix>:pending_calls` (hash)  
  `pending_id -> serialized PendingCall`
- `<prefix>:call_contexts` (hash)  
  `pending_id -> serialized call context`
- `<prefix>:pending_by_provider:<provider>` (set)  
  tracks pending ids by provider
- `<prefix>:pending_by_provider_sid:<provider>` (hash)  
  `provider_call_sid -> pending_id`

This gives fast lookups for:

- `pending_id` direct access
- provider SID callback correlation
- provider-scoped pending call listing

Additional Redis keys:

- `<prefix>:pending_live:<pending_id>` (string with TTL) for stale-call cleanup
- `<prefix>:webhook_claim:<event_key>` (string with TTL) for idempotent webhook claim

## Current scope of implementation

Implemented now:

- Telephony pending call and context operations moved to the new store abstraction.
- Multi-session call handling in the app now uses per-call session keys.
- Terminal callbacks stop only the related call session key.
- Webhook callbacks now claim idempotency keys and ignore duplicates within TTL.
- Stale pending-call state is cleaned up when liveness TTL has expired.

Not yet implemented:

- distributed session execution ownership handoff (if media lands on non-owner pod)
- dead-letter handling for repeated provider failures

## Operational requirements

1. Redis reachable from all app instances
2. Same `REDIS_PREFIX` across instances for one environment
3. `SHARED_STATE_BACKEND=redis` in production where callbacks can hit any instance
4. Redis auth/TLS enabled for hosted environments

## Recommended production follow-ups

1. Add webhook idempotency table/keying (event id + provider sid + timestamp window)
2. Add TTL for stale pending calls and context keys
3. Add structured metrics:
   - unresolved callback count
   - pending call age
   - provider SID match misses
4. Add sticky routing or media-session owner routing for websocket traffic
5. Add rate limits and queueing for high CPS outbound dialing

## Files changed for this decision

- `voice_sales_agent/call_state.py`
- `voice_sales_agent/web_app.py`
- `voice_sales_agent/config.py`
- `.env.example`
- `README.md`
- `requirements.txt`
