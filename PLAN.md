# milesight_nvr_2_onvif_proxy — implementation plan

> **Status (2026-10-22):** implemented and unit/integration tested; see §6. Live video through the proxy is **not yet verified** — the NVR's remote-stream budget was exhausted by other consumers during development (every SETUP got 453).

Goal: expose every channel of a Milesight NVR as its own standalone ONVIF camera
(Device + Media + Events + WS-Discovery), whose RTSP stream is a proxy of that
channel's **sub stream**, and whose ONVIF events mirror that channel's NVR alarms.

## 1. What the NVR looks like (probed 2026-10-22)

| Item | Finding |
|---|---|
| Model / fw | Milesight NVR, `softversion` = `71.9.0.19-r5` |
| HTTP (CGI) | `http://213.8.48.134:90/sdk.cgi?action=…`, HTTP Basic auth |
| RTSP | only `213.8.48.134:1554` reachable externally (`get.network.port`: rtsp_port=1554) |
| ONVIF on NVR | not reachable externally (`/onvif/device_service` → 400) — so we can't simply relay the NVR's own ONVIF |
| Channels | `get.camera.ipclist&format=json` → 24 channels (`chnId` 0..23), 23 online, CAM6 `state=-1`. Gives name, manufacturer, model, sn, mac, fwversion, state per channel — perfect for per-device `GetDeviceInformation` |
| Events | `get.status.event&trans=1` → per-type 64-char bitmasks, char *i* = channel *i+1*: `videoLoss, motion, audioAlarm, lpr, vcaEntrance, vcaExiting, vcaMotion, vcaTamper, vcaLinecross, vcaLoiter, vcaHuman, vcaObject, vcaPcntIn, vcaPcntOut, ipcAlarmInStatus0..3, nvrAlarmInStatus…` (same thing systems_scrapper's `EventStatusCgi` polls) |
| Main stream | `rtsp://…:1554/ch_1NN` (NN two-digit) — e.g. `ch_101` h264 720x576 (CAM1 = EBZ NBD, matches), `ch_102` hevc 1080p15, `ch_119/120` hevc 720p |
| Stream limits | parallel pulls quickly return **453 Not Enough Bandwidth** → the NVR caps concurrent remote streams. Design must pull on demand and share one upstream per channel. |
| RTSP quirk | URLs with `?query` fail at SETUP (NVR appends track control onto the query) |

| Sub stream | `rtsp://…:1554/ch_4NN` — e.g. `ch_404/402/406/412` hevc 640x480@10, `ch_419` hevc 640x360@25; EBZ cams (`ch_401`, `ch_422`) serve h264 720x576 (same as main). Path template in config: `sub_path_template: "ch_4{channel:02d}"` |

### Known gaps (handled by discovery, step 2)
- `ch_105`/`ch_405` and `ch_124`/`ch_424` 404 even though CAM5/CAM24 report online; `ch_121` returns no video; `ch_106` streams although CAM6 is `state=-1`.
  Discovery probes every channel's sub path and only exposes channels that actually answer; config can force-include/exclude.

## 2. Architecture

```
                       ┌───────────── milesight_nvr_2_onvif_proxy (one process) ─────────────┐
 ONVIF client ──HTTP──▶│ aiohttp site :8001  → VirtualCamera(ch1)  Device/Media/Events          │
 (NVR/VMS/Frigate)     │ aiohttp site :8002  → VirtualCamera(ch2)  …                            │
                       │ …one HTTP port per channel (ONVIF identifies a device by its XAddr)   │
         ──RTSP──────▶ │ GstRtspServer :8554  /cam1 /cam2 …  (shared, on-demand rtspsrc relay) │──RTSP──▶ NVR :1554
         ──UDP 3702──▶ │ WS-Discovery: one ProbeMatch per VirtualCamera (own UUID + XAddr)     │
                       │ NvrEventPoller: polls get.status.event 1 Hz → diffs bits → per-camera │──HTTP──▶ NVR :90
                       │   EventService (PullPoint) for that channel only                      │
                       └────────────────────────────────────────────────────────────────────────┘
```

Key decisions:
- **One process, N virtual devices.** Each channel gets its own HTTP port (`http_base_port + channel`),
  its own device UUID (`uuid5(nvr_sn + channel)`), and its own serial/MAC/model taken from `ipclist`, so clients see N different cameras.
  RTSP is shared on one port with per-camera mounts (optionally per-camera RTSP ports later if a client dedups by RTSP host:port).
- **RTSP relay = GStreamer RTSP server, no transcoding.** Per mount: `rtspsrc location=<nvr sub url> protocols=tcp latency=200 ! rtph26{4,5}depay ! rtph26{4,5}pay name=pay0`.
  Codec picked from discovery (both h264 and hevc exist on this NVR). `factory.set_shared(True)` + default on-demand construction ⇒ the NVR is only pulled while ≥1 client watches, and only once per channel — essential given the 453 limit.
  Upstream errors (EOS/453) → media is torn down; next client DESCRIBE retries. (Alternative considered: MediaMTX as the relay with `sourceOnDemand` — robust, but adds a binary; keep GStreamer to reuse onvif-cam-sim's stack, revisit if relay stability is poor.)
- **H.264 and H.265 are both first-class** (this NVR's sub streams are a mix — see §1):
  - *Detection*: discovery reads the codec from the sub stream's SDP (`a=rtpmap:<pt> H264/90000` vs `H265/90000`) plus width/height/fps; cached per channel, overridable in config (`codec: h264|h265`).
  - *Relay*: chain chosen per codec — `rtph264depay ! h264parse config-interval=-1 ! rtph264pay` or `rtph265depay ! h265parse config-interval=-1 ! rtph265pay` (`name=pay0 pt=96`). `config-interval=-1` re-sends SPS/PPS(/VPS) on every keyframe so clients joining a shared stream mid-GOP can decode immediately. No transcoding by default.
  - *ONVIF Media1* (`ver10/media`): H.264 channels advertise `<tt:Encoding>H264</tt:Encoding>` + `H264` profile block. Media1's schema has no H265 enum, so H.265 channels are advertised through Media1 as well (many clients still only call Media1) with `Encoding` set to `H265` — commonly accepted in practice (Hikvision/Milesight do the same) — and properly via Media2.
  - *ONVIF Media2* (`ver20/media`): `GetProfiles`, `GetStreamUri`, `GetVideoEncoderConfigurations`, `GetVideoEncoderConfigurationOptions` with `Encoding` = `H264` / `H265` (MIME-style names per Media2). Advertised in `GetServices`/`GetCapabilities` so clients that prefer Media2 (go2rtc, Frigate, Milestone) pick it.
  - *Optional fallback*: `transcode_to_h264: true` per channel/global — inserts `rtph265depay ! h265parse ! avdec_h265 ! videoconvert ! x264enc tune=zerolatency speed-preset=ultrafast ! h264parse ! rtph264pay` for H.264-only clients (CPU cost; off by default; sub-stream resolution keeps it cheap).
  - *Tests*: relay + SOAP golden tests run for both codecs (fake upstream from onvif-cam-sim with `x264enc` and `x265enc`).
- **Events = CGI polling, not the NVR's ONVIF.** Poll `get.status.event&trans=1` every 1 s with one keep-alive session, parse the bitmask lines, diff against previous snapshot, and for each changed (channel, type) push a `PropertyOperation="Changed"` notification into that channel's EventService.
  New subscriptions get the current state as `PropertyOperation="Initialized"` (ONVIF-compliant clients expect this).
  Poll failure → keep last state, log, back off; optionally raise video-loss on all cameras after N failures.
- **Credentials**: ONVIF/RTSP side accepts configured proxy creds (WS-UsernameToken digest + RTSP basic/digest via `RTSPAuth`); NVR creds stay server-side and never appear in returned stream URIs.

### Event mapping (NVR key → ONVIF topic / data item)

| NVR key | ONVIF topic | SimpleItem |
|---|---|---|
| `motion` | `tns1:VideoSource/MotionAlarm` + `tns1:RuleEngine/CellMotionDetector/Motion` | `State` / `IsMotion` |
| `vcaMotion` | `tns1:RuleEngine/MotionDetector/Motion` | `IsMotion` |
| `vcaHuman` | `tns1:RuleEngine/HumanDetector/Human` | `IsHuman` |
| `vcaLinecross` | `tns1:RuleEngine/LineDetector/Crossed` | `IsCrossed` (state-ish; emit true/false) |
| `vcaEntrance` / `vcaExiting` | `tns1:RuleEngine/FieldDetector/ObjectsInside` | `IsInside` |
| `vcaLoiter` | `tns1:RuleEngine/LoiteringDetector/Loitering` | `IsLoitering` |
| `vcaTamper` | `tns1:VideoSource/GlobalSceneChange/ImagingService` | `State` |
| `videoLoss` | `tns1:VideoSource/SignalLoss` | `State` |
| `vcaObject` | `tns1:RuleEngine/ObjectDetector/LeftOrRemoved` (vendor-ish) | `State` |
| `audioAlarm` | `tns1:AudioAnalytics/Audio/DetectedSound` | `IsSoundDetected` |
| `ipcAlarmInStatusK` | `tns1:Device/Trigger/DigitalInput` (token `di{K}`) | `LogicalState` |

The table lives in one dict so it's easy to extend; `GetEventProperties` is rendered from the same dict.

## 3. Repo layout

Reuse the onvif-cam-sim `onvif/` package (soap_utils, templates, event_service, discovery), refactored from a single global `AppConfig` to a per-device `VirtualCamera` context. Copy rather than depend — the sim is a different product and its synthetic-source code doesn't belong here.

```
milesight_nvr_2_onvif_proxy/
  pyproject.toml            # aiohttp, jinja2, pygobject, pyyaml, msgspec
  config.example.yaml       # nvr url/creds, ports, sub_path_template, channel overrides/excludes, proxy creds
  main.py
  milesight_onvif_proxy/
    config.py               # dataclasses + loader
    nvr/
      client.py             # aiohttp CGI client: get_ipclist(), get_event_status()
      parse.py              # [STATUS] key=value + bitmask parsing (pure, unit tested)
      discovery.py          # build ChannelInfo list: ipclist + RTSP probe (codec/res/fps) sequentially; cache to json
    virtual_camera.py       # VirtualCamera dataclass: channel, uuid, http_port, mount, ChannelInfo, EventService
    rtsp_relay.py           # GstRtspServer, one on-demand shared relay factory per camera
    events_bridge.py        # NvrEventPoller: poll → diff → per-camera EventService.broadcast
    onvif/
      soap_utils.py  auth.py (WS-UsernameToken)  device_service.py  media_service.py
      event_service.py  discovery.py (multi-device ProbeMatch)  templates/*.xml
    app.py                  # wire everything; one aiohttp AppRunner + N TCPSites
  scripts/probe_rtsp.sh, probe_streams.sh   # discovery helpers (already here)
  tests/                    # parse fixtures captured from this NVR, SOAP golden tests
```

## 4. Steps

1. **Scaffold** repo (uv, ruff, pytest), config loader, `config.example.yaml`; real config with creds is gitignored.
2. **NVR client + discovery.** `get.camera.ipclist&format=json` → camera list; for each channel a raw RTSP **DESCRIBE with Digest auth** (RFC 2069, realm "RECOVISION Streaming Service") → exists? + codec from the SDP. DESCRIBE does *not* use a stream slot (SETUP does → 453), so it is safe while the NVR's budget is full. Width/height aren't in the SDP: they come from `channels.cache.json` and are refined from real caps by the relay the first time a stream flows (default 640x480@10 until then). Config can `exclude` channels or override path/codec.
   Channels whose sub path doesn't answer are skipped (logged), re-probed periodically.
3. **RTSP relay.** Factory per camera, codec-aware depay/pay chain, shared + on-demand, TCP upstream, reconnect on next request. Verify: `ffprobe rtsp://proxy:8554/cam2` while watching only one NVR session is opened for two simultaneous clients.
4. **ONVIF per-device services.** Port Device/Media handlers to take a `VirtualCamera`. Add the ops real clients call:
   Device: `GetDeviceInformation` (model/sn/fw from ipclist), `GetCapabilities`, `GetServices`, `GetSystemDateAndTime`, `GetScopes`, `GetHostname`, `GetNetworkInterfaces`.
   Media: `GetProfiles`/`GetProfile` (single profile = sub stream with real res/codec; H265 needs Media2 `GetProfiles` or `tt:H264`-only clients will choke — add minimal **Media2** (`ver20/media`) for hevc channels), `GetStreamUri`, `GetVideoSources`, `GetVideoEncoderConfiguration(s)`, `GetSnapshotUri` (optional: proxy NVR snapshot CGI if one exists).
   Auth: optional WS-UsernameToken check.
5. **Events.** Per-camera `EventService` (from the sim, add `GetEventProperties`, `SetSynchronizationPoint`, Initialized messages, per-subscription topic filter optional). `NvrEventPoller` drives them using the mapping table.
6. **WS-Discovery.** One socket; on Probe reply with one ProbeMatch per camera (or one message per camera); send `Hello` on startup. Scopes include `onvif://www.onvif.org/name/<CAM name>` and `…/hardware/<model>`.
7. **Tests.** Fixture-based parser tests (raw responses captured from this NVR), event diff tests, SOAP golden-file tests per op, a smoke test using `onvif-zeep` against a running proxy with a fake NVR (aiohttp stub + gst `videotestsrc` RTSP — the onvif-cam-sim can serve as the fake upstream).
8. **Validate against real clients**: ONVIF Device Manager / `onvif-zeep`, go2rtc or Frigate, and the t5hob system (systems_scrapper ONVIF path) — each channel should appear as a separate camera, stream should play, triggering motion on a channel should produce a PullMessages notification only on that virtual camera.

## 5. Risks

- **NVR concurrent-stream cap (453)** — mitigated by on-demand + shared; document that N watched cameras ⇒ N NVR sessions.
- **HEVC channels** — Media1's schema has no H265; strict Media1-only clients may reject them → Media2 + optional `transcode_to_h264` fallback.
- **Polling latency** — 1 s poll ⇒ ≤1 s event latency; short pulses (< poll interval) can be missed. If that matters, investigate the NVR's push/alarm HTTP notifications (`enable_push=1` in `get.network.port`) later.
- **WS-Discovery** only works on the proxy's LAN segment; remote clients add devices by IP:port manually.

## 6. Implementation status

Done (35 tests, ruff clean): config, NVR CGI client + parsers (fixtures captured from the real NVR), DESCRIBE/Digest discovery + cache, on-demand shared GStreamer relay for h264/h265 (+ optional transcode, optional Basic auth), per-camera ONVIF Device/Media/Media2/Events servers (one HTTP port per channel), WS-Security UsernameToken auth, PullPoint events with `Initialized`/`Changed`, topic filters and `GetEventProperties`, multi-device WS-Discovery with Hello/Bye, NVR event poller.

Verified live against the real NVR: channel list, per-codec detection (17x h265, 5x h264 sub streams; CAM5/CAM24 -> 404, skipped), event polling (CAM6 video-loss -> `SignalLoss=true`), and a third-party client (`onvif-zeep`) reading device info, scopes, profiles, stream URI and events.

Not verified / not done:
- **Live RTSP relay from the real NVR** (blocked by 453). Tested against a fake upstream: both codecs, real-resolution detection, N viewers -> 1 upstream session. When the upstream is full the relay answers `503` and keeps running.
- Unmapped NVR alarms: `vcaExiting`, `vcaPcntIn/Out`, `lpr`. No snapshot URI, PTZ, or recording/playback.
- Event latency is bounded by the 1 s poll; the NVR's HTTP push (`enable_push=1`) is unexplored.
- CAM6 (offline per `ipclist`) still answers DESCRIBE, so it's exposed (it reports `SignalLoss=true`). Use `channels.exclude` to hide it.
