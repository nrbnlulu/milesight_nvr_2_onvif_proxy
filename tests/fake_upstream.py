"""A fake NVR RTSP endpoint (GStreamer test sources) for relay tests."""

from __future__ import annotations

import socket
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer

PIPELINES = {
    "h264": (
        "( videotestsrc is-live=true ! video/x-raw,width=320,height=240,framerate=10/1 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast key-int-max=10 ! h264parse ! "
        "rtph264pay name=pay0 pt=96 )"
    ),
    "h265": (
        "( videotestsrc is-live=true ! video/x-raw,width=320,height=240,framerate=10/1 ! "
        "x265enc tune=zerolatency speed-preset=ultrafast key-int-max=10 ! h265parse ! "
        "rtph265pay name=pay0 pt=96 )"
    ),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeUpstream:
    def __init__(self, mounts: dict[str, str]) -> None:
        """mounts: path (e.g. '/ch_401') -> codec ('h264' | 'h265')."""
        Gst.init(None)
        self.port = free_port()
        self.connections = 0
        self._server = GstRtspServer.RTSPServer()
        self._server.set_service(str(self.port))
        self._server.set_address("127.0.0.1")
        self._server.connect("client-connected", self._on_client)
        for path, codec in mounts.items():
            factory = GstRtspServer.RTSPMediaFactory()
            factory.set_launch(PIPELINES[codec])
            factory.set_shared(True)
            self._server.get_mount_points().add_factory(path, factory)
        self._loop = GLib.MainLoop()
        self._server.attach(None)
        self._thread = threading.Thread(target=self._loop.run, daemon=True)

    def _on_client(self, *_: object) -> None:
        self.connections += 1

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._loop.quit()
        self._thread.join(timeout=5)
