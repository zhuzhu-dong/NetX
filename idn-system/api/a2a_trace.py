"""Runtime A2A trace recording — thin re-export from infra/trace.py.

All logic lives in ``infra.trace.TraceBus``.  This module exists solely
for backward compatibility; new code should ``from infra.trace import TraceBus``.
"""
# fmt: off
from infra.trace import (  # noqa: F401
    PROTOCOL_VERSION,
    TraceBus,
    build_runtime_evidence,
    normalize_message,
    record_consensus_sync,
    record_delegation,
    record_heartbeat,
    record_react_trigger,
    record_response,
)
# fmt: on
