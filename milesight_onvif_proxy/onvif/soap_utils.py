"""SOAP parsing/rendering helpers shared by the ONVIF services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"

jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

SOAP_CONTENT_TYPE = "application/soap+xml"

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tr2="http://www.onvif.org/ver20/media/wsdl"'
    ' xmlns:tev="http://www.onvif.org/ver10/events/wsdl"'
    ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
    ' xmlns:wsa="http://www.w3.org/2005/08/addressing"'
    ' xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"'
    ' xmlns:tns1="http://www.onvif.org/ver10/topics">\n'
    "<soap:Body>\n{body}\n</soap:Body>\n</soap:Envelope>\n"
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(slots=True)
class SoapRequest:
    operation: ET.Element
    header: ET.Element | None

    @property
    def action(self) -> str:
        return local_name(self.operation.tag)


def parse_soap(body: bytes) -> SoapRequest | None:
    """Parse a SOAP 1.1/1.2 envelope into its request operation element and header."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    header = soap_body = None
    for child in root:
        name = local_name(child.tag)
        if name == "Header":
            header = child
        elif name == "Body":
            soap_body = child
    if soap_body is None or len(soap_body) == 0:
        return None
    return SoapRequest(operation=soap_body[0], header=header)


def find_text(element: ET.Element, tag_local_name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == tag_local_name:
            return child.text
    return None


def render_op(template: str, macro: str, **context: object) -> str:
    """Render one response body from a `{% macro %}` in `templates/<template>`, wrapped in an envelope."""
    module = jinja_env.get_template(template).make_module(context)
    return _ENVELOPE.format(body=getattr(module, macro)(**context))


def soap_response(xml: str) -> web.Response:
    return web.Response(text=xml, content_type=SOAP_CONTENT_TYPE)


def soap_fault(reason: str, *, status: int = 500, subcode: str | None = None) -> web.Response:
    code = (
        f"<soap:Subcode><soap:Value>{subcode}</soap:Value></soap:Subcode>" if subcode else ""
    )
    reason = reason.replace("&", "&amp;").replace("<", "&lt;")
    body = (
        f'<soap:Fault><soap:Code><soap:Value>soap:Receiver</soap:Value>{code}</soap:Code>'
        f'<soap:Reason><soap:Text xml:lang="en">{reason}</soap:Text></soap:Reason></soap:Fault>'
    )
    return web.Response(
        text=_ENVELOPE.format(body=body), content_type=SOAP_CONTENT_TYPE, status=status
    )
