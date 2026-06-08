# Piopiy Live Verification Checklist

## Status

- App deployed on Lightsail
- Piopiy provider enabled
- Inbound and outbound Piopiy routes wired
- Server env updated with Piopiy credentials

## Dashboard URLs to configure

Set these in the Piopiy dashboard:

- Answer URL: `https://aivoicebot4u.com/piopiy/answer`
- Live Event URL: `https://aivoicebot4u.com/piopiy/events`
- CDR URL: `https://aivoicebot4u.com/piopiy/cdr`

## Expected behavior

### 1. Inbound call

When a caller dials your Piopiy number:

- Piopiy sends a webhook to `/piopiy/answer`
- The app creates an inbound pending call session
- The app returns a `stream` action with a websocket URL
- Piopiy connects to `/piopiy/stream/{pending_id}`
- Live stream audio enters the voice agent session

### 2. Live events

Piopiy POSTs call status updates to `/piopiy/events`.

Expected statuses include:

- `stream_connected`
- `in_answered`
- `out_started`
- `out_answered`
- `in_hangup`
- `out_hangup`
- `stream_disconnected`
- `stream_error`

### 3. CDR

Piopiy POSTs completed call detail records to `/piopiy/cdr`.

The app stores the last event/CDR payload in call context for later inspection.

## What to test first

1. Make one inbound test call to the Piopiy number.
2. Confirm the call appears in the app dashboard.
3. Check the results tab for captured call data.
4. Make one outbound call from the UI using provider `piopiy`.
5. Confirm the call status becomes answered and the transcript/session updates.

## If something fails

- If `/piopiy/answer` is not hit, the Piopiy dashboard answer URL is wrong.
- If the stream does not connect, verify the Piopiy streaming option is enabled.
- If call records do not appear, check the CDR URL and confirm the webhook request reaches the app.

