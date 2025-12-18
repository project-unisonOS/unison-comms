# Integration Plan: unison-comms ↔ unison-capability ↔ unison-security

This plan proposes how to integrate `unison-comms` with the UnisonOS capability system (manifest + resolver contract) and the existing security direction in `unison-security`, while minimizing churn and avoiding parallel OAuth/secrets systems.

## Proposed end state (ownership boundaries)

- `unison-capability` (core platform) owns:
  - discovery/resolve/install/run
  - manifest persistence (base+local layering)
  - policy + egress gating + audit events
  - OAuth onboarding and binding secret handles into local manifest entries
- `unison-security` (security platform) owns:
  - SPIFFE/SPIRE identity patterns + Envoy/OPA templates
  - policy bundles + logging/audit schema + redaction rules
  - canonical secrets backends (Vault/Secret Manager) and guidance/stubs
- `unison-comms` (domain layer) owns:
  - normalized comms models + card generation (UMP-ish)
  - channel-agnostic comms tools (`comms.check`, `comms.compose`, etc.)
  - provider-specific adapters ONLY as “domain transforms”; no independent secret stores

## Planner invocation (capability-first)

All comms actions become manifest-declared capabilities invoked via the resolver:

1) Planner calls `capability.search/resolve` for `comms.*` intent.
2) Planner calls `capability.run` on a resolved comms capability.
3) Any connector use (email/calendar/chat) is itself a capability dependency:
   - connectors remain disabled by default; OAuth onboarding happens via resolver admin surfaces
   - comms tools read only secret handles (e.g., `secret://...`) and never store raw secrets

## Work items (ordered)

### Task 1 — Normalize comms surface as MCP tools (preferred transport)

- **Repos touched**
  - `unison-comms` (new MCP server entry point, reuse existing domain logic)
  - `unison-capability` (seeded manifest entry to advertise the comms MCP server)
  - `unison-docs` (update comms/UMP docs to clarify resolver mediation)
- **Implementation sketch**
  - Add `unison-comms/src/mcp_server.py` exposing MCP tools:
    - `comms.check`, `comms.summarize`, `comms.reply`, `comms.compose`
    - `comms.join_meeting`, `comms.prepare_meeting`, `comms.debrief_meeting` (optional)
  - Keep the existing HTTP API temporarily (for current orchestrator + renderer), but treat MCP as the long-term contract.
- **Acceptance criteria**
  - MCP server can be started locally and lists the above tools with JSON schemas.
  - Tools return the same normalized message + cards shape as the current HTTP endpoints.
- **Tests required**
  - MCP tool invocation smoke test (in-process) for `comms.check` and `comms.compose`.
- **Docs required**
  - Add a short `unison-comms/docs/mcp.md` describing how to run the MCP server and how it maps to the intent surface.

### Task 2 — Remove parallel secret storage from unison-comms (delegate secrets)

- **Repos touched**
  - `unison-comms`
  - (optional) `unison-security` if a minimal shared secrets client is introduced
- **Implementation sketch**
  - Deprecate env-only “provider secrets” in favor of secret handles passed at runtime (or resolved from local profile).
  - Remove/disable ad-hoc encrypted local stores for provider credentials.
  - Keep local persistence only for non-secret state (e.g., cached message IDs), and make it explicitly scoped.
- **Acceptance criteria**
  - `unison-comms` does not accept/store raw OAuth refresh tokens or app passwords in its persistent storage.
  - Logs contain no secret material; redaction tests cover common token patterns.
- **Tests required**
  - “no secrets in logs” regression test.
- **Docs required**
  - Update `unison-comms/docs/email-onboarding.md` to route onboarding through `unison-capability` OAuth flows and secret handles (remove app-password guidance for production).

### Task 3 — Replace direct IMAP/SMTP provider access with resolver-mediated connector capabilities

- **Repos touched**
  - `unison-comms`
  - `unison-capability`
- **Implementation sketch**
  - Treat provider access as connector capabilities (`connector.email.*`, `connector.calendar.*`, etc.).
  - `unison-comms` becomes a thin layer:
    - performs normalization and UX shaping
    - calls connector capabilities via `capability.run` (or via MCP tools declared in the manifest)
  - Keep deny-by-default egress at the resolver; comms itself should not initiate arbitrary outbound calls.
- **Acceptance criteria**
  - A comms action that requires external access (e.g., `comms.check` for Gmail) fails closed unless:
    - connector capability is enabled in `manifest.local.json`
    - OAuth onboarding completed and secret handle bound
    - network allowlist allows required domains
- **Tests required**
  - Integration-style test with a mocked connector capability returning sample messages.

### Task 4 — Align security posture with unison-security (no open listeners by default)

- **Repos touched**
  - `unison-comms`
  - `unison-workspace/unison-devstack` (compose wiring)
  - `unison-security` (docs/templates only, if needed)
- **Implementation sketch**
  - Default `unison-comms` to bind to loopback or unix socket; production deployments should front with Envoy/OPA (SPIFFE mTLS + ext_authz) per `unison-security/docs/SERVICE_IDENTITY.md`.
  - If `BatonMiddleware` remains in use, require scopes for write/send endpoints and keep “disable auth” test flag restricted to test mode.
- **Acceptance criteria**
  - Comms service is not reachable on a public interface by default.
  - Privileged comms actions require authn/authz (consistent with org conventions).
- **Tests required**
  - Auth-required test for a privileged action (compose/reply) when running without Envoy.

### Task 5 — Move orchestrator off direct comms calls (planner-contract compliance)

- **Repos touched**
  - `unison-orchestrator`
  - `unison-capability`
  - `unison-docs`
- **Implementation sketch**
  - Update orchestrator’s comms skills so they do:
    - `capability.search/resolve` for the comms intent
    - `capability.run` for the selected capability
  - Preserve existing skill names (`comms.check`, etc.) for backward compatibility at the intent layer.
- **Acceptance criteria**
  - No direct HTTP calls from orchestrator to `/comms/*` are required for comms execution.
  - Resolver audit events show `run_start/run_success` for comms capabilities.
- **Tests required**
  - Orchestrator unit test: comms skill issues resolver calls rather than `ServiceHttpClient.post("/comms/...")`.

### Task 6 — Fix renderer SSE route mismatch (compatibility shim)

- **Repos touched**
  - `unison-comms` and/or `unison-experience-renderer`
- **Implementation sketch**
  - Either:
    - add an alias route in comms: `GET /comms/unison/stream` -> existing SSE, or
    - update renderer to use `GET /stream/unison`
- **Acceptance criteria**
  - Renderer receives Unison channel events in devstack without manual rewiring.
- **Tests required**
  - Minimal SSE route test (status code + event shape).

## Acceptance demo (end-to-end)

1) Seeded baseline resolves a comms intent:
   - `capability.search(intent="comms.check")` returns a local comms capability immediately (seeded).
2) Running without onboarding:
   - `capability.run("comms.check", {"channel":"email"})` returns only local/stub messages and does not attempt external egress.
3) OAuth onboarding enables a connector:
   - admin starts OAuth for a connector capability (e.g., Gmail) via resolver admin endpoint
   - resolver stores refresh token in secrets backend and writes only `secret://...` refs into `manifest.local.json`
4) After enablement:
   - `capability.run("comms.check", ...)` invokes connector capability under resolver egress gates and emits audit events.

## Notes / constraints

- No new OAuth or secrets storage system should be introduced in `unison-comms`.
- Any comms provider access must be representable as manifest capabilities (tool/MCP server/skill pack) and executed via the resolver to ensure policy, trust, and audit consistency.

