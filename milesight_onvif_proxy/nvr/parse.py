"""Pure parsers for the Milesight NVR CGI responses."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NvrCamera:
    """One entry of `get.camera.ipclist`. `channel` is 1-based (NVR `chnId` + 1)."""

    channel: int
    name: str
    manufacturer: str
    model: str
    serial: str
    mac: str
    firmware: str
    state: int
    connect_state: int | None

    @property
    def online(self) -> bool:
        return self.state == 2


def parse_ipclist(text: str) -> list[NvrCamera]:
    data = json.loads(text)
    cameras = []
    for item in data.get("list", []):
        channel = int(item["chnId"]) + 1
        cameras.append(
            NvrCamera(
                channel=channel,
                name=item.get("name") or f"CAM{channel}",
                manufacturer=item.get("manufacturer") or "",
                model=item.get("model") or "",
                serial=item.get("snReal") or item.get("sn") or "",
                mac=item.get("mac") or "",
                firmware=item.get("fwversion") or "",
                state=int(item.get("state", -1)),
                connect_state=item.get("connectState"),
            )
        )
    return cameras


# Bitmask keys of `get.status.event`: character i of the value is channel i + 1.
BITMASK_KEYS = (
    "videoLoss",
    "motion",
    "audioAlarm",
    "lpr",
    "vcaEntrance",
    "vcaExiting",
    "vcaMotion",
    "vcaTamper",
    "vcaLinecross",
    "vcaLoiter",
    "vcaHuman",
    "vcaObject",
    "vcaPcntIn",
    "vcaPcntOut",
    "ipcAlarmInStatus0",
    "ipcAlarmInStatus1",
    "ipcAlarmInStatus2",
    "ipcAlarmInStatus3",
)

EventSnapshot = dict[str, str]


def parse_event_status(text: str) -> EventSnapshot:
    """Extract the per-type channel bitmasks from a `get.status.event` response."""
    snapshot: EventSnapshot = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and key in BITMASK_KEYS:
            snapshot[key] = value.strip()
    return snapshot


def channel_active(snapshot: EventSnapshot, key: str, channel: int) -> bool:
    mask = snapshot.get(key, "")
    return 0 < channel <= len(mask) and mask[channel - 1] == "1"


def diff_snapshots(
    old: EventSnapshot, new: EventSnapshot, channels: list[int]
) -> list[tuple[str, int, bool]]:
    """Return (key, channel, new_state) for every bit that flipped between snapshots."""
    changes = []
    for key, mask in new.items():
        old_mask = old.get(key, "")
        for channel in channels:
            now = 0 < channel <= len(mask) and mask[channel - 1] == "1"
            before = 0 < channel <= len(old_mask) and old_mask[channel - 1] == "1"
            if now != before:
                changes.append((key, channel, now))
    return changes
