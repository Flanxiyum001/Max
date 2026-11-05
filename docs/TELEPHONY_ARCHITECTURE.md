# MAX telephony and communications architecture

**Status:** Future interface contract only. No phone provider or number is provisioned; no live call, WhatsApp or email functionality is claimed.

## Scope and priorities

Desired: MAX calls the owner, receives authorized owner calls, resumes task context across voice/web/calls, announces completed important tasks, respects quiet hours and offers user-controlled transcript retention. Messaging integrations use **official** WhatsApp Business and authorized email providers, with their respective account approvals. Do not equate an authenticated phone call with broad account authorization.

**Preferred sequence:** first build authenticated, foreground, in-app voice over existing HTTPS/web session; then research actual telephony number feasibility, call routing, regional law and pricing; then add a PSTN adapter if and only if viable.

## Regional feasibility gate — India

Primary-source findings as of 2026-11-05:
- [Twilio India Voice Guidelines](https://www.twilio.com/en-us/guidelines/in/voice) list **domestic inbound/outbound as N/A** for Twilio's published India route and describe international-to-India outbound as subject to non-Indian number and carrier restrictions.
- [Twilio verified +91 caller-ID FAQ](https://help.twilio.com/articles/57566259695003) states +91 caller-ID origination is blocked for outbound Twilio calls.
- These facts **do not prove no alternative Indian carrier exists**, but **disqualify any casual assumption** that a Twilio number can provide MAX's desired in-country domestic two-way service.
- Review applicable DoT/TRAI rules, consent, telemarketer restrictions, recording notices, user-location and number allocation obligations with the eventual provider/legal counsel. Do not interpret this document as legal clearance.

### Provider acceptance worksheet (no provider selected)

| Test | Required evidence |
| --- | --- |
| Outbound India-to-India and international | Actual provider supported routes; verified caller ID and source geography; sandbox call |
| Inbound reachability from owner's network | Number rental/ownership eligibility, local/foreign DID reachability, carrier test |
| Webhook and media stream | TLS, signed request verification, reconnect behavior, latency and DTMF |
| Identity | Caller number alone insufficient; signed app challenge, voice session pairing or one-time app approval |
| Recording/transcripts | Configurable consent, on/off, retention/deletion and location of processing |
| Costs | Number monthly fee, inbound/outbound per minute, stream, recording, taxes, API calls; measured estimate |
| Compliance | Local rules, service restrictions, consent, rate limits, approved uses |
| Operational controls | Provider outages, webhook retries, idempotency, fraud limits, max call duration, spend cap |

## Trust-boundary design

`Owner phone/PSTN -> provider -> signed webhook ingress -> MAX Call Controller -> Auth challenge -> scoped session -> MAX Core / Trust`.

- A call arriving from a remembered number remains **untrusted until session authentication**; caller ID can be spoofed/forwarded.
- Webhook signature and timestamp are checked against the provider's exact documentation. Reject replayed message IDs, invalid signatures, unexpected destination IDs, and stale time windows.
- Inbound audio and transcription are untrusted speech content; do not accept spoken payment authorization or vault release as sufficient proof of identity.
- The Call Controller binds a verified `call_id`, `task_id`, `owner_session_id`, `consent_state`, `channel_type`, `language`, `provider_event_id`, `retention_class`. Never place long-lived app secrets in media prompts.
- Each call/session has configurable expiry, quiet-hours policy, concurrency/cost caps and explicit hang-up. Playback announcements carry a brief AI-identity notice where appropriate.

## Call lifecycle

1. Owner enables calling and registers approved destinations using an authenticated private UI (future phase).
2. Outbound call intent enters MAX Trust. High-consequence communications, recipient changes or unusual destinations require fresh approval.
3. Controller asks adapter to create call with idempotency key, stores pending state, and audits decision.
4. Signed webhook reports ringing/answered/failed/completed; verify it is an authorized event for the intended session.
5. Inbound caller is challenged through an authenticated app session or a secure paired flow before task access; SMS/voice OTP must not be treated as a generic bypass.
6. Media adapter handles STT/TTS/interruptions with enforced mute/quiet hours. MAX Core receives **scoped** transcript content and never raw provider authority.
7. Completion logs verified provider status; recording/transcript processing follows retention choice. Retry reconciliation only for provider-confirmed-safe operations.

## State machine and interface

`created -> dialing/ringing -> authenticated -> connected -> ended`, with explicit `rejected`, `failed`, `timed_out` and `disputed` terminal states. Unknown or conflicting webhook events go to reconciliation, not 'success'.

Minimal normalized port:
- `place_call(destination_id, owner_approval_id, purpose, idempotency_key)`
- `handle_signed_provider_event(event, source)`
- `authenticate_inbound(call_id, approved_session)`
- `stream_media(call_id, codec, direction, cancellation)`
- `end_call(call_id, reason)`
- `get_authoritative_status(call_id)`

## Task-context continuity

The phone adapter receives an opaque task reference, not an unrestricted copy of all memory. Model-generated spoken summaries are privacy-filtered. On hang-up, owner may resume via authenticated web/desktop channel with checkpoint ID. A call must never change model gateway privacy classification or tool scope.

## Notifications, email and WhatsApp

- Default urgent alerts to the existing authenticated app, not unsolicited telephony; throttle and deduplicate.
- WhatsApp: use approved official Meta Business APIs and account provisioning; enforce opt-in and template/session restrictions based on current official documentation. Do not automate a personal WhatsApp client or bypass rate limits.
- Email: OAuth/API with minimum scopes; independently verify destination before sharing attachments, show a recipient/content preview and log delivery status.
- All channels: inbound content is untrusted; signatures/tokens checked at ingress, not by LLM; outbound high-risk sharing requires trusted confirmation.

## Security acceptance tests (future; none executed)

- Spoofed caller ID cannot retrieve memory or authorize tools.
- Bad/old/replayed webhook signature denied.
- Out-of-order duplicate events do not duplicate calls, alerts or actions.
- Unknown recipient, missing owner approval, exhausted budget and quiet hours deny outgoing calls.
- Call disconnect cancels streaming and does not abandon/duplicate durable task.
- Transcripts off means no retained content; deletion and export are verifiable.
- Provider outage produces an honest failed/degraded state and preserves task history.
- India route/number feasibility demonstrated with lawful test account before feature is declared available.
