---
name: openviking-design-doc-maintainer
description: Maintain the OpenViking architecture document set under docs/design, especially admin-session-performance. Use when creating, reviewing, reorganizing, synchronizing, or changing the lifecycle status of requirements, analysis, design, test-plan, implementation-plan, migration-runbook, roadmap, or ADR documents; when checking those documents against current code; or when preparing them as an approved implementation or production-operations baseline.
---

# OpenViking Design Doc Maintainer

Maintain design documents as an evidence-backed system rather than independent Markdown files. Keep requirements, design, tests, implementation slices, migration operations, and the production roadmap consistent with current code and fixed tenant decisions.

## Authoritative paths

Start with:

- `docs/design/admin-session-performance/README.md`
- `docs/design/admin-session-performance/01-analysis.md`
- `docs/design/admin-session-performance/02-requirements.md`
- `docs/design/admin-session-performance/03-design.md`
- `docs/design/admin-session-performance/04-test-plan.md`
- `docs/design/admin-session-performance/05-implementation-plan.md`
- `docs/design/admin-session-performance/06-migration-runbook.md`
- `docs/design/production-readiness-roadmap.md`

Read only the documents relevant to the requested change, but always read the document-set README and inspect current code before asserting current behavior.

## Fixed domain decisions

Preserve these decisions unless the user explicitly changes them:

- One hotel tenant maps to `account_id`.
- Front-desk and hotel technical operators use tenant subaccounts identified by `user_id`.
- Existing `ROOT / ADMIN / USER` role and permission semantics remain unchanged.
- `account_id` is the isolation key for knowledge, resources, sessions, memories, feedback, statistics, caches, events, jobs, and query projections.
- Do not introduce Organization, Property, or RoleBinding models.
- Session files are the current fact source; Admin query storage is a rebuildable projection.
- `AdminSessionQuery` is the stable deep-module interface.
- SQLite is for local development, contract tests, and controlled validation. PostgreSQL is the production target for roughly 8000 hotel tenants.
- Historical rebuild is read-only by default. `.meta.json` repair/write-back is a separate explicit operation.
- Rollout is account-scoped: `off -> shadow -> prefer -> required`.

If a request conflicts with a fixed decision, identify the conflict before editing and require an explicit replacement decision.

## Workflow

### 1. Classify the request

Classify it as one or more of:

- code-fact refresh;
- requirement or scope change;
- architecture/interface change;
- test or acceptance change;
- implementation sequencing change;
- migration/runbook change;
- document status transition;
- reorganization or link maintenance.

Do not broaden a narrow update into a redesign.

### 2. Establish current truth

Inspect the live worktree and relevant implementation before changing factual claims. Prefer these paths for Admin session work:

- `openviking/server/routers/admin.py`
- `openviking/service/session_service.py`
- `openviking/session/session.py`
- `openviking/storage/viking_fs.py`
- `openviking/server/identity.py`
- `openviking/service/core.py`
- `admin/src/pages/Dashboard.tsx`
- `admin/src/pages/Sessions.tsx`
- `tests/server/test_admin_api.py`
- `tests/server/test_api_sessions.py`

Use `rg` to find current symbols and line locations. Treat links and line references as evidence, not decoration. Do not preserve stale claims merely because they already exist in a document.

### 3. Compute the synchronization set

Update every affected document, and only those documents:

| Change | Minimum synchronization set |
| --- | --- |
| Current behavior or bottleneck | analysis; design review evidence if conclusions change |
| Business decision, scope, compatibility, NFR | requirements; design; test plan |
| Interface, schema, state machine, failure semantics | design; test plan; implementation plan; migration runbook when operationally relevant |
| Acceptance or performance gate | requirements; test plan; implementation phase exit gate; roadmap if production readiness changes |
| Implementation order or code location | implementation plan; design when the seam changes |
| Rebuild, repair, deletion, restore, rollout, rollback | requirements; design; test plan; migration runbook |
| Platform production target | roadmap; affected slice documents |
| File move or rename | README/navigation; every local link; roadmap entrypoint |

Reject terminology drift. Use `AdminSessionQuery` for the module/interface, query projection for derived architecture data, and index only as the operational short name for concrete storage, tables, state, CLI, or database indexes.

### 4. Preserve layered scope

Keep three claims distinct:

1. The current MVP behavior.
2. The Admin query production-hardening slice.
3. The final platform production architecture.

Do not call Phase 0-3 platform production-ready. The final target also requires stateless API replicas, shared durable content storage, PostgreSQL control/query planes, durable outbox/workers, recovery drills, security, observability, and operational ownership.

### 5. Manage document status

Use only these statuses:

- `Draft`: still changing; not an implementation or production baseline.
- `In Review`: actively under formal review.
- `Approved`: approved as an implementation baseline.
- `Implementing`: approved plan currently being implemented.
- `Validated`: claims verified by tests, benchmark, or rehearsal.
- `Active`: current production or operational authority.
- `Completed`: implementation plan finished with required evidence.
- `Superseded`: replaced; link to the successor.
- `Archived`: retained only for history.

Never promote status automatically. Require evidence appropriate to the document:

- approval or review record for `Approved`;
- linked implementation and passing tests for `Validated`;
- benchmark for performance gates;
- staging rehearsal for migration runbooks;
- production rollout and owner confirmation for `Active`;
- successor link for `Superseded`.

When evidence is unavailable, keep the existing status and report what is missing.

### 6. Validate

Run the bundled validator from the repository root:

```bash
python3 .agents/skills/openviking-design-doc-maintainer/scripts/validate_docs.py
```

Also run targeted `rg` consistency searches for renamed concepts and old paths. Run application tests only when code changed or when the user explicitly requests live verification. For documentation-only edits, report that application tests were not run.

### 7. Report

Report:

- which documents changed and why;
- fixed decisions or interfaces affected;
- status changes and their evidence, or why status stayed unchanged;
- validation performed;
- any remaining decision, baseline, implementation, or rehearsal gap.

Do not claim approval, implementation, validation, or production readiness from prose completion alone.

## ADR rule

Create `docs/design/admin-session-performance/adr/` only when recording the first major, hard-to-reverse decision. Use an ADR for replacements to fixed domain decisions, fact-source changes, interface splits, database technology changes, consistency-model changes, or irreversible migration choices. Do not create ADRs for routine wording, link, or implementation-detail edits.

## Safety

- Preserve unrelated dirty-worktree changes.
- Never rewrite historical facts to make the target architecture appear already implemented.
- Never make rebuild imply `.meta.json` write-back.
- Never omit `account_id` from storage, cache, event, job, checkpoint, or query isolation discussions.
- Never treat SQLite as the 8000-tenant HA production target.
- Never change document status without explicit evidence.
