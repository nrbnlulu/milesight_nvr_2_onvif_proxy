"""YAML config loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(slots=True)
class NvrConfig:
    host: str
    username: str
    password: str
    http_port: int = 80
    rtsp_port: int = 554
    sub_path_template: str = "ch_4{channel:02d}"

    @property
    def http_base_url(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    def sub_path(self, channel: int) -> str:
        return self.sub_path_template.format(channel=channel)

    def rtsp_url(self, path: str, *, with_auth: bool = True) -> str:
        auth = f"{self.username}:{self.password}@" if with_auth else ""
        return f"rtsp://{auth}{self.host}:{self.rtsp_port}/{path}"


@dataclass(slots=True)
class ProxyConfig:
    bind_host: str = "0.0.0.0"
    advertise_host: str | None = None
    rtsp_port: int = 8554
    http_base_port: int = 8100
    username: str | None = None
    password: str | None = None
    transcode_to_h264: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.username and self.password)


@dataclass(slots=True)
class EventsConfig:
    poll_interval_s: float = 1.0


@dataclass(slots=True)
class DiscoveryConfig:
    ws_discovery: bool = True  # announce/answer WS-Discovery on the LAN
    cache_file: str = "channels.cache.json"


@dataclass(slots=True)
class ChannelOverride:
    codec: str | None = None
    path: str | None = None


@dataclass(slots=True)
class AppConfig:
    nvr: NvrConfig
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    exclude: set[int] = field(default_factory=set)
    overrides: dict[int, ChannelOverride] = field(default_factory=dict)


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    channels = raw.get("channels") or {}
    return AppConfig(
        nvr=NvrConfig(**raw["nvr"]),
        proxy=ProxyConfig(**(raw.get("proxy") or {})),
        events=EventsConfig(**(raw.get("events") or {})),
        discovery=DiscoveryConfig(**(raw.get("discovery") or {})),
        exclude=set(channels.get("exclude") or []),
        overrides={
            int(ch): ChannelOverride(**ov) for ch, ov in (channels.get("overrides") or {}).items()
        },
    )
