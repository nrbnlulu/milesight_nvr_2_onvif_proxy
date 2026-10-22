"""GStreamer RTSP server relaying each channel's NVR sub stream (no re-encode by default).

One shared, on-demand media per channel: the NVR is only pulled while at least one client is
watching, and every viewer of a channel shares a single upstream session (the NVR refuses new
sessions with `453 Not Enough Bandwidth` once its remote-stream budget is exhausted).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer

from milesight_onvif_proxy.config import AppConfig, NvrConfig, ProxyConfig
from milesight_onvif_proxy.nvr.discovery import Channel

logger = logging.getLogger(__name__)

_LATENCY_MS = 200
_ROLE = "viewer"

CapsCallback = Callable[[int, int, int, float], None]  # channel, width, height, fps


def mount_path(channel: int) -> str:
    return f"/cam{channel}"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_launch_string(nvr: NvrConfig, channel: Channel, *, transcode_to_h264: bool = False) -> str:
    """The `( ... pay0 )` launch description for one channel."""
    stream = channel.stream
    source = (
        f"rtspsrc location={_quote(nvr.rtsp_url(stream.path, with_auth=False))} "
        f"user-id={_quote(nvr.username)} user-pw={_quote(nvr.password)} "
        f"protocols=tcp latency={_LATENCY_MS} ! "
        f"application/x-rtp,media=video ! "
    )
    if stream.codec == "h265" and transcode_to_h264:
        chain = (
            "rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! "
            "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=30 ! "
            "h264parse name=parse config-interval=-1 ! rtph264pay name=pay0 pt=96"
        )
    elif stream.codec == "h265":
        chain = (
            "rtph265depay ! h265parse name=parse config-interval=-1 ! "
            "rtph265pay name=pay0 pt=96 config-interval=-1"
        )
    else:
        chain = (
            "rtph264depay ! h264parse name=parse config-interval=-1 ! "
            "rtph264pay name=pay0 pt=96 config-interval=-1"
        )
    return f"( {source}{chain} )"


def advertised_codec(channel: Channel, *, transcode_to_h264: bool) -> str:
    return "h264" if transcode_to_h264 else channel.stream.codec


class RtspRelay:
    def __init__(
        self,
        config: AppConfig,
        channels: list[Channel],
        on_caps: CapsCallback | None = None,
    ) -> None:
        self._config = config
        self._channels = channels
        self._on_caps = on_caps
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: GstRtspServer.RTSPServer | None = None

    def start(self) -> None:
        Gst.init(None)
        proxy = self._config.proxy
        server = GstRtspServer.RTSPServer()
        server.set_service(str(proxy.rtsp_port))
        server.set_address(proxy.bind_host)
        permissions = self._setup_auth(server, proxy)

        mounts = server.get_mount_points()
        for channel in self._channels:
            factory = GstRtspServer.RTSPMediaFactory()
            factory.set_launch(
                build_launch_string(
                    self._config.nvr, channel, transcode_to_h264=proxy.transcode_to_h264
                )
            )
            factory.set_shared(True)
            if permissions is not None:
                factory.set_permissions(permissions)
            factory.connect("media-configure", self._on_media_configure, channel.number)
            mounts.add_factory(mount_path(channel.number), factory)
            logger.info("RTSP mount %s -> %s (%s)", mount_path(channel.number),
                        channel.camera.name, channel.stream.codec)

        if server.attach(None) == 0:
            raise RuntimeError(f"Failed to bind RTSP server on {proxy.bind_host}:{proxy.rtsp_port}")
        self._server = server
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, name="rtsp-relay", daemon=True)
        self._thread.start()
        logger.info("RTSP relay listening on rtsp://%s:%d/", proxy.bind_host, proxy.rtsp_port)

    @staticmethod
    def _setup_auth(
        server: GstRtspServer.RTSPServer, proxy: ProxyConfig
    ) -> GstRtspServer.RTSPPermissions | None:
        if not proxy.auth_enabled:
            return None
        auth = GstRtspServer.RTSPAuth()
        token = GstRtspServer.RTSPToken()
        token.set_string("media.factory.role", _ROLE)
        auth.add_basic(
            GstRtspServer.RTSPAuth.make_basic(proxy.username, proxy.password), token
        )
        server.set_auth(auth)
        permissions = GstRtspServer.RTSPPermissions()
        permissions.add_permission_for_role(_ROLE, "media.factory.access", True)
        permissions.add_permission_for_role(_ROLE, "media.factory.construct", True)
        return permissions

    def _on_media_configure(
        self, _factory: GstRtspServer.RTSPMediaFactory, media: GstRtspServer.RTSPMedia, number: int
    ) -> None:
        if self._on_caps is None:
            return
        parse = media.get_element().get_by_name("parse")
        pad = parse.get_static_pad("src") if parse is not None else None
        if pad is None:
            return

        def probe(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.CAPS:
                structure = event.parse_caps().get_structure(0)
                ok_w, width = structure.get_int("width")
                ok_h, height = structure.get_int("height")
                ok_f, num, den = structure.get_fraction("framerate")
                if ok_w and ok_h:
                    fps = round(num / den, 2) if ok_f and den else 0.0
                    self._on_caps(number, width, height, fps)
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()
        if self._thread is not None:
            self._thread.join(timeout=5)
