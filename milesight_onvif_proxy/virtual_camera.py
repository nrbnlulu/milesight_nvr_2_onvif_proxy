"""One virtual ONVIF camera = one NVR channel."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from milesight_onvif_proxy.config import AppConfig
from milesight_onvif_proxy.nvr.discovery import Channel, StreamInfo
from milesight_onvif_proxy.onvif.event_service import EventService
from milesight_onvif_proxy.rtsp_relay import advertised_codec, mount_path


@dataclass(slots=True)
class VirtualCamera:
    channel: Channel
    config: AppConfig
    advertise_host: str
    device_uuid: str
    http_port: int
    events: EventService = field(default=None)  # type: ignore[assignment]  # set by app wiring

    @property
    def number(self) -> int:
        return self.channel.number

    @property
    def name(self) -> str:
        return self.channel.camera.name

    @property
    def stream(self) -> StreamInfo:
        return self.channel.stream

    def update_stream(self, width: int, height: int, framerate: float) -> None:
        stream = replace(
            self.channel.stream,
            width=width,
            height=height,
            framerate=framerate or self.channel.stream.framerate,
        )
        self.channel = Channel(camera=self.channel.camera, stream=stream)

    @property
    def codec(self) -> str:
        """Codec as advertised to ONVIF clients (h264 when transcoding)."""
        return advertised_codec(self.channel, transcode_to_h264=self.config.proxy.transcode_to_h264)

    @property
    def base_url(self) -> str:
        return f"http://{self.advertise_host}:{self.http_port}"

    @property
    def rtsp_uri(self) -> str:
        return f"rtsp://{self.advertise_host}:{self.config.proxy.rtsp_port}{mount_path(self.number)}"

    # Tokens (unique within this device only)
    @property
    def profile_token(self) -> str:
        return f"profile{self.number}"

    @property
    def source_token(self) -> str:
        return f"vs{self.number}"


def build_cameras(
    config: AppConfig, channels: list[Channel], advertise_host: str, nvr_serial: str
) -> list[VirtualCamera]:
    return [
        VirtualCamera(
            channel=channel,
            config=config,
            advertise_host=advertise_host,
            device_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"milesight:{nvr_serial}:{channel.number}")),
            http_port=config.proxy.http_base_port + channel.number,
        )
        for channel in channels
    ]
