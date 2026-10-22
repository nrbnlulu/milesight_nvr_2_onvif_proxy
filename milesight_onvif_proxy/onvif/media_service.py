"""ONVIF Media (ver10) and Media2 (ver20) services for one virtual camera."""

from __future__ import annotations

from aiohttp import web

from milesight_onvif_proxy.onvif.soap_utils import (
    SoapRequest,
    find_text,
    local_name,
    render_op,
    soap_fault,
    soap_response,
)
from milesight_onvif_proxy.virtual_camera import VirtualCamera


def _unknown_token(token: str | None) -> web.Response:
    return soap_fault(f"Unknown ProfileToken: {token}", subcode="ter:NoProfile")


async def handle_media(request: SoapRequest, cam: VirtualCamera) -> web.Response:
    action = request.action
    if action in ("GetProfile", "GetStreamUri"):
        token = find_text(request.operation, "ProfileToken")
        if token != cam.profile_token:
            return _unknown_token(token)

    match action:
        case "GetServiceCapabilities":
            xml = render_op("media.xml", "service_capabilities")
        case "GetProfiles":
            xml = render_op("media.xml", "get_profiles", cam=cam)
        case "GetProfile":
            xml = render_op("media.xml", "get_profile", cam=cam)
        case "GetVideoSources":
            xml = render_op("media.xml", "get_video_sources", cam=cam)
        case "GetVideoSourceConfigurations":
            xml = render_op("media.xml", "get_video_source_configurations", cam=cam)
        case "GetVideoEncoderConfigurations":
            xml = render_op("media.xml", "get_video_encoder_configurations", cam=cam)
        case "GetVideoEncoderConfiguration":
            xml = render_op("media.xml", "get_video_encoder_configuration", cam=cam)
        case "GetStreamUri":
            xml = render_op("media.xml", "get_stream_uri", uri=cam.rtsp_uri)
        case other:
            return soap_fault(f"Unsupported action: {other}", subcode="ter:ActionNotSupported")
    return soap_response(xml)


async def handle_media2(request: SoapRequest, cam: VirtualCamera) -> web.Response:
    action = request.action
    match action:
        case "GetServiceCapabilities":
            xml = render_op("media2.xml", "service_capabilities")
        case "GetProfiles":
            token = find_text(request.operation, "Token")
            if token is not None and token != cam.profile_token:
                return _unknown_token(token)
            types = {
                (e.text or "").strip()
                for e in request.operation.iter()
                if local_name(e.tag) == "Type"
            }
            with_configs = bool(types & {"All", "VideoSource", "VideoEncoder"})
            xml = render_op("media2.xml", "get_profiles", cam=cam, with_configs=with_configs)
        case "GetVideoSourceConfigurations":
            xml = render_op("media2.xml", "get_video_source_configurations", cam=cam)
        case "GetVideoEncoderConfigurations":
            xml = render_op("media2.xml", "get_video_encoder_configurations", cam=cam)
        case "GetStreamUri":
            token = find_text(request.operation, "ProfileToken")
            if token != cam.profile_token:
                return _unknown_token(token)
            xml = render_op("media2.xml", "get_stream_uri", uri=cam.rtsp_uri)
        case other:
            return soap_fault(f"Unsupported action: {other}", subcode="ter:ActionNotSupported")
    return soap_response(xml)
