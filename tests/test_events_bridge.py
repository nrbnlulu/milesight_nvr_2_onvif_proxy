import asyncio

from milesight_onvif_proxy.events_bridge import NvrEventPoller
from milesight_onvif_proxy.events_map import KINDS_BY_NVR_KEY
from tests.test_onvif import make_camera


def snap(**masks: str) -> dict[str, str]:
    return {"videoLoss": "0" * 8, "vcaHuman": "0" * 8, "motion": "0" * 8} | masks


class FakeClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def get_event_snapshot(self):
        self.calls += 1
        item = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(item, Exception):
            raise item
        return item


def _cams():
    cams = [make_camera(number=n) for n in (2, 3)]
    return cams, NvrEventPoller(FakeClient([snap()]), cams, 0.01)


def _pull(_cam, sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


async def test_first_snapshot_sets_state_without_notifying():
    cams, poller = _cams()
    sub = cams[0].events.create_subscription(None)
    assert poller.apply(snap(vcaHuman="01000000")) == 0
    assert _pull(cams[0], sub) == []
    # a subscriber arriving later sees the current state as Initialized
    late = cams[0].events.create_subscription(None)
    initialized = {(n.data_name, n.state) for n in _pull(cams[0], late) if n.operation == "Initialized"}
    assert ("IsHuman", True) in initialized  # channel 2 is bit index 1


async def test_changes_reach_only_the_affected_camera():
    cams, poller = _cams()
    subs = [c.events.create_subscription(None) for c in cams]
    poller.apply(snap())
    assert poller.apply(snap(vcaHuman="01000000")) == len(KINDS_BY_NVR_KEY["vcaHuman"])
    cam2, cam3 = (_pull(c, s) for c, s in zip(cams, subs, strict=True))
    assert [(n.data_name, n.state, n.operation) for n in cam2] == [("IsHuman", True, "Changed")]
    assert cam3 == []
    poller.apply(snap(vcaHuman="00000000"))
    assert [(n.state) for n in _pull(cams[0], subs[0])] == [False]


async def test_unexposed_channels_and_unmapped_keys_are_ignored():
    cams, poller = _cams()
    subs = [c.events.create_subscription(None) for c in cams]
    poller.apply(snap(vcaPcntIn="0" * 8))
    assert poller.apply(snap(vcaHuman="10000000", vcaPcntIn="11111111")) == 0  # ch1 not exposed
    assert all(_pull(c, s) == [] for c, s in zip(cams, subs, strict=True))


async def test_poll_loop_survives_errors_and_recovers():
    cams = [make_camera(number=2)]
    client = FakeClient([RuntimeError("boom"), {}, snap(), snap(vcaHuman="01000000")])
    poller = NvrEventPoller(client, cams, 0.01)
    sub = cams[0].events.create_subscription(None)
    poller.start()
    for _ in range(300):
        if not sub.queue.empty():
            break
        await asyncio.sleep(0.02)
    await poller.stop()
    assert [n.data_name for n in _pull(cams[0], sub)] == ["IsHuman"]
