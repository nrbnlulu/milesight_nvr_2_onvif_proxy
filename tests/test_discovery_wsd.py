import re
from xml.etree import ElementTree as ET

from milesight_onvif_proxy.onvif.discovery import (
    WSDiscoveryProtocol,
    build_bye,
    build_hello,
    build_probe_match,
    device_scopes,
)
from tests.test_onvif import make_camera

PROBE = (
    b'<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    b'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    b'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Header>'
    b"<w:MessageID>uuid:abc-123</w:MessageID></e:Header><e:Body><d:Probe/></e:Body></e:Envelope>"
)


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


def test_probe_match_identifies_the_camera():
    cam = make_camera(number=7)
    data = build_probe_match(cam, "uuid:abc-123")
    ET.fromstring(data)  # well-formed
    assert f"urn:uuid:{cam.device_uuid}".encode() in data
    assert b"http://10.0.0.5:8107/onvif/device_service" in data
    assert b"<wsa:RelatesTo>uuid:abc-123</wsa:RelatesTo>" in data
    assert "name/CAM7" in device_scopes(cam)


def test_hello_and_bye_are_wellformed():
    cam = make_camera()
    for data in (build_hello(cam), build_bye(cam)):
        ET.fromstring(data)


def test_probe_gets_one_match_per_camera_and_non_probes_are_ignored():
    cams = [make_camera(number=n) for n in (1, 2, 3)]
    proto = WSDiscoveryProtocol(cams)
    transport = FakeTransport()
    proto.connection_made(transport)
    proto.datagram_received(b"<garbage/>", ("1.2.3.4", 5000))
    assert transport.sent == []
    proto.datagram_received(PROBE, ("1.2.3.4", 5000))
    assert len(transport.sent) == 3
    assert {addr for _, addr in transport.sent} == {("1.2.3.4", 5000)}
    for data, _ in transport.sent:
        assert data.count(b"<wsd:ProbeMatch>") == 1
    assert sorted(
        re.search(rb"http://10\.0\.0\.5:(\d+)/onvif/device_service", data).group(1)
        for data, _ in transport.sent
    ) == [b"8101", b"8102", b"8103"]
    assert len({re.search(rb"urn:uuid:([0-9a-f-]{36})", d).group(1) for d, _ in transport.sent}) == 3
    assert all(b"uuid:abc-123" in data for data, _ in transport.sent)
