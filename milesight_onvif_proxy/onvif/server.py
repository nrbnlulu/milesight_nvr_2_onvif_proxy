"""Per-camera ONVIF HTTP server: one aiohttp app (and port) per virtual camera."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from milesight_onvif_proxy.onvif.auth import UNAUTHENTICATED_ACTIONS, verify_username_token
from milesight_onvif_proxy.onvif.device_service import handle_device
from milesight_onvif_proxy.onvif.event_service import (
    handle_event_service,
    handle_pullpoint,
)
from milesight_onvif_proxy.onvif.media_service import handle_media, handle_media2
from milesight_onvif_proxy.onvif.soap_utils import SoapRequest, parse_soap, soap_fault
from milesight_onvif_proxy.virtual_camera import VirtualCamera

logger = logging.getLogger(__name__)

Handler = Callable[[SoapRequest, VirtualCamera], Awaitable[web.Response]]


def _authorized(cam: VirtualCamera, request: SoapRequest) -> bool:
    proxy = cam.config.proxy
    if not proxy.auth_enabled or request.action in UNAUTHENTICATED_ACTIONS:
        return True
    return verify_username_token(request.header, proxy.username, proxy.password)


def build_camera_app(cam: VirtualCamera) -> web.Application:
    events = cam.events

    async def serve(http: web.Request, service: str, call: Handler) -> web.Response:
        parsed = parse_soap(await http.read())
        if parsed is None:
            return soap_fault("Malformed SOAP request", status=400, subcode="ter:InvalidArgVal")
        logger.debug("cam%d %s: %s", cam.number, service, parsed.action)
        if not _authorized(cam, parsed):
            return soap_fault("Sender not authorized", status=401, subcode="ter:NotAuthorized")
        return await call(parsed, cam)

    async def device(http: web.Request) -> web.Response:
        return await serve(http, "device", handle_device)

    async def media(http: web.Request) -> web.Response:
        return await serve(http, "media", handle_media)

    async def media2(http: web.Request) -> web.Response:
        return await serve(http, "media2", handle_media2)

    async def event(http: web.Request) -> web.Response:
        async def call(req: SoapRequest, c: VirtualCamera) -> web.Response:
            return await handle_event_service(req, events, c.base_url)

        return await serve(http, "event", call)

    async def pullpoint(http: web.Request) -> web.Response:
        sub_id = http.match_info["sub_id"]

        async def call(req: SoapRequest, _c: VirtualCamera) -> web.Response:
            return await handle_pullpoint(req, events, sub_id)

        return await serve(http, "pullpoint", call)

    app = web.Application()
    app.router.add_post("/onvif/device_service", device)
    app.router.add_post("/onvif/media_service", media)
    app.router.add_post("/onvif/media2_service", media2)
    app.router.add_post("/onvif/event_service", event)
    app.router.add_post("/onvif/events/pullpoint/{sub_id}", pullpoint)
    return app
