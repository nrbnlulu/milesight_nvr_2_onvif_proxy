"""Wires NVR discovery, RTSP relay, per-camera ONVIF servers, WS-Discovery and the event poller."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

from aiohttp import web

from milesight_onvif_proxy.config import AppConfig, load_config
from milesight_onvif_proxy.events_bridge import NvrEventPoller
from milesight_onvif_proxy.nvr.client import NvrClient
from milesight_onvif_proxy.nvr.discovery import discover_channels, save_cache
from milesight_onvif_proxy.onvif.discovery import build_bye, start_discovery
from milesight_onvif_proxy.onvif.event_service import EventService
from milesight_onvif_proxy.onvif.server import build_camera_app
from milesight_onvif_proxy.rtsp_relay import RtspRelay
from milesight_onvif_proxy.virtual_camera import build_cameras

logger = logging.getLogger(__name__)


def detect_advertise_host(configured: str | None) -> str:
    if configured:
        return configured
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def run(config: AppConfig) -> None:
    client = NvrClient(config.nvr)
    advertise_host = detect_advertise_host(config.proxy.advertise_host)
    channels = await discover_channels(config, client)
    if not channels:
        await client.close()
        raise SystemExit("No usable NVR channels found; check config and NVR connectivity")

    cameras = build_cameras(config, channels, advertise_host, nvr_serial=config.nvr.host)
    for cam in cameras:
        cam.events = EventService(cam.source_token)
    by_number = {cam.number: cam for cam in cameras}
    loop = asyncio.get_running_loop()

    def apply_caps(number: int, width: int, height: int, fps: float) -> None:
        cam = by_number[number]
        if (cam.stream.width, cam.stream.height) == (width, height):
            return
        cam.update_stream(width, height, fps)
        logger.info("cam%d: stream is %dx%d@%g", number, width, height, fps)
        save_cache(Path(config.discovery.cache_file), {c.number: c.stream for c in by_number.values()})

    relay = RtspRelay(
        config, [c for c in channels], on_caps=lambda *a: loop.call_soon_threadsafe(apply_caps, *a)
    )
    relay.start()

    runners: list[web.AppRunner] = []
    for cam in cameras:
        runner = web.AppRunner(build_camera_app(cam), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.proxy.bind_host, cam.http_port).start()
        runners.append(runner)
        logger.info("cam%d %s -> %s/onvif/device_service  rtsp %s", cam.number, cam.name,
                    cam.base_url, cam.rtsp_uri)

    discovery = await start_discovery(cameras) if config.discovery.ws_discovery else None
    poller = NvrEventPoller(client, cameras, config.events.poll_interval_s)
    poller.start()

    try:
        await asyncio.Event().wait()
    finally:
        if discovery is not None:
            discovery.announce(build_bye)
        await poller.stop()
        for runner in runners:
            await runner.cleanup()
        relay.stop()
        await client.close()


def main(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(config_path)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("Shutting down")
