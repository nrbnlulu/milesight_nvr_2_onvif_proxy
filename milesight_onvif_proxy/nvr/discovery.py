"""Resolve the exposed channel set: NVR camera list + a cheap RTSP DESCRIBE per sub stream.

DESCRIBE doesn't reserve a stream slot on the NVR (SETUP does, and the NVR answers
`453 Not Enough Bandwidth` when its remote-stream budget is used up), so discovery is safe to
run while other clients are pulling video. Resolution isn't in the SDP; it comes from the cache
and is refined by the relay once a real stream is flowing (see `rtsp_relay`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from milesight_onvif_proxy.config import AppConfig, NvrConfig
from milesight_onvif_proxy.nvr.client import NvrClient
from milesight_onvif_proxy.nvr.parse import NvrCamera

logger = logging.getLogger(__name__)

_TIMEOUT_S = 8
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS = 640, 480, 10.0

_RTPMAP_RE = re.compile(r"a=rtpmap:\d+ (H264|H265|HEVC)/90000", re.IGNORECASE)
_FRAMERATE_RE = re.compile(r"a=(?:x-)?framerate:\s*([\d.]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StreamInfo:
    path: str
    codec: str  # "h264" | "h265"
    width: int = 0  # 0 = unknown until the relay sees real caps
    height: int = 0
    framerate: float = 0.0

    @property
    def size(self) -> tuple[int, int]:
        return (self.width or DEFAULT_WIDTH, self.height or DEFAULT_HEIGHT)

    @property
    def fps(self) -> float:
        return self.framerate or DEFAULT_FPS


@dataclass(frozen=True, slots=True)
class Channel:
    camera: NvrCamera
    stream: StreamInfo

    @property
    def number(self) -> int:
        return self.camera.channel


@dataclass(frozen=True, slots=True)
class DescribeResult:
    status: int | None  # None = network error/timeout
    codec: str | None = None
    framerate: float = 0.0


def parse_sdp(sdp: str) -> tuple[str | None, float]:
    match = _RTPMAP_RE.search(sdp)
    codec = None
    if match:
        codec = "h264" if match.group(1).upper() == "H264" else "h265"
    fps = _FRAMERATE_RE.search(sdp)
    return codec, float(fps.group(1)) if fps else 0.0


def digest_authorization(
    username: str, password: str, method: str, uri: str, realm: str, nonce: str
) -> str:
    """RFC 2069 Digest response (no qop), as used by the Milesight RTSP service."""
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (
        f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{response}"'
    )


async def _read_response(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, str], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _TIMEOUT_S)
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    body = await asyncio.wait_for(reader.readexactly(length), _TIMEOUT_S) if length else b""
    return status, headers, body


async def _describe(nvr: NvrConfig, path: str) -> DescribeResult:
    uri = nvr.rtsp_url(path, with_auth=False)
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(nvr.host, nvr.rtsp_port), _TIMEOUT_S
        )
        writer.write(f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode())
        status, headers, body = await _read_response(reader)
        if status == 401:
            challenge = headers.get("www-authenticate", "")
            realm = re.search(r'realm="([^"]*)"', challenge)
            nonce = re.search(r'nonce="([^"]*)"', challenge)
            if not (realm and nonce):
                return DescribeResult(401)
            auth = digest_authorization(
                nvr.username, nvr.password, "DESCRIBE", uri, realm.group(1), nonce.group(1)
            )
            writer.write(
                f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                f"Authorization: {auth}\r\n\r\n".encode()
            )
            status, headers, body = await _read_response(reader)
        codec, fps = parse_sdp(body.decode(errors="replace")) if status == 200 else (None, 0.0)
        return DescribeResult(status, codec, fps)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, IndexError):
        return DescribeResult(None)
    finally:
        if writer is not None:
            writer.close()


def _load_cache(path: Path) -> dict[int, StreamInfo]:
    try:
        raw = json.loads(path.read_text())
        return {int(ch): StreamInfo(**s) for ch, s in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def save_cache(path: Path, streams: dict[int, StreamInfo]) -> None:
    path.write_text(json.dumps({str(ch): asdict(s) for ch, s in sorted(streams.items())}, indent=2))


async def discover_channels(config: AppConfig, client: NvrClient) -> list[Channel]:
    cameras = [c for c in await client.get_cameras() if c.channel not in config.exclude]
    cache_path = Path(config.discovery.cache_file)
    cache = _load_cache(cache_path)
    channels: list[Channel] = []

    for camera in cameras:
        override = config.overrides.get(camera.channel)
        path = (override.path if override and override.path else None) or config.nvr.sub_path(
            camera.channel
        )
        cached = cache.get(camera.channel)
        if cached is not None and cached.path != path:
            cached = None

        result = await _describe(config.nvr, path)
        if result.status == 200 and result.codec:
            info = replace(
                cached or StreamInfo(path=path, codec=result.codec),
                codec=result.codec,
                framerate=result.framerate or (cached.framerate if cached else 0.0),
            )
        elif result.status in (404, 400) or (result.status == 200 and not result.codec):
            logger.warning("channel %d (%s): %s not available (status %s), skipping",
                           camera.channel, camera.name, path, result.status)
            continue
        elif cached is not None:
            logger.warning("channel %d (%s): DESCRIBE failed (status %s), using cached info",
                           camera.channel, camera.name, result.status)
            info = cached
        else:
            logger.warning("channel %d (%s): DESCRIBE failed (status %s) and no cache, skipping",
                           camera.channel, camera.name, result.status)
            continue

        if override and override.codec:
            info = replace(info, codec=override.codec)
        channels.append(Channel(camera=camera, stream=info))
        logger.info("channel %d (%s): %s %s %s", camera.channel, camera.name, path, info.codec,
                    f"{info.width}x{info.height}@{info.framerate:g}" if info.width else "(size TBD)")

    save_cache(cache_path, {c.number: c.stream for c in channels})
    return channels
