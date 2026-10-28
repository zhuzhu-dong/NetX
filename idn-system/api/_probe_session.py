"""
api/_probe_session.py
======================

Persistent aiohttp session for Mininet probe HTTP calls.

Follows the same lazy-init + close pattern as
``mcp_servers.onos_server.ONOSClient``.  Shared by ``api/telemetry.py``,
``api/topology.py``, and ``api/baseline.py`` to avoid creating a new
TCP connection per probe request.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)


class ProbeSession:
    """Persistent HTTP session targeting the Mininet probe REST API."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None
        self._session_lock: asyncio.Lock | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            # QoS demonstrations deliberately run for at least 15 seconds.
            # The HTTP budget must also include server startup, TC readback,
            # process cleanup and JSON transfer; a 15s total timeout aborts a
            # healthy 15s probe before it can return evidence.
            timeout = int(os.getenv("PROBE_TIMEOUT", "60"))
            self._connector = aiohttp.TCPConnector(
                limit=max(int(os.getenv("PROBE_POOL_SIZE", "12")), 1),
                keepalive_timeout=30,
                force_close=False,
            )
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=self._connector,
            )
            logger.info("ProbeSession created (timeout=%ds)", timeout)
        return self._session

    async def get(self, path: str, params: dict | None = None) -> dict | None:
        """GET from the Mininet probe, returning JSON or None."""
        base = os.getenv("MININET_PROBE_URL")
        if not base:
            logger.warning("MININET_PROBE_URL not set — probe request skipped")
            return None
        url = f"{base.rstrip('/')}{path}"
        # Docker/Mininet restarts can invalidate a keep-alive socket while
        # leaving the backend process alive.  A GET is read-only, so one
        # connection-level retry after replacing that socket is safe and lets
        # formal recovery observe the newly started runtime.  POST deliberately
        # remains non-retrying below: its outcome can be unknown after a lost
        # response and must never duplicate a southbound mutation.
        for attempt in range(2):
            try:
                session = await self._get_session()
                async with session.get(url, params=params or {}) as resp:
                    # The Mininet probe returns structured diagnostic JSON for
                    # non-2xx responses (for example, an INT receiver timeout).
                    # Preserve that body instead of replacing it with aiohttp's
                    # generic ``502 Bad Gateway`` exception text.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {"error": (await resp.text()).strip() or resp.reason}
                    if resp.status >= 400:
                        return {"status": "error", "http_status": resp.status, **data}
                    return data
            except aiohttp.ClientConnectionError as exc:
                if attempt == 0:
                    logger.info("Probe GET %s reconnecting after transport reset: %s", path, exc)
                    await self.close()
                    continue
                logger.warning("Probe GET %s failed after reconnect: %s", path, exc)
                return {"status": "unavailable", "error": str(exc)}
            except Exception as exc:
                logger.warning("Probe GET %s failed: %s", path, exc)
                return {"status": "unavailable", "error": str(exc)}

    async def post(self, path: str, json_data: dict | None = None) -> dict | None:
        """POST to the Mininet probe, returning JSON or None."""
        base = os.getenv("MININET_PROBE_URL")
        if not base:
            logger.warning("MININET_PROBE_URL not set — probe request skipped")
            return None
        url = f"{base.rstrip('/')}{path}"
        try:
            session = await self._get_session()
            async with session.post(url, json=json_data or {}) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    return {"status": "error", "http_status": resp.status, **data}
                return data
        except Exception as exc:
            logger.warning("Probe POST %s failed: %s", path, exc)
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("ProbeSession closed.")
        if self._connector is not None:
            await self._connector.close()
            self._connector = None


_probe = ProbeSession()


async def get_probe_session() -> ProbeSession:
    return _probe


async def close_probe_session() -> None:
    await _probe.close()
