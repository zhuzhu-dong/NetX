"""Read-only generic operating-state projections for the frontend."""
from __future__ import annotations

import json

from fastapi import APIRouter

from api.store import get_store
from infra.qos_resource_allocator import QoSResourceAllocator

router = APIRouter(prefix="/api/v1/system", tags=["system-state"])


@router.get("/policies")
async def policies_state():
    store = get_store()
    with store._lock, store._connect() as connection:
        rows = connection.execute("SELECT * FROM policies ORDER BY updated_at DESC").fetchall()
    values = []
    for row in rows:
        item = dict(row)
        for column in ("yang_json", "runtime_json"):
            if column in item:
                item[column.removesuffix("_json")] = json.loads(item.pop(column) or "{}")
        values.append(item)
    return {"policies": values, "count": len(values)}


@router.get("/flows")
async def flows_state():
    from mcp_servers.onos_server import get_onos_client

    raw = await get_onos_client().get_flows()
    flows = raw.get("flows") or []
    return {"flows": flows, "count": len(flows), "source": "onos_rest"}


@router.get("/queues")
async def queues_state():
    bindings = QoSResourceAllocator(get_store()).list_active()
    values = [item.model_dump(mode="json") for item in bindings]
    return {"queues": values, "count": len(values), "source": "qos_resource_bindings"}
