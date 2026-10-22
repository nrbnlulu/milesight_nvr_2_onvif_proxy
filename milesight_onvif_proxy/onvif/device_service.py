"""ONVIF Device Management service for one virtual camera."""

from __future__ import annotations

from datetime import UTC, datetime

from aiohttp import web

from milesight_onvif_proxy.onvif.soap_utils import (
    SoapRequest,
    render_op,
    soap_fault,
    soap_response,
)
from milesight_onvif_proxy.virtual_camera import VirtualCamera


async def handle_device(request: SoapRequest, cam: VirtualCamera) -> web.Response:
    match request.action:
        case "GetDeviceInformation":
            xml = render_op("device.xml", "device_information", cam=cam)
        case "GetCapabilities":
            xml = render_op("device.xml", "capabilities", cam=cam)
        case "GetServices":
            xml = render_op("device.xml", "services", cam=cam, include_capability=False)
        case "GetServiceCapabilities":
            xml = render_op("device.xml", "service_capabilities")
        case "GetSystemDateAndTime":
            xml = render_op("device.xml", "system_date_time", now=datetime.now(UTC))
        case "GetScopes":
            xml = render_op("device.xml", "scopes", cam=cam)
        case "GetHostname":
            xml = render_op("device.xml", "hostname", cam=cam)
        case other:
            return soap_fault(f"Unsupported action: {other}", subcode="ter:ActionNotSupported")
    return soap_response(xml)
