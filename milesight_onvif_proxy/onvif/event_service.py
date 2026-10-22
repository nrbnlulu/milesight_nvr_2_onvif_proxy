"""ONVIF Events: WS-BaseNotification PullPoint subscriptions for one virtual camera."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import quoteattr

from aiohttp import web

from milesight_onvif_proxy.events_map import EVENT_KINDS, EventKind, Notification
from milesight_onvif_proxy.onvif.soap_utils import (
    SoapRequest,
    find_text,
    render_op,
    soap_fault,
    soap_response,
)

logger = logging.getLogger(__name__)

DEFAULT_LIFETIME = timedelta(minutes=2)
MAX_LIFETIME = timedelta(hours=1)
DEFAULT_PULL_TIMEOUT_S = 10.0
MAX_PULL_TIMEOUT_S = 30.0
QUEUE_LIMIT = 1000

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>[\d.]+)S)?)?$"
)


def parse_duration(text: str | None) -> float | None:
    """ISO-8601 duration -> seconds, or None if absent/invalid."""
    match = _DURATION_RE.match((text or "").strip())
    if not match or not any(match.groupdict().values()):
        return None
    p = match.groupdict()
    return (
        float(p["days"] or 0) * 86400
        + float(p["hours"] or 0) * 3600
        + float(p["minutes"] or 0) * 60
        + float(p["seconds"] or 0)
    )


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def topic_matches(expression: str, topic: str) -> bool:
    """Match a topic against a (possibly `|`-separated) ONVIF TopicExpression.

    Supports concrete topics and the `//.` descendant wildcard (`tns1:RuleEngine//.`).
    """
    for alternative in expression.split("|"):
        alternative = alternative.strip()
        if not alternative:
            continue
        if alternative.endswith("//."):
            prefix = alternative[:-3].rstrip("/")
            if not prefix or topic == prefix or topic.startswith(prefix + "/"):
                return True
        elif alternative == topic:
            return True
    return False


@dataclass(slots=True, kw_only=True)
class PullPointSubscription:
    id: str
    topic_filter: str | None = None
    queue: asyncio.Queue[Notification] = field(default_factory=lambda: asyncio.Queue(QUEUE_LIMIT))
    expires: datetime = field(default_factory=lambda: datetime.now(UTC) + DEFAULT_LIFETIME)

    def accepts(self, notification: Notification) -> bool:
        return self.topic_filter is None or topic_matches(self.topic_filter, notification.topic)

    def push(self, notification: Notification) -> None:
        if not self.accepts(notification):
            return
        if self.queue.full():
            self.queue.get_nowait()  # drop oldest rather than block the producer
        self.queue.put_nowait(notification)


class EventService:
    """Per-camera subscription registry; tracks current state so new subscribers get `Initialized`."""

    def __init__(self, source_token: str) -> None:
        self._source_token = source_token
        self._subs: dict[str, PullPointSubscription] = {}
        self._state: dict[tuple[str, str], tuple[EventKind, bool]] = {}

    def _notification(self, kind: EventKind, state: bool, operation: str) -> Notification:
        token = self._source_token
        if kind.source_is_input:
            token = f"di{kind.nvr_key[-1]}"
        return Notification(
            topic=kind.topic,
            source_name=kind.source_name,
            source_token=token,
            data_name=kind.data_name,
            state=state,
            utc_time=_fmt_time(datetime.now(UTC)),
            operation=operation,
        )

    def publish(self, kind: EventKind, state: bool) -> None:
        """Record a state change and fan a `Changed` notification out to subscribers."""
        self._state[(kind.topic, kind.nvr_key)] = (kind, state)
        notification = self._notification(kind, state, "Changed")
        self._evict_expired()
        for sub in self._subs.values():
            sub.push(notification)

    def set_initial(self, kind: EventKind, state: bool) -> None:
        """Record state without notifying (first poll)."""
        self._state[(kind.topic, kind.nvr_key)] = (kind, state)

    def _seed(self, sub: PullPointSubscription) -> None:
        for kind, state in self._state.values():
            sub.push(self._notification(kind, state, "Initialized"))

    def create_subscription(
        self, topic_filter: str | None, lifetime: timedelta = DEFAULT_LIFETIME
    ) -> PullPointSubscription:
        self._evict_expired()
        sub = PullPointSubscription(
            id=uuid.uuid4().hex,
            topic_filter=topic_filter,
            expires=datetime.now(UTC) + min(lifetime, MAX_LIFETIME),
        )
        self._subs[sub.id] = sub
        self._seed(sub)
        return sub

    def synchronize(self, sub: PullPointSubscription) -> None:
        self._seed(sub)

    def get(self, sub_id: str) -> PullPointSubscription | None:
        self._evict_expired()
        return self._subs.get(sub_id)

    def remove(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def renew(self, sub: PullPointSubscription, lifetime: timedelta = DEFAULT_LIFETIME) -> None:
        sub.expires = datetime.now(UTC) + min(lifetime, MAX_LIFETIME)

    def _evict_expired(self) -> None:
        now = datetime.now(UTC)
        for sid in [s for s, sub in self._subs.items() if sub.expires < now]:
            del self._subs[sid]


async def _drain(
    queue: asyncio.Queue[Notification], timeout_s: float, limit: int
) -> list[Notification]:
    messages: list[Notification] = []
    try:
        messages.append(await asyncio.wait_for(queue.get(), timeout=timeout_s))
    except TimeoutError:
        return messages
    while len(messages) < limit:
        try:
            messages.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return messages


def build_topic_set() -> str:
    """The `wstop:TopicSet` tree for GetEventProperties, generated from the mapping table."""
    tree: dict = {}
    leaves: dict[str, EventKind] = {}
    for kind in EVENT_KINDS:
        leaves.setdefault(kind.topic, kind)
    for topic in leaves:
        node = tree
        for part in topic.removeprefix("tns1:").split("/"):
            node = node.setdefault(part, {})

    def render(node: dict, path: str) -> str:
        out = []
        for name, children in node.items():
            full = f"{path}/{name}" if path else f"tns1:{name}"
            if children:
                out.append(f"<tns1:{name}>{render(children, full)}</tns1:{name}>")
                continue
            kind = leaves[full]
            source_type = "tt:ReferenceToken"
            out.append(
                f'<tns1:{name} wstop:topic="true"><tt:MessageDescription IsProperty="true">'
                f'<tt:Source><tt:SimpleItemDescription Name={quoteattr(kind.source_name)} Type="{source_type}"/></tt:Source>'
                f'<tt:Data><tt:SimpleItemDescription Name={quoteattr(kind.data_name)} Type="xs:boolean"/></tt:Data>'
                f"</tt:MessageDescription></tns1:{name}>"
            )
        return "".join(out)

    return render(tree, "")


def _lifetime(text: str | None, default: timedelta) -> timedelta:
    seconds = parse_duration(text)
    return timedelta(seconds=seconds) if seconds else default


async def handle_event_service(request: SoapRequest, events: EventService, base_url: str) -> web.Response:
    action = request.action
    if action == "GetServiceCapabilities":
        return soap_response(render_op("events.xml", "service_capabilities"))
    if action == "GetEventProperties":
        return soap_response(render_op("events.xml", "event_properties", topic_set=build_topic_set()))
    if action == "CreatePullPointSubscription":
        topic_filter = find_text(request.operation, "TopicExpression")
        lifetime = _lifetime(find_text(request.operation, "InitialTerminationTime"), DEFAULT_LIFETIME)
        sub = events.create_subscription(topic_filter, lifetime)
        return soap_response(
            render_op(
                "events.xml",
                "create_pullpoint",
                address=f"{base_url}/onvif/events/pullpoint/{sub.id}",
                current_time=_fmt_time(datetime.now(UTC)),
                termination_time=_fmt_time(sub.expires),
            )
        )
    return soap_fault(f"Unsupported action: {action}", subcode="ter:ActionNotSupported")


async def handle_pullpoint(
    request: SoapRequest, events: EventService, sub_id: str
) -> web.Response:
    sub = events.get(sub_id)
    if sub is None:
        return soap_fault(
            f"Unknown or expired subscription: {sub_id}", subcode="wsrf-rw:ResourceUnknownFault"
        )
    action = request.action
    now = datetime.now(UTC)

    if action == "PullMessages":
        timeout = parse_duration(find_text(request.operation, "Timeout")) or DEFAULT_PULL_TIMEOUT_S
        timeout = min(timeout, MAX_PULL_TIMEOUT_S)
        limit = int(find_text(request.operation, "MessageLimit") or 50)
        events.renew(sub)  # an active puller keeps its subscription alive
        messages = await _drain(sub.queue, timeout, limit)
        return soap_response(
            render_op(
                "events.xml",
                "pull_messages",
                current_time=_fmt_time(datetime.now(UTC)),
                termination_time=_fmt_time(sub.expires),
                messages=messages,
            )
        )
    if action == "Renew":
        text = find_text(request.operation, "TerminationTime")
        events.renew(sub, _lifetime(text, DEFAULT_LIFETIME))
        return soap_response(
            render_op("events.xml", "renew", current_time=_fmt_time(now),
                      termination_time=_fmt_time(sub.expires))
        )
    if action == "Unsubscribe":
        events.remove(sub_id)
        return soap_response(render_op("events.xml", "unsubscribe"))
    if action == "SetSynchronizationPoint":
        events.synchronize(sub)
        return soap_response(render_op("events.xml", "set_synchronization_point"))
    return soap_fault(f"Unsupported action: {action}", subcode="ter:ActionNotSupported")
