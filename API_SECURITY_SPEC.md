# HELT Customer API — Access & Security Specification

**Status: v0.2 — APPROVED 2026-07-31.** The five §10 questions are answered
(decisions recorded in §10). One design change from v0.1: the **Hosted UI
login page is deferred** — there is a single customer for now, who receives
credentials in a document and fetches tokens by script (§2). S1–S2
implementation lives in this repo.

Companion to `HANDOFF.md` §6 (API-first decision, settled). This document
specifies how the currently wide-open sandbox API becomes the real
customer-facing API: who can call it, what they can see, and how abuse is
contained.

---

## 1. Scope and goals

**In scope:** the read API (API Gateway + query Lambda + everything a browser
or customer backend touches): authentication, per-pack and per-field
authorization, rate limiting, CORS, secret handling, audit.

**Out of scope (unchanged by this spec):** the MQTT ingest path (already
mTLS + CN-scoped IoT policy in production), the cloud→pack command path (no
real commands exist yet — revisit when they do), OTA, fleet provisioning.

**Goals, in priority order:**
1. No anonymous access to any telemetry.
2. A user sees only their packs, and only the fields they're entitled to.
3. One authorization code path for both humans (dashboard) and machines
   (business backends) — per HANDOFF §6.
4. Contain cost/abuse: the AWS account currently has a **10 concurrent Lambda
   executions** cap (HANDOFF §5.6), and InfluxDB reads are the expensive part.
5. Zero standing per-customer infrastructure — onboarding stays a data-plane
   operation (a row + a credential).

---

## 2. Authentication — Amazon Cognito User Pool

**Decision: one Cognito User Pool** (`helt-users`, us-east-1), email as the
sign-in identifier. Self-signup **disabled**: users are created by us
(admin-create), because a pack entitlement has to exist anyway before an
account is useful.

- **Now (single-customer phase): headless users, no login page.** Every
  credential — the customer's and our internal dashboard account — is a
  normal pool user. The customer receives a document (see
  `API_ACCESS.md`) containing their email + password + a ~10-line snippet
  that exchanges those for a 1 h access token via Cognito's `InitiateAuth`
  API (`USER_PASSWORD_AUTH` flow, public app client, no secret). A 24/7
  poller re-fetches a token once an hour — one extra HTTPS call. This *is*
  the machine flow: no separate credential type, so the one-authz-path rule
  holds by construction.
- **Deferred: Hosted UI login page.** When there are multiple human users, add
  the Cognito Hosted UI (OAuth2 code + PKCE — zero password handling,
  MFA/forgot-password free, CSS-brandable). Nothing migrates: the same user
  accounts, same `sub`, same entitlement rows serve both flows.
- **Rejected: dedicated M2M app client** (client-credentials): same effort for
  the customer, but AWS bills M2M app clients (~$6/mo) and it creates a
  second identity disconnected from any future dashboard login. Standalone
  API keys (REST-API usage plans) also rejected; see §6 and Appendix A.
- **Tokens:** access token, 1 h expiry; refresh token 30 d. Clients send
  `Authorization: Bearer <access_token>`. Never in query strings.
  Logout = Cognito global sign-out (revokes refresh; access tokens die ≤1 h).
- **Internal dashboard:** minimal in-page prompt (email + password →
  `InitiateAuth` from the browser, ~40 lines, no redirect), signed in as the
  internal ops user which is entitled to all packs. Tokens live in
  sessionStorage, auto-refreshed while the tab is open. This is a gate, not
  the future login UX.

**Why Cognito:** managed + free at this scale (free tier ≈ 10k MAU), and API
Gateway **HTTP APIs have a native JWT authorizer** — issuer/audience/signature/
expiry are all validated *at the gateway*, before our Lambda runs and before
anything is billed beyond the authorizer check. No custom auth code at the gate.

---

## 3. Authorization — the entitlements store

**DynamoDB table `helt_entitlements`** (on-demand billing, single-digit-ms,
pennies/month at this scale):

| Attribute | Type | Meaning |
|---|---|---|
| `user_id` (PK) | S | Cognito `sub` claim (stable UUID; survives email changes) |
| `pack_id` (SK) | S | e.g. `HELT-0001` |
| `field_groups` | SS | any of `core`, `health`, `location`, `ops` — or `ALL` |
| `granted_by`, `granted_at` | S | audit trail |

One item = "this user may see this pack, at this field granularity."
`Query(user_id)` returns everything a user can see in one read.

**Field groups** (proposed — open question #1): entitlements name *groups*,
not raw fields, so adding a telemetry field later doesn't require touching
every entitlement row:

| Group | Fields today |
|---|---|
| `core` | `soc_pct`, `power_w`, `pack_voltage_v`, `current_a`, `inv_output_w`, `dc_input_w` |
| `health` | `soh_pct`, `cycle_count`, `max_cell_temp_c`, `enclosure_temp_c`, `enclosure_humidity_pct`, `bms_protections` |
| `location` | `lat`, `lon` (gates `/track` entirely) |
| `ops` | `si_state`, `bms_state`, `seq`, `ts_synced`, status payload fields |

The group→field mapping is a constant in the query Lambda (versioned with the
code, same place the dependency chain already lives).

**Enforcement — all inside the query Lambda** (the gateway only proves *who*;
the Lambda decides *what*):

1. JWT already validated by the gateway; Lambda reads `sub` from
   `event.requestContext.authorizer.jwt.claims` — never from the body/URL.
2. Load entitlements for `sub` (container-local cache, 60 s TTL — at 3 s
   dashboard polling this cuts DynamoDB reads ~20×; 60 s staleness on a grant
   is acceptable).
3. `GET /packs` → return only entitled packs (joined with live online status).
4. `GET /packs/{id}/latest` → 403 if no item for that pack; otherwise return
   the **intersection** of stored fields with entitled groups.
5. `GET /packs/{id}/history?field=X` → 403 unless X is in an entitled group
   (this also tightens the current regex-only field validation: the field must
   additionally be a *known* field in the group map).
6. `GET /packs/{id}/track` → requires `location`.
7. Decisions are logged (§7).

**401 vs 403:** missing/expired/invalid token → 401 (gateway emits this);
valid token but no entitlement → 403 with `{"error":"forbidden"}` — the
response never reveals whether the pack exists.

**Admin surface (phase S2):** a `grant.py` CLI (wraps `put-item` /
`delete-item` / `query`) run by us. An admin web page is explicitly later; the
table schema doesn't change when it arrives.

---

## 4. Request flow

```
Client (customer script / dashboard prompt)
   ──email+password──▶ Cognito InitiateAuth ──▶ tokens (1h access / 30d refresh)
   │
   ▼  Authorization: Bearer <access_token>
API Gateway (HTTP API)
   ├─ JWT authorizer: signature / issuer / audience / expiry   → 401 on failure
   ├─ CORS: exact origins only (§6)
   └─ stage throttle (§5)
   ▼
query Lambda
   ├─ sub ← authorizer claims
   ├─ entitlements ← DynamoDB (60 s container cache)
   ├─ pack check → 403 · field/group filter → intersection or 403
   ├─ Flux query (unchanged machinery, InfluxDB token stays server-side)
   └─ audit log line (structured JSON → CloudWatch)
```

---

## 5. Rate limiting and cost containment

**Measured (2026-07-27, 5-pack fleet):** the pipeline itself costs ~$10.7/mo;
the dominant *variable* cost is **InfluxDB query executions ($0.012/100)** —
the original dashboard fired ~61 queries/min (~$0.44/hour per open tab, ~97%
of a 24/7 bill). Mitigations, applied to the sandbox and carried forward as
requirements on the production API:

1. **Response cache in the query Lambda** (30 s TTL, per container): N
   concurrent viewers share one InfluxDB query per window. This must survive
   the S2 authorization work — cache keys include the pack + range but sit
   *behind* the entitlement check (cache the Influx read, filter per user).
2. **One bundled query per refresh** (`/histories`): all fields + GPS trail
   in a single Flux query instead of 10.
3. **`/latest` drops its status sub-query** unless `?status=1` — liveness
   derives from telemetry freshness.
4. **Client cadence = data cadence:** the dashboard polls every 30 s (matching
   the firmware's 30 s batching); faster polling only re-reads the cache.
5. **InfluxDB retention policy** (not yet applied): cap `helt_sandbox` at
   e.g. 90 d so storage stops accumulating (~+0.35 GB compressed/month today).

Net effect: ~5 InfluxDB queries/min per active pack view (~$0.036/hour,
~12× cheaper), and additional simultaneous viewers are nearly free.

- **Stage throttle now (HTTP API supports this):** rate 10 req/s, burst 20 —
  sized to the 10-concurrency Lambda cap. Per-route override: `history` and
  `track` (heavier Flux) throttled tighter, ~5 req/s.
- **Per-user quotas:** HTTP APIs have no native per-caller usage plans. Not
  needed at sandbox scale. When needed (phase S3+), the options are, in
  preference order: (a) a DynamoDB token-bucket inside the query Lambda (we
  already read DynamoDB per request; one more attribute on the entitlement
  row), (b) AWS WAF rate-based rules (~$8/mo), (c) migrate to REST API for
  usage plans (loses the native JWT authorizer — see Appendix A).
- **Lambda concurrency limit increase:** request via Service Quotas before any
  real fleet (it's a support ticket, free, but takes days — don't do it last).
- The dashboard already self-limits (4-wide fetch pool, downsampled ranges).

---

## 6. Transport, CORS, and API surface

- **CORS lockdown** (currently `*` — closing this is part of phase S1):
  `Access-Control-Allow-Origin` = exactly the dashboard origins (the
  github.io origin + `http://localhost:8000` for dev), methods `GET,OPTIONS`,
  headers `Authorization,Content-Type`, max-age 3600.
- Explicit routes replace the `$default` catch-all, each with the JWT
  authorizer attached: `GET /packs`, `GET /packs/{pack_id}/latest`,
  `GET /packs/{pack_id}/history`, `GET /packs/{pack_id}/track`. Anything else
  404s at the gateway without invoking Lambda.
- TLS is API Gateway default; custom domain (`api.helt.co.za`) + CloudFront
  is future polish, not security-relevant now.
- Error shape everywhere: `{"error": "<machine_code>", "detail": "<human>"}`.

---

## 7. Secrets, logging, audit

- InfluxDB tokens stay in Lambda env vars for the sandbox; production moves
  them to **SSM Parameter Store SecureString** (free tier, KMS-encrypted)
  read at cold start. Secrets Manager not justified ($0.40/secret/mo, no
  rotation requirement for InfluxDB tokens yet).
- Query Lambda logs **structured JSON audit lines**:
  `{ts, user_id, route, pack_id, field, decision: allow|deny, latency_ms}` —
  never tokens, never claim payloads beyond `sub`. CloudWatch retention 90 d.
- Cognito advanced security (adaptive auth, compromised-credential checks) is
  available but paid — off for sandbox, revisit for production.

---

## 8. Threat checklist (what this design answers)

| Threat | Answer |
|---|---|
| Anonymous scraping of telemetry | JWT authorizer at the gateway |
| User A reads user B's pack | entitlement keyed by Cognito `sub`, checked server-side |
| Renter sees pack GPS they shouldn't | `location` group excluded from their grant |
| Token theft | 1 h expiry, no tokens in URLs, global sign-out, HTTPS only |
| Cost abuse / DoS | stage throttle, tight routes, Lambda cap awareness, WAF later |
| Flux injection | existing regex **plus** field-must-be-known-group-member check |
| Enumerating pack IDs | `/packs` lists only entitlements; 403 is existence-neutral |
| Lambda/API misconfig drift | explicit routes only; authorizer required on every route |

Residual/accepted for sandbox: no MFA enforcement, no WAF, Influx tokens in
env vars, sandbox IoT policy still permissive (ingest side, separate task).

---

## 9. Rollout phases (each is sandbox-runnable, each extends setup.sh/teardown.sh)

- **S1 — Authentication + CORS:** create user pool + public app client
  (`USER_PASSWORD_AUTH`) + test users (internal ops + two fake customers);
  attach JWT authorizer to explicit routes; lock CORS; dashboard gets the
  minimal in-page prompt, sessionStorage tokens + hourly refresh, and a
  logged-out state. *Demo: unauthenticated curl → 401; token → data.*
- **S2 — Authorization:** `helt_entitlements` table + `grant.py`; Lambda
  enforcement (pack check, group filter, `/packs` filtering); audit lines.
  *Demo: user A sees SANDBOX-01..03; user B sees 04..05 without `location` —
  B's map hides, B's `/track` 403s.*
- **S3 — Hardening:** stage/route throttles, concurrency-increase request,
  SSM for tokens, optional machine-user flow for a fake "business customer".
- **Cost:** ~$0/month at sandbox scale (Cognito free tier, DynamoDB on-demand
  cents, no WAF yet).

---

## 10. Decisions (answered 2026-07-31)

1. **Field groups** — the four groups `core / health / location / ops` as
   specced. Per-field grants rejected (admin fiddliness, and every new
   telemetry field would touch entitlement rows).
2. **Login UX** — **deferred entirely.** Single customer for now; they get
   credentials in a document and fetch tokens by script (§2). The internal
   dashboard gets a minimal in-page prompt — a gate, not the product login.
   Hosted UI vs embedded form gets decided when multiple human users exist.
3. **Machine access** — the headless-user flow shipped in S1 *is* machine
   access; a dedicated M2M app client is rejected for now (AWS M2M billing,
   ~$6/mo, and a second identity). Revisit only if a customer needs OAuth
   client-credentials semantics specifically.
4. **Admin** — `grant.py` CLI confirmed; admin web page explicitly later.
5. **User creation** — admin-create only confirmed; self-signup stays off.

---

## Appendix A — alternatives considered and rejected

- **REST API + API keys/usage plans:** native per-customer quotas, but no
  native JWT authorizer (needs a custom Lambda authorizer = auth code we must
  own), keys are long-lived bearer secrets, and it's a second credential
  system violating the one-authz-path rule. Rejected; token-bucket in DynamoDB
  covers quotas when needed.
- **Cognito Identity Pools + IAM auth:** signs requests with SigV4, elegant
  for AWS-native clients, hostile for third-party backends and browser code.
- **Per-user InfluxDB tokens:** InfluxDB tokens are bucket-scoped only — they
  cannot express per-pack or per-field access (HANDOFF §6 already settled
  this); the Lambda stays the only reader.
- **Auth0/Clerk/etc.:** capable, but another vendor bill and data-residency
  surface for something Cognito's free tier covers.
