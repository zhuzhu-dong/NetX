# Public Production E2E Acceptance Report

## Run identity

- Test run: `PROD-E2E-20260811-PUB01`
- Date: 2026-08-11 (Asia/Shanghai)
- Public evaluator path: `<your-deployment-host>`
- Baseline revision supplied for acceptance: `bc7a594d35e8adf959088cc6166a24598118eaa7`
- Deployed revision at completion: `d8e0fdf50a58d6ac3af0b0cc56915de4e5361a96` on branch `0810`
- Test identity: `user_id=prod_e2e`, `session_id=PROD-E2E-20260811-PUB01-S13-RERUN`.

This was an in-place public-path acceptance run. It did not reboot the ECS, delete a Docker volume or database, execute `docker compose down -v`, start a watcher, expose a control port, or print any secret.

## Production baseline and final state

| Check | Baseline / final evidence | Result |
| --- | --- | --- |
| Public frontend | `GET /` and `/dashboard` returned HTTP 200 | PASS |
| Public backend | `GET /api/v1/health/ready` returned `healthy` | PASS |
| Topology | 6 switches, 5 hosts, 13 links | PASS |
| Baseline connectivity | `h_research` ↔ `h_dc` ping: 3/3 replies in each tested direction, 0% loss | PASS |
| Core containers | `idn-onos`, `idn-mininet`, `idn-backend`, `idn-frontend` all healthy after the run | PASS |
| Control-plane ports | Public 5173, 8000, 8181, 8101, 6653, 50051–50056 were closed/filtered; 22 and 80 open; 443 closed/filtered | PASS |
| Public client bundle | No `IDN_API_KEY`, provider-key, proxy-key, or demo-key marker in downloaded HTML/JS | PASS |

The deployment continues to use Nginx as the public `/api/` reverse proxy. The browser bundle does not contain an API secret. Nginx injects server-side API authentication for the proxy path.

## Functional evidence

### Real LLM preview

The public preview path accepted the canonical research-video request with the real compatible OpenAI provider (`qwen-plus`), without model fallback. Its canonical semantics were:

```text
research_video
192.168.10.10/32 -> 10.0.0.10/32
UDP / 5004 / DSCP 46
MIN_BANDWIDTH GTE 20 Mbps
clarification_required = false
```

Preview was read-only: it did not create a policy, binding, ONOS flow, P4 classifier, or TC class. Ambiguous or incomplete language was handled as clarification rather than an arbitrary network write. Unknown/invalid endpoint and malformed-value cases were fail-closed in preview; no HTTP 500 or traceback was observed.

### Capacity fail-closed result

The formal 20 Mbps MIN_BANDWIDTH candidate calibration collected five real 3-second UDP receiver samples at an offered 30 Mbps:

`16.7925, 8.2579, 19.7484, 22.2639, 22.3256 Mbps`.

The conservative lower bound was 8.2579 Mbps; after the configured 5 Mbps safety margin the schedulable value was 3.2579 Mbps. The system correctly returned `UNSATISFIABLE` / calibration-not-supported before policy, binding, ONOS, P4, or TC mutation. The analogous 30 Mbps negative path was also fail-closed. This is an external runtime-capacity limitation, not a relaxation of the SLA or safety contract.

### Formal RateLimit execution and traffic proof

Intent `INT-20260811-abab2129` was submitted through the public API using a dedicated session. It completed `SATISFIED` with `RATE_LIMIT_KPI_VERIFIED` for policy `POL-20260811-D2AE00E5`, scope `TSK2-7ce48b7a4825ee49d0f4d293`, and binding `RB-aafe4f81af6742df`.

| Layer | Observed evidence |
| --- | --- |
| ONOS | One exact flow, ID `49539598510431364`, device `device:bmv2:5`, DSCP action 46 |
| Direct P4Runtime | One `MyIngress.sla_enforcement` entry; counter increased from 24,373 / 30,270,070 bytes to 36,609 / 45,465,986 bytes during probe |
| TC / HTB | `s_access_r-eth3`, class `1:100`, `rate 20Mbit`, `ceil 20Mbit`; UDP/5004 flower with `ip_tos 0xb8/fc` and exact source/destination |
| Traffic | Official service-validation API, 15 seconds, offered 40 Mbps UDP; sender 38.6548 Mbps, receiver 13.0125 Mbps, loss 64.5229%, jitter 0.7010 ms |
| TC accounting | Target-class packets and bytes increased; HTB overlimits increased; P4 and TC both observed the test flow |

The receiver did not exceed the 20 Mbps rate limit. The unusually low receiver throughput is consistent with the independently observed external runtime capacity limit; it is not presented as a 20 Mbps MIN_BANDWIDTH success.

The active RateLimit policy was observed for 60 seconds at offsets 0/30/60. At every point the API was healthy, lifecycle was `SATISFIED`, exactly one binding was active, rate/ceil remained 20/20, and the exact P4 matching count remained one.

## P1 found and repaired during this run

Two production defects were found and fixed because they blocked truthful evidence or cleanup.

1. **Service-validation socket buffer default.** The compose default forced iperf `-w 2097152`, while the Mininet namespace exposed a much lower actual socket buffer. The probe failed before sending traffic with “socket buffer size not set correctly.” Commit `5b2704921282e9d89aa82c7048d00595e13f3603` removes the forced default while retaining an explicit operator override. Its targeted regression passed.
2. **Withdrawal reported success with a residual P4 classifier.** Withdrawal held an `INTENT_WITHDRAWAL` writer lease and then attempted nested terminal compensation. Fencing rejected the second lease (`NETWORK_WRITE_LEASE_HELD_BY_ACTIVE_WRITER`), but legacy compensation reported zero failures. In addition, successful southbound cleanup did not retire the durable policy document. Commit `d8e0fdf50a58d6ac3af0b0cc56915de4e5361a96` reuses the held fencing context, makes compensation errors fail cleanup, uses durable policy runtime flow receipts as exact fallback references, retires the policy only after verified cleanup, and retries a false `WITHDRAWN` projection as `WITHDRAW_PENDING_CLEANUP`.

Regression execution for the withdrawal repair: `33 passed` across `test_api.py` and `test_revision_idempotency_withdrawal.py`.

The actual resource was then withdrawn again via the official public DELETE endpoint. Final cross-layer readback was:

```text
policy status = WITHDRAWN; pending_yang_json = null
binding active = 0
public active binding count = 0
ONOS / direct P4 exact selector count = 0 (NO_MATCHING_ENTRY)
TC class 1:100 = absent
UDP/5004 target flower = absent
current intent projection = 0
```

No manual TC, P4Runtime, or ONOS deletion was used.

## Scenario matrix

| ID | Scenario | Status | Evidence / rationale |
| --- | --- | --- | --- |
| S01 | Public frontend and SPA routes | PASS | `/`, `/dashboard` HTTP 200; assets load through public Nginx path. |
| S02 | Public backend health/topology | PASS | `healthy`; topology 6/5/13. |
| S03 | Real LLM canonical preview | PASS | qwen-plus, no fallback; canonical research-video semantics. |
| S04 | Natural-language phrasing robustness | PASS | Canonical and ambiguous variants resolved deterministically; ambiguity led to clarification. |
| S05 | Missing-information clarification | PASS | No arbitrary selector or southbound write. |
| S06 | Invalid endpoint/value rejection | PASS | Fail-closed preview/API behavior; no traceback/500. |
| S07 | First MIN_BANDWIDTH 20 Mbps | FAIL (external capacity) | Formal calibration ran; conservative capacity did not support 20; no resource created. |
| S08 | MIN four-layer evidence | NOT EXECUTED | Dependent on a positive MIN admission; S07 correctly fail-closed. |
| S09 | Exact MIN reuse | NOT EXECUTED | Requires a safely admitted MIN policy; not fabricated under capacity limit. |
| S10 | MIN 20→10 downgrade | NOT EXECUTED | Same dependency as S09. |
| S11 | MIN 10→20 upgrade | NOT EXECUTED | Same dependency as S09. |
| S12 | 30 Mbps capacity negative | PASS | Formal fail-closed behavior; no data-plane residue. |
| S13 | RATE_LIMIT 20 Mbps | PASS | Real LLM → policy → ONOS/P4/TC → 40 Mbps probe → 60s stability → formal withdrawal. |
| S14 | MIN ↔ RATE_LIMIT mode switch | NOT EXECUTED | No admitted MIN resource exists; avoided mutating an external/baseline scope. |
| S15 | DROP / ACL_BLOCK | NOT EXECUTED FOR PRODUCTION SAFETY | Supported action family was discovered, but no isolated production test-port contract was available. |
| S16 | FORWARD/connectivity intent | NOT EXECUTED FOR PRODUCTION SAFETY | Baseline connectivity retained; no isolated forwarding test resource was created. |
| S17 | Formal conflicting policy | NOT EXECUTED | Conflict metadata was observed; no conflicting data-plane policy was introduced into production. |
| S18 | 120 Mbps resource conflict | NOT EXECUTED | The real 20/30 calibration already established capacity rejection; no excess request was needed. |
| S19 | Policy withdrawal | PASS | Initial P1 found, fixed, redeployed, and official retry cleanup proved complete. |
| S20 | Fault injection/self-healing | NOT EXECUTED FOR PRODUCTION SAFETY | No kill/fault injected into an online production data plane. |
| S21 | ONOS/Mininet transient failure | NOT EXECUTED FOR PRODUCTION SAFETY | Existing healthy containers were not killed. |
| S22 | Concurrent preview | PASS | Bounded public concurrent-preview sample completed without HTTP 500, SQLite lock, foreign-key failure, or response mixing. |
| S23 | Duplicate-click idempotency | NOT EXECUTED | Avoided creating duplicate formal resource after the cleanup defect was found. |
| S24 | Low-confidence/malformed LLM protection | PASS | Ambiguous/incomplete requests were clarified/fail-closed, never written southbound. |
| S25 | Chinese/English equivalence | NOT EXECUTED | Chinese canonical path accepted; a full English equivalence matrix was not run. |
| S26 | XSS/special/oversize matrix | NOT EXECUTED | Public bundle secret/XSS exposure check completed; full mutation matrix deferred to avoid unnecessary provider calls. |
| S27 | Public port boundary | PASS | Only 22/80 externally reachable in the tested list. |
| S28 | Secret scan | PASS | No sensitive marker in public HTML/JS; no secret printed. |
| S29 | Nginx authority behavior | SECURITY OBSERVATION | Server-side injection keeps keys out of browser bundles, but any public browser reaching Nginx can exercise destructive backend APIs. This is demo convenience, not production public write authorization. |
| S30 | Resident stability | PASS | 60-second 0/30/60 RateLimit observation stable. |
| S31 | Log health | PASS | No new traceback, 500, DB lock, P4Runtime, fencing, or rollback exception after the final repair window. |
| S32 | Final health/cleanup | PASS | Four containers healthy; public health/topology normal; tagged data-plane resource fully absent. |

## Security observations and limitations

- **SECURITY OBSERVATION:** Nginx’s server-side API-key injection prevents client-side secret exposure but gives anonymous public visitors backend mutation authority through the public proxy. Public authentication/authorization is needed before treating this as an Internet-facing write surface.
- TLS/443 is not enabled in the observed environment. It was closed/filtered, not accidentally exposed.
- The standard Dockerfile rebuild was temporarily blocked by an external Docker Hub metadata lookup for `python:3.11-slim`. The source repair was still deployed safely as a local layer on the existing healthy backend image, after the server had fast-forwarded the committed source. This deployment limitation is external infrastructure evidence, not a repository runtime failure.

## Final assessment

There is no P0. The two P1 defects found during acceptance were fixed, regression-tested, deployed, and revalidated. The remaining negative/chaos/migration scenarios are explicitly marked rather than claimed as pass. The production system is healthy and the test resource is fully cleaned; the external runtime capacity limit remains active and truthful MIN_BANDWIDTH admission stays fail-closed.
