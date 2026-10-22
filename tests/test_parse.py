from pathlib import Path

from milesight_onvif_proxy.nvr.discovery import digest_authorization, parse_sdp
from milesight_onvif_proxy.nvr.parse import (
    channel_active,
    diff_snapshots,
    parse_event_status,
    parse_ipclist,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_ipclist_fixture():
    cameras = parse_ipclist((FIXTURES / "ipclist.json").read_text())
    assert len(cameras) == 24
    first = cameras[0]
    assert (first.channel, first.name, first.model, first.online) == (1, "CAM1", "NBD", True)
    assert not cameras[5].online  # CAM6 offline (state -1)
    assert all("REDACTED" not in c.serial for c in cameras)


def test_parse_event_status_fixture():
    snap = parse_event_status((FIXTURES / "event_status.txt").read_text())
    assert snap["videoLoss"][5] == "1"
    assert channel_active(snap, "videoLoss", 6)
    assert not channel_active(snap, "videoLoss", 1)
    assert not channel_active(snap, "motion", 3)
    assert "ipcAlarmInStatus0" in snap


def test_channel_active_out_of_range():
    assert not channel_active({"motion": "01"}, "motion", 0)
    assert not channel_active({"motion": "01"}, "motion", 3)
    assert not channel_active({}, "motion", 1)


def test_diff_snapshots():
    old = {"motion": "0100", "vcaHuman": "0000"}
    new = {"motion": "0001", "vcaHuman": "1000"}
    assert sorted(diff_snapshots(old, new, [1, 2, 4])) == [
        ("motion", 2, False),
        ("motion", 4, True),
        ("vcaHuman", 1, True),
    ]
    assert diff_snapshots(new, new, [1, 2, 3, 4]) == []


def test_diff_ignores_unexposed_channels():
    assert diff_snapshots({"motion": "00"}, {"motion": "11"}, [2]) == [("motion", 2, True)]


def test_parse_sdp():
    sdp = "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\na=framerate:10\r\n"
    assert parse_sdp(sdp) == ("h265", 10.0)
    assert parse_sdp("a=rtpmap:96 H264/90000") == ("h264", 0.0)
    assert parse_sdp("a=rtpmap:0 PCMU/8000") == (None, 0.0)


def test_digest_authorization_rfc2069_vector():
    # RFC 2069 §2.4 example
    header = digest_authorization(
        "Mufasa", "CircleOfLife", "GET", "/dir/index.html",
        "testrealm@host.com", "dcd98b7102dd2f0e8b11d0f600bfb0c093",
    )
    assert 'response="1949323746fe6a43ef61f9606e7febea"' in header
