import shutil
import subprocess
import threading
import time

import pytest

from milesight_onvif_proxy.config import AppConfig, NvrConfig, ProxyConfig
from milesight_onvif_proxy.nvr.discovery import Channel, StreamInfo
from milesight_onvif_proxy.nvr.parse import NvrCamera
from milesight_onvif_proxy.rtsp_relay import RtspRelay, build_launch_string, mount_path
from tests.fake_upstream import FakeUpstream, free_port

needs_ffprobe = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe missing")


def _channel(number: int, codec: str) -> Channel:
    camera = NvrCamera(number, f"CAM{number}", "m", "model", f"sn{number}", "", "fw", 2, 1)
    return Channel(camera, StreamInfo(path=f"ch_4{number:02d}", codec=codec))


def _nvr(port: int) -> NvrConfig:
    return NvrConfig(host="127.0.0.1", username="u", password="p", rtsp_port=port)


def test_launch_string_per_codec():
    nvr = _nvr(554)
    h264 = build_launch_string(nvr, _channel(1, "h264"))
    h265 = build_launch_string(nvr, _channel(2, "h265"))
    assert "rtph264depay" in h264 and "h265" not in h264
    assert "rtph265depay" in h265 and "rtph265pay" in h265
    assert "config-interval=-1" in h264 and "config-interval=-1" in h265
    assert "x264enc" in build_launch_string(nvr, _channel(2, "h265"), transcode_to_h264=True)
    assert "x264enc" not in build_launch_string(nvr, _channel(1, "h264"), transcode_to_h264=True)


def _ffprobe(url: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", url],
        check=False, capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip() or out.stderr.strip()


@pytest.fixture
def relay_env():
    upstream = FakeUpstream({"/ch_401": "h264", "/ch_402": "h265"})
    upstream.start()
    caps: dict[int, tuple[int, int, float]] = {}
    config = AppConfig(
        nvr=_nvr(upstream.port),
        proxy=ProxyConfig(bind_host="127.0.0.1", rtsp_port=free_port()),
    )
    relay = RtspRelay(
        config,
        [_channel(1, "h264"), _channel(2, "h265")],
        on_caps=lambda ch, w, h, fps: caps.__setitem__(ch, (w, h, fps)),
    )
    relay.start()
    yield upstream, config, caps
    relay.stop()
    upstream.stop()


@needs_ffprobe
@pytest.mark.parametrize(("number", "codec"), [(1, "h264"), (2, "hevc")])
def test_relays_both_codecs(relay_env, number, codec):
    _upstream, config, caps = relay_env
    url = f"rtsp://127.0.0.1:{config.proxy.rtsp_port}{mount_path(number)}"
    assert _ffprobe(url) == f"{codec},320,240"
    deadline = time.monotonic() + 5
    while number not in caps and time.monotonic() < deadline:
        time.sleep(0.1)
    assert caps[number][:2] == (320, 240)


@needs_ffprobe
def test_viewers_share_one_upstream_session(relay_env):
    upstream, config, _caps = relay_env
    url = f"rtsp://127.0.0.1:{config.proxy.rtsp_port}{mount_path(1)}"
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(_ffprobe(url))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["h264,320,240"] * 3
    assert upstream.connections == 1
