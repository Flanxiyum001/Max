# M14 — reviewed memory/gateway seam (first safe slice)

**Review branch only; NOT a deployed assistant or autonomous MCP loop.**

The new opt-in MemoryGatewayLoop connects the existing reviewed MemoryStore to
Gateway.complete. It pulls at most four owner+scope approved, non-expired facts,
serializes them as explicitly **untrusted JSON data** before conversation turns,
and revalidates the original GatewayRequest's input ceiling. Because M12 has
no per-record privacy or egress labeling, attaching memories forces
local_only=True and cloud_allowed=False. If no verified-local route is
eligible, the Gateway rejects execution. The caller authenticates owner/scope.

After a successfully completed invocation, one strict "remember KEY: VALUE"
**authenticated original user note** can become a PENDING candidate. It is never
approved automatically, and no tool proposal is dispatched. The denyword
filter is defense in depth, not PII/secret classification: the trusted host must
classify the original turn as PUBLIC or INTERNAL and reject sensitive content
*before* passing trusted_owner_note. No model-output extraction is enabled.

There is currently no MAX Core API endpoint, local provider connection, memory
UI, passive background extraction, automatic retention scheduling, or remote
memory synchronization. The bridge remains intentionally opt-in and is covered
by offline unit tests. Memory text must not appear in logs, audit rows or
upstream clouds. Prompt injection inside memory JSON remains a model-level risk;
trust policies must never depend on model compliance with the warning.

## Further integration gates

- Authenticated owner/session/device layer and user-visible memory review API.
- Independent sensitive-data classification and record-specific egress label.
- Context-budget relevance ranking and source revocation.
- MCP tool policy with pinned observed schema, confirmed endpoint identity,
  single-use hardware-backed owner approval, worker isolation and safe retries.
- pgvector schema/embedding compatibility and backup/restore validation.
- Staging tests before enabling any external side effects or production access.
