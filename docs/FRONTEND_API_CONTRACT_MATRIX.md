# Frontend API Contract Matrix

Source of truth: the locally running backend's `GET /openapi.json` on 2026-08-11.  The submission UI is a presentation layer only: it never manufactures a topology, intent, policy, KPI, agent decision, execution result, or current-intent state.

| Page / component | Authoritative API | Method | Request / response used | Lifecycle dependency | Access | Integrated | Regression |
|---|---|---|---|---|---|---|---|
| Dashboard overview / chat workspace | `/api/v1/chat`, `/api/v1/intents/{id}/events` | POST, GET/SSE | text/session -> assistant reply, `intent_id`, stage events | PENDING through terminal | mutation only when backend detects a formal intent | yes | 16 |
| Intent Studio Preview | `/api/v1/intents/preview` | POST | text/priority -> semantic and admission preview | PREVIEW | read-only; no Policy/Binding/ONOS/P4/TC | yes | 5, 15 |
| Formal Submit | `/api/v1/intents` and chat-created formal intent contract | POST | text/priority/session -> durable `INT-*` | PENDING/PLANNING/RUNNING | mutation | yes | 6, 16 |
| Clarification | `/api/v1/intents/{id}/clarifications` | POST | continuation text -> resumed lifecycle | CLARIFICATION_REQUIRED | mutation | yes | 7 |
| Intent detail | `/api/v1/intents/{id}/details` | GET | aggregate record/spec/runtime/plan/execution/assurance/history/decisions/evidence | all states | read-only | yes | 10 |
| Workflow / Agent run | `/api/v1/intents/{id}/events`, `/a2a-trace`, `/details` | SSE, GET | real stage and A2A payloads | PENDING/RUNNING/terminal | read-only | yes | 10, 16 |
| Timeline / History | `/api/v1/intents`, `/api/v1/conversations`, `/api/v1/sessions/{id}/intents` | GET | persisted items/session entries | all states | read-only | yes | 9, 10 |
| Evidence | `/api/v1/intents/{id}/evidence`, `/execution`, `/assurance`, `/system/flows` | GET | evidence plane records; missing pre-execution planes remain `NOT_APPLICABLE` | terminal/active | read-only | yes | 11 |
| Decision Report | `/api/v1/intents/{id}/decision-report`, `/decisions`, `/details` | GET | final status, reason, admission, planned/actual actions | all states | read-only | yes | 10 |
| Current intents | `/api/v1/intents/current` | GET | server-selected `display_bucket` projection | active/current only | read-only | yes | 9, 23 |
| Withdrawal | `/api/v1/intents/{id}/withdraw`, `/intents/effective/{scope}/withdraw` | POST | authoritative withdrawal transition/readback | WITHDRAWING -> WITHDRAWN | mutation | yes; no optimistic terminal state | 8, 23 |
| Topology | `/api/v1/topology/current`, `/topology/events` | GET/SSE | live nodes, links and health fields | independent | read-only | yes | 12, 13, 20 |
| Telemetry / INT | `/api/v1/telemetry/current`, `/telemetry/int`, `/telemetry/history` | GET | current/series telemetry | independent | read-only | yes | 14 |
| Runtime / System | `/api/v1/health/ready`, `/health`, `/watcher/status`, `/reconciliation/status` | GET | availability and runtime status | independent | read-only | yes | 12, 20 |
| Diagnostics | `/api/v1/faults`, `/api/v1/audit`, `/diagnostics/environment` | GET | persisted fault/audit/runtime diagnostics | independent | read-only | yes | 12 |

## Semantic guardrails

- Preview is the only pre-submit action and is explicitly presented as **not executed**.
- Current-intent grouping honours the server's `display_bucket`; lifecycle fallback never puts `WITHDRAWN` into the active projection.
- The centralized lifecycle adapter maps: `PREVIEW`, `PENDING`, `PLANNING`, `EVALUATED`, `CLARIFICATION_REQUIRED`, `RUNNING`, `SATISFIED`, `UNSATISFIABLE`, `FAILED`, `DEGRADED`, `WITHDRAWING`, `WITHDRAW_PENDING_CLEANUP`, and `WITHDRAWN`.
- Capacity rejection is displayed from persisted final reason/stop category. A UI must not label it `TRUE_POLICY_CONFLICT` unless the backend explicitly returns that category.
- `planned_actions` are intent-plan evidence only; `actual_actions` require execution evidence. For pre-execution admission rejection, data-plane evidence is `NOT_APPLICABLE`, not fabricated as missing or stale.

## Browser security contract

The client only uses relative `/api/v1` URLs. It carries no master API key in the bundle. Public authentication is owned by the server-side reverse proxy; there is no localStorage login guard or demo account route.
