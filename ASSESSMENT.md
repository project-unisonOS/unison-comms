# unison-comms Assessment (Capability-Manifest Era)

Date: 2025-12-18  
Scope: evaluate `unison-comms` relative to the normative UnisonOS capability system (`unison-docs/docs/platform/capabilities/*`) and the current `unison-capability` seeded manifest + registry + OAuth/secrets model, without introducing parallel security mechanisms.

## Executive decision

`unison-comms` is **partially overlapping** and **complementary**, but it requires refactoring to align with the capability resolver and UnisonOS security architecture.

- **Complementary**: it provides a domain-specific “Unified Messaging / comms” HTTP surface and a normalized message/card shape used by the Operating Surface and `unison-orchestrator`.
- **Overlapping / conflicting**: it currently embeds connector credential patterns (env vars, ad-hoc local encryption) and performs direct outbound network access (IMAP/SMTP) without going through the resolver’s policy/egress gates and without the capability manifest lifecycle.

Recommendation: keep `unison-comms` as **domain logic + UX shaping** (normalization/cards), but move/route all connector lifecycle, OAuth onboarding, secret storage, trust/policy/egress enforcement to **`unison-capability` (and ultimately `unison-security`)**. Ensure any externally reachable comms functionality is representable and invokable as a manifest capability (tool, MCP server, or skill pack).

## 1) Code inventory (what unison-comms actually does today)

### 1.1 Integrations and features

- **Email**
  - `GmailAdapter` using **IMAP + SMTP** (`imaplib.IMAP4_SSL`, `smtplib.SMTP_SSL`) with **Gmail App Passwords** via env vars:
    - `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`, optional `GMAIL_IMAP_HOST`, `GMAIL_SMTP_HOST`
  - Fetch: last ~5 unseen messages (best-effort, failures return empty).
  - Send: replies and composed emails via SMTP.
- **Unison-to-Unison (local)**
  - `UnisonAdapter` stores messages locally in a JSON file, with optional/implicit Fernet encryption key.
  - Provides `fetch_messages`, `send_reply`, `send_compose` for `channel="unison"`.
- **Meetings**
  - `/comms/join_meeting`, `/comms/prepare_meeting`, `/comms/debrief_meeting` are stubs returning cards.

### 1.2 Transport surface

- A **FastAPI** HTTP service exposing intent-ish endpoints:
  - `/comms/check`, `/comms/summarize`, `/comms/reply`, `/comms/compose`
  - plus meeting stubs and SSE stream endpoint `GET /stream/unison`
- Produces “dashboard cards” inline with results.

### 1.3 Authn/authz, secrets, policy posture

- Optional `BatonMiddleware` (from `unison-common`) is installed only if present, but **does not require** a baton by default; it rejects invalid batons if provided.
- No explicit authz tiers for read vs run vs admin.
- Provider credentials are currently expected via **environment variables**; local storage encryption is **ad-hoc** (Fernet).
- Direct outbound network (IMAP/SMTP) is not routed through the resolver’s centralized egress/policy gates.

## 2) Docs audit (claims vs reality)

In `unison-comms/README.md` and `unison-comms/docs/email-onboarding.md`, Gmail onboarding is described using **App Passwords** and optional “store encrypted locally” language. This predates the current capability system direction where:

- capability manifests **must not store secrets** (references only),
- OAuth onboarding for connectors is handled by the resolver’s admin surface,
- outbound egress must be enforced consistently (deny-by-default + allowlists).

The existing comms docs remain useful as **domain UX guidance**, but the implementation and onboarding docs are not aligned with the current platform direction.

## 3) Dependency analysis (who depends on unison-comms today)

`unison-comms` is currently a **runtime dependency** of:

- `unison-orchestrator`:
  - skills call `POST /comms/*` directly (e.g., `handler_comms_check`, `handler_comms_compose`)
  - configured via `UNISON_COMMS_HOST`/`UNISON_COMMS_PORT`
- `unison-workspace/unison-devstack/docker-compose.yml`:
  - includes a `comms` service build + healthcheck
- `unison-experience-renderer`:
  - subscribes to an EventSource at `'/comms/unison/stream'`, which currently **does not match** the `unison-comms` implementation (`GET /stream/unison`)
- `unison-docs/dev/specs/unified-messaging-protocol.md`:
  - documents `unison-comms` as the “initial HTTP surface” for comms intents

Net: `unison-comms` cannot be removed without coordinated refactors.

## 4) Overlap vs the new capability model

### 4.1 What overlaps

- **Connector onboarding and secrets**:
  - `unison-capability` now has OAuth onboarding + a secrets reference model (`secret://...`) and explicitly forbids embedding secrets in manifests.
  - `unison-comms` currently assumes env secrets and/or ad-hoc encrypted local files. This is a duplicate pattern and will diverge from policy/audit requirements.
- **Network egress control**:
  - `unison-capability` centralizes egress and per-capability allowlists.
  - `unison-comms` performs outbound email network I/O directly.

### 4.2 What is complementary

- **UMP/Comms domain layer**:
  - The normalized message shape and card generation are “Operating Surface” concerns.
  - This domain layer can be preserved as a service/library while connector execution shifts to manifest-declared capabilities.

## 5) Recommendation: target ownership boundaries

### unison-capability (core platform; owner)

Owns:
- capability discovery/resolve/install/run
- capability manifest persistence (base+local layering)
- policy enforcement + centralized egress gates
- OAuth onboarding flows and secrets handle binding
- audit events for capability lifecycle and runs

### unison-security (platform security; owner)

Owns (or will own, as it matures):
- SPIFFE/SPIRE identity + mTLS patterns (Envoy sidecars)
- OPA/ext_authz policy bundles and policy decision logging conventions
- canonical audit logging schema and redaction rules
- canonical secrets backend integrations (Vault/Secret Manager)

### unison-comms (domain layer; keep, but refactor)

Owns:
- comms domain endpoints and normalized message/card models (UMP-ish)
- adapter selection and domain-specific transforms

Delegates:
- OAuth onboarding and secret storage to resolver/security (never stores secrets in its own configs/manifests)
- outbound provider API access to capabilities (invoked through resolver), or to MCP servers that are themselves manifest-declared and policy-governed

## 6) Proposed target architecture (text diagram)

```text
Interaction Model
   |
Planner/Orchestrator (planner role)
   |
   |  (planner-contract) capability.search/resolve/install/run
   v
unison-capability (resolver)
   |-- enforces manifest + policy + egress + audit
   |-- OAuth onboarding (admin) -> secrets backend (unison-security/Vault)
   |
   +--> tool capabilities (local)
   +--> mcp_server capabilities (connectors and domain MCP servers)
   +--> a2a_peer capabilities (delegation, if needed)

Optional (UX domain service):
unison-comms (domain layer)
   |-- provides normalized cards/messages for dashboard
   |-- executes provider actions ONLY via resolver-run (no direct provider calls)
```

## 7) Backward-compatibility risks

- `unison-orchestrator` currently calls `unison-comms` directly; moving comms intents behind the resolver will require orchestrator changes.
- The renderer currently targets `'/comms/unison/stream'`; aligning the SSE path will change the renderer or require a compatibility route.
- Existing Gmail adapter relies on app passwords; replacing with OAuth device flow changes onboarding and operational expectations.

## 8) Staging strategy (safe migration)

1) **Stabilize contracts**: keep `/comms/*` responses stable while refactoring internals.
2) **Introduce capability-backed execution**: re-implement provider actions in comms by invoking `unison-capability` (or MCP servers) rather than direct IMAP/SMTP.
3) **Move onboarding to resolver**: comms no longer accepts raw secrets; it only references secret handles already provisioned by resolver admin flows.
4) **Shift planner/orchestrator**: update `unison-orchestrator` to resolve and run comms capabilities through the resolver (planner-contract compliant).
5) **Deprecate legacy paths**: once planner integration is complete, reduce `unison-comms` to optional UX/service components (or an MCP server) and remove redundant credential code.

## 9) Bottom line

`unison-comms` should **not** be deleted, but it should **stop being a standalone connector runtime**. In the capability-manifest era, it becomes either:

- a **domain-only service** that calls the resolver for any external actions, or
- an **MCP server** exposing `comms.*` tools whose execution is governed by `unison-capability`.

The connector lifecycle, OAuth/secrets, and policy/egress enforcement should be consolidated under `unison-capability` and `unison-security`.

