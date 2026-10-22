"""aiohttp client for the Milesight NVR CGI API (`sdk.cgi`)."""

from __future__ import annotations

import aiohttp

from milesight_onvif_proxy.config import NvrConfig
from milesight_onvif_proxy.nvr.parse import (
    EventSnapshot,
    NvrCamera,
    parse_event_status,
    parse_ipclist,
)


class NvrClient:
    def __init__(self, config: NvrConfig, session: aiohttp.ClientSession | None = None):
        self._config = config
        self._own_session = session is None
        self._session = session or aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(config.username, config.password),
            timeout=aiohttp.ClientTimeout(total=10),
        )

    async def close(self) -> None:
        if self._own_session:
            await self._session.close()

    async def _cgi(self, action: str, **params: str) -> str:
        async with self._session.get(
            f"{self._config.http_base_url}/sdk.cgi", params={"action": action, **params}
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def get_cameras(self) -> list[NvrCamera]:
        return parse_ipclist(await self._cgi("get.camera.ipclist", format="json"))

    async def get_event_snapshot(self) -> EventSnapshot:
        return parse_event_status(await self._cgi("get.status.event", trans="1"))
