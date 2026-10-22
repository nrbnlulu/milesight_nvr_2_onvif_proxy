"""NVR alarm type -> ONVIF event topic mapping, plus the notification model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventKind:
    nvr_key: str
    topic: str
    data_name: str
    source_name: str = "VideoSourceToken"
    source_is_input: bool = False  # Source value is a digital-input token instead of the video source


EVENT_KINDS: tuple[EventKind, ...] = (
    EventKind("motion", "tns1:VideoSource/MotionAlarm", "State"),
    EventKind("motion", "tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion"),
    EventKind("vcaMotion", "tns1:RuleEngine/MotionDetector/Motion", "IsMotion"),
    EventKind("vcaHuman", "tns1:RuleEngine/HumanDetector/Human", "IsHuman"),
    EventKind("vcaLinecross", "tns1:RuleEngine/LineDetector/Crossed", "IsCrossed"),
    EventKind("vcaEntrance", "tns1:RuleEngine/FieldDetector/ObjectsInside", "IsInside"),
    EventKind("vcaLoiter", "tns1:RuleEngine/LoiteringDetector/Loitering", "IsLoitering"),
    EventKind("vcaTamper", "tns1:VideoSource/GlobalSceneChange/ImagingService", "State"),
    EventKind("videoLoss", "tns1:VideoSource/SignalLoss", "State"),
    EventKind("vcaObject", "tns1:RuleEngine/ObjectDetector/LeftOrRemoved", "State"),
    EventKind("audioAlarm", "tns1:AudioAnalytics/Audio/DetectedSound", "IsSoundDetected"),
    *(
        EventKind(
            f"ipcAlarmInStatus{i}",
            "tns1:Device/Trigger/DigitalInput",
            "LogicalState",
            source_name="InputToken",
            source_is_input=True,
        )
        for i in range(4)
    ),
)

KINDS_BY_NVR_KEY: dict[str, list[EventKind]] = {}
for _kind in EVENT_KINDS:
    KINDS_BY_NVR_KEY.setdefault(_kind.nvr_key, []).append(_kind)


@dataclass(slots=True, kw_only=True)
class Notification:
    topic: str
    source_name: str
    source_token: str
    data_name: str
    state: bool
    utc_time: str
    operation: str  # "Initialized" | "Changed"
