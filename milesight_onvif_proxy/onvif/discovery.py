"""WS-Discovery responder for many virtual devices: one ProbeMatch per camera."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import uuid
from urllib.parse import quote

from milesight_onvif_proxy.virtual_camera import VirtualCamera

logger = logging.getLogger(__name__)

MCAST_GROUP = "239.255.255.250"
MCAST_PORT = 3702

_PROBE_RE = re.compile(rb"<(?:[\w-]+:)?Probe[\s>/]")
_MESSAGE_ID_RE = re.compile(rb"<(?:[\w-]+:)?MessageID>([^<]+)</(?:[\w-]+:)?MessageID>")

_NS = (
    'xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"'
)


def device_scopes(cam: VirtualCamera) -> str:
    model = quote(cam.channel.camera.model or "channel", safe="")
    return (
        "onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/Profile/Streaming "
        f"onvif://www.onvif.org/name/{quote(cam.name, safe='')} "
        f"onvif://www.onvif.org/hardware/{model}"
    )


def _match_body(cam: VirtualCamera) -> str:
    return (
        f"<wsa:EndpointReference><wsa:Address>urn:uuid:{cam.device_uuid}</wsa:Address>"
        "</wsa:EndpointReference><wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>"
        f"<wsd:Scopes>{device_scopes(cam)}</wsd:Scopes>"
        f"<wsd:XAddrs>{cam.base_url}/onvif/device_service</wsd:XAddrs>"
        "<wsd:MetadataVersion>1</wsd:MetadataVersion>"
    )


def _envelope(action: str, body: str, relates_to: str | None = None, to: str | None = None) -> bytes:
    relates = f"<wsa:RelatesTo>{relates_to}</wsa:RelatesTo>" if relates_to else ""
    to_addr = to or "http://www.w3.org/2005/08/addressing/anonymous"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><soap:Envelope {_NS}><soap:Header>'
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>{relates}"
        f"<wsa:To>{to_addr}</wsa:To><wsa:Action>{action}</wsa:Action></soap:Header>"
        f"<soap:Body>{body}</soap:Body></soap:Envelope>"
    ).encode()


def build_probe_match(cam: VirtualCamera, relates_to: str) -> bytes:
    return _envelope(
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches",
        f"<wsd:ProbeMatches><wsd:ProbeMatch>{_match_body(cam)}</wsd:ProbeMatch></wsd:ProbeMatches>",
        relates_to,
    )


def build_hello(cam: VirtualCamera) -> bytes:
    return _envelope(
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Hello",
        f"<wsd:Hello>{_match_body(cam)}</wsd:Hello>",
        to="urn:schemas-xmlsoap-org:ws:2005:04:discovery",
    )


def build_bye(cam: VirtualCamera) -> bytes:
    return _envelope(
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Bye",
        f"<wsd:Bye><wsa:EndpointReference><wsa:Address>urn:uuid:{cam.device_uuid}"
        "</wsa:Address></wsa:EndpointReference></wsd:Bye>",
        to="urn:schemas-xmlsoap-org:ws:2005:04:discovery",
    )


class WSDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, cameras: list[VirtualCamera]) -> None:
        self._cameras = cameras
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None or not _PROBE_RE.search(data):
            return
        match = _MESSAGE_ID_RE.search(data)
        relates_to = match.group(1).decode() if match else ""
        logger.info("WS-Discovery Probe from %s -> %d matches", addr, len(self._cameras))
        for cam in self._cameras:
            self._transport.sendto(build_probe_match(cam, relates_to), addr)

    def announce(self, builder) -> None:
        if self._transport is None:
            return
        for cam in self._cameras:
            self._transport.sendto(builder(cam), (MCAST_GROUP, MCAST_PORT))


async def start_discovery(cameras: list[VirtualCamera], bind_host: str = "") -> WSDiscoveryProtocol:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", MCAST_PORT))
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        struct.pack("4sl", socket.inet_aton(MCAST_GROUP), socket.INADDR_ANY),
    )
    sock.setblocking(False)
    _transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: WSDiscoveryProtocol(cameras), sock=sock
    )
    protocol.announce(build_hello)
    logger.info("WS-Discovery responder on %s:%d for %d cameras", MCAST_GROUP, MCAST_PORT, len(cameras))
    return protocol
