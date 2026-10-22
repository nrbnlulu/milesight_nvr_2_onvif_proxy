"""End-to-end tests of one virtual camera's ONVIF HTTP surface (no NVR, no GStreamer)."""

import base64
import hashlib
import re
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest
from aiohttp.test_utils import TestClient, TestServer

from milesight_onvif_proxy.config import AppConfig, NvrConfig, ProxyConfig
from milesight_onvif_proxy.events_map import KINDS_BY_NVR_KEY
from milesight_onvif_proxy.nvr.discovery import Channel, StreamInfo
from milesight_onvif_proxy.nvr.parse import NvrCamera
from milesight_onvif_proxy.onvif.event_service import EventService
from milesight_onvif_proxy.onvif.server import build_camera_app
from milesight_onvif_proxy.virtual_camera import build_cameras

ENV = (
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">{header}'
    "<s:Body>{body}</s:Body></s:Envelope>"
)
TDS = 'xmlns="http://www.onvif.org/ver10/device/wsdl"'
TRT = 'xmlns="http://www.onvif.org/ver10/media/wsdl"'
TR2 = 'xmlns="http://www.onvif.org/ver20/media/wsdl"'
TEV = 'xmlns="http://www.onvif.org/ver10/events/wsdl"'


def soap(body: str, header: str = "") -> str:
    return ENV.format(body=body, header=f"<s:Header>{header}</s:Header>" if header else "")


def make_camera(codec: str = "h265", number: int = 4, **proxy):
    camera = NvrCamera(number, f"CAM{number}", "Milesight", "MS-C2967", "SER123", "AA-BB", "61.8", 2, 1)
    channel = Channel(camera, StreamInfo(f"ch_4{number:02d}", codec, 640, 480, 10.0))
    config = AppConfig(
        nvr=NvrConfig(host="nvr", username="a", password="b"),
        proxy=ProxyConfig(http_base_port=8100, **proxy),
    )
    cam = build_cameras(config, [channel], "10.0.0.5", "nvr")[0]
    cam.events = EventService(cam.source_token)
    return cam


@pytest.fixture
async def client():
    cam = make_camera()
    async with TestClient(TestServer(build_camera_app(cam))) as c:
        c.cam = cam
        yield c


async def call(client: TestClient, path: str, body: str, header: str = ""):
    resp = await client.post(path, data=soap(body, header), headers={"Content-Type": "application/soap+xml"})
    text = await resp.text()
    return resp.status, text, ET.fromstring(text)


def find(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == name]


async def test_device_information_uses_real_nvr_data(client):
    status, _, root = await call(client, "/onvif/device_service", f"<GetDeviceInformation {TDS}/>")
    assert status == 200
    assert find(root, "Model")[0].text == "MS-C2967"
    assert find(root, "SerialNumber")[0].text == "SER123"


async def test_capabilities_and_services_point_at_this_camera(client):
    _, text, _ = await call(client, "/onvif/device_service", f"<GetCapabilities {TDS}/>")
    assert "http://10.0.0.5:8104/onvif/media_service" in text
    _, text, _ = await call(client, "/onvif/device_service", f"<GetServices {TDS}/>")
    assert "http://10.0.0.5:8104/onvif/media2_service" in text
    _, text, _ = await call(client, "/onvif/device_service", f"<GetScopes {TDS}/>")
    assert "onvif://www.onvif.org/name/CAM4" in text


@pytest.mark.parametrize(("codec", "expected"), [("h264", "H264"), ("h265", "H265")])
async def test_media_profiles_advertise_codec(codec, expected):
    cam = make_camera(codec)
    async with TestClient(TestServer(build_camera_app(cam))) as c:
        for path, body in [
            ("/onvif/media_service", f"<GetProfiles {TRT}/>"),
            ("/onvif/media2_service", f'<GetProfiles {TR2}><Type>All</Type></GetProfiles>'),
        ]:
            status, _, root = await call(c, path, body)
            assert status == 200
            assert find(root, "Encoding")[0].text == expected
            assert find(root, "Width")[0].text == "640"
        _, _, root = await call(c, "/onvif/media_service", f"<GetProfiles {TRT}/>")
        assert (len(find(root, "H264")) == 1) == (codec == "h264")


async def test_media2_profiles_without_type_have_no_configurations(client):
    _, _, root = await call(client, "/onvif/media2_service", f"<GetProfiles {TR2}/>")
    assert find(root, "Profiles") and not find(root, "VideoEncoder")


async def test_stream_uri_has_no_nvr_credentials(client):
    body = f"<GetStreamUri {TRT}><StreamSetup/><ProfileToken>profile4</ProfileToken></GetStreamUri>"
    _, _, root = await call(client, "/onvif/media_service", body)
    assert find(root, "Uri")[0].text == "rtsp://10.0.0.5:8554/cam4"
    body2 = f"<GetStreamUri {TR2}><Protocol>RTSP</Protocol><ProfileToken>profile4</ProfileToken></GetStreamUri>"
    _, _, root = await call(client, "/onvif/media2_service", body2)
    assert find(root, "Uri")[0].text == "rtsp://10.0.0.5:8554/cam4"


async def test_unknown_profile_token_faults(client):
    body = f"<GetStreamUri {TRT}><ProfileToken>nope</ProfileToken></GetStreamUri>"
    status, text, _ = await call(client, "/onvif/media_service", body)
    assert status == 500 and "NoProfile" in text


async def test_malformed_and_unsupported(client):
    resp = await client.post("/onvif/device_service", data="not xml")
    assert resp.status == 400
    status, text, _ = await call(client, "/onvif/device_service", f"<Reboot {TDS}/>")
    assert status == 500 and "ActionNotSupported" in text


def _create_sub(extra: str = "") -> str:
    return f"<CreatePullPointSubscription {TEV}>{extra}</CreatePullPointSubscription>"


async def _subscribe(client, extra=""):
    _, _, root = await call(client, "/onvif/event_service", _create_sub(extra))
    address = find(root, "Address")[0].text
    assert address.startswith("http://10.0.0.5:8104/onvif/events/pullpoint/")
    return address.removeprefix("http://10.0.0.5:8104")


def _pull(timeout="PT1S"):
    return f"<PullMessages {TEV}><Timeout>{timeout}</Timeout><MessageLimit>50</MessageLimit></PullMessages>"


def _items(root):
    return [
        (m.get("PropertyOperation"), find(m, "SimpleItem")[-1].get("Name"),
         find(m, "SimpleItem")[-1].get("Value"))
        for m in find(root, "Message") if m.get("PropertyOperation")
    ]


async def test_events_initialized_then_changed(client):
    events = client.cam.events
    for kind in KINDS_BY_NVR_KEY["vcaHuman"]:
        events.set_initial(kind, False)
    path = await _subscribe(client)
    _, text, root = await call(client, path, _pull())
    assert ("Initialized", "IsHuman", "false") in _items(root)
    assert "xmlns:tns1" in text  # topic prefix is declared

    events.publish(KINDS_BY_NVR_KEY["vcaHuman"][0], True)
    _, _, root = await call(client, path, _pull())
    assert _items(root) == [("Changed", "IsHuman", "true")]


async def test_pull_times_out_with_no_messages(client):
    path = await _subscribe(client)
    status, _, root = await call(client, path, _pull("PT0.2S"))
    assert status == 200 and _items(root) == []


async def test_topic_filter_and_set_synchronization_point(client):
    events = client.cam.events
    events.set_initial(KINDS_BY_NVR_KEY["vcaHuman"][0], True)
    events.set_initial(KINDS_BY_NVR_KEY["videoLoss"][0], False)
    path = await _subscribe(
        client,
        "<Filter xmlns:wsnt='http://docs.oasis-open.org/wsn/b-2' xmlns:tns1='http://www.onvif.org/ver10/topics'>"
        "<wsnt:TopicExpression Dialect='http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet'>"
        "tns1:RuleEngine//.</wsnt:TopicExpression></Filter>",
    )
    _, _, root = await call(client, path, _pull("PT0.2S"))
    assert [i[1] for i in _items(root)] == ["IsHuman"]  # SignalLoss filtered out

    await call(client, path, f"<SetSynchronizationPoint {TEV}/>")
    _, _, root = await call(client, path, _pull("PT0.2S"))
    assert [i[0] for i in _items(root)] == ["Initialized"]


async def test_renew_unsubscribe_and_unknown(client):
    path = await _subscribe(client)
    status, _, root = await call(
        client, path,
        '<Renew xmlns="http://docs.oasis-open.org/wsn/b-2"><TerminationTime>PT5M</TerminationTime></Renew>',
    )
    assert status == 200 and find(root, "TerminationTime")
    status, _, _ = await call(client, path, '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>')
    assert status == 200
    status, text, _ = await call(client, path, _pull())
    assert status == 500 and "ResourceUnknown" in text


async def test_event_properties_topic_set(client):
    _, text, root = await call(client, "/onvif/event_service", f"<GetEventProperties {TEV}/>")
    assert find(root, "TopicSet")
    for topic in ("HumanDetector", "SignalLoss", "MotionAlarm", "DigitalInput"):
        assert topic in text


# --- auth ---------------------------------------------------------------------------------

def _wsse(username: str, password: str, *, text: bool = False, created: str | None = None) -> str:
    created = created or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = b"0123456789abcdef"
    if text:
        pw = f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>'
        extra = ""
    else:
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        pw = f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        extra = f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>"
    return (
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"'
        ' xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{username}</wsse:Username>{pw}{extra}</wsse:UsernameToken></wsse:Security>"
    )


@pytest.fixture
async def secure_client():
    cam = make_camera(username="viewer", password="s3cret")
    async with TestClient(TestServer(build_camera_app(cam))) as c:
        yield c


async def test_auth_required_except_open_operations(secure_client):
    body = f"<GetDeviceInformation {TDS}/>"
    status, text, _ = await call(secure_client, "/onvif/device_service", body)
    assert status == 401 and "NotAuthorized" in text
    status, _, _ = await call(secure_client, "/onvif/device_service", f"<GetSystemDateAndTime {TDS}/>")
    assert status == 200
    status, _, _ = await call(secure_client, "/onvif/device_service", f"<GetCapabilities {TDS}/>")
    assert status == 200


async def test_auth_digest_and_text_accepted_wrong_rejected(secure_client):
    body = f"<GetDeviceInformation {TDS}/>"
    for header, expected in [
        (_wsse("viewer", "s3cret"), 200),
        (_wsse("viewer", "s3cret", text=True), 200),
        (_wsse("viewer", "wrong"), 401),
        (_wsse("viewer", "wrong", text=True), 401),
        (_wsse("intruder", "s3cret"), 401),
        (_wsse("viewer", "s3cret", created="2020-01-01T00:00:00Z"), 401),  # replay/skew
    ]:
        status, _, _ = await call(secure_client, "/onvif/device_service", body, header)
        assert status == expected, header[:80]


async def test_event_service_also_requires_auth(secure_client):
    status, _, _ = await call(secure_client, "/onvif/event_service", _create_sub())
    assert status == 401
    status, _, _ = await call(secure_client, "/onvif/event_service", _create_sub(), _wsse("viewer", "s3cret"))
    assert status == 200


def test_distinct_cameras_get_distinct_identity():
    a, b = make_camera(number=1), make_camera(number=2)
    assert a.device_uuid != b.device_uuid and a.http_port != b.http_port
    assert re.fullmatch(r"[0-9a-f-]{36}", a.device_uuid)
