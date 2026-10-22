"""Polls the NVR's event status and turns bit flips into per-camera ONVIF events.

Not mapped (no sensible ONVIF equivalent for a pull-point boolean): `vcaExiting`, `vcaPcntIn`,
`vcaPcntOut`, `lpr`.
"""

from __future__ import annotations

import asyncio
import logging

from milesight_onvif_proxy.events_map import KINDS_BY_NVR_KEY
from milesight_onvif_proxy.nvr.client import NvrClient
from milesight_onvif_proxy.nvr.parse import EventSnapshot, channel_active, diff_snapshots
from milesight_onvif_proxy.virtual_camera import VirtualCamera

logger = logging.getLogger(__name__)

_MAX_BACKOFF_S = 10.0


class NvrEventPoller:
    def __init__(self, client: NvrClient, cameras: list[VirtualCamera], interval_s: float) -> None:
        self._client = client
        self._by_channel = {cam.number: cam for cam in cameras}
        self._interval = interval_s
        self._last: EventSnapshot | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="nvr-event-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def apply(self, snapshot: EventSnapshot) -> int:
        """Feed one snapshot; returns the number of notifications published."""
        published = 0
        if self._last is None:
            for key, kinds in KINDS_BY_NVR_KEY.items():
                if key not in snapshot:
                    continue
                for number, cam in self._by_channel.items():
                    state = channel_active(snapshot, key, number)
                    for kind in kinds:
                        cam.events.set_initial(kind, state)
        else:
            for key, number, state in diff_snapshots(
                self._last, snapshot, list(self._by_channel)
            ):
                for kind in KINDS_BY_NVR_KEY.get(key, []):
                    self._by_channel[number].events.publish(kind, state)
                    published += 1
        self._last = snapshot
        return published

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                snapshot = await self._client.get_event_snapshot()
                if not snapshot:
                    raise ValueError("empty event status response")
                published = self.apply(snapshot)
                if published:
                    logger.info("published %d event notification(s)", published)
                if failures:
                    logger.info("NVR event polling recovered after %d failure(s)", failures)
                failures = 0
                delay = self._interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  # network/parse errors must never kill the poller
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    logger.warning("NVR event poll failed (%d in a row): %s", failures, exc)
                delay = min(self._interval * 2**failures, _MAX_BACKOFF_S)
            await asyncio.sleep(delay)
