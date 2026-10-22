# milesight_nvr_2_onvif_proxy

Exposes every channel of a Milesight NVR as its **own ONVIF camera**: a Device/Media/Media2/Events
server per channel (own HTTP port, UUID, real model/serial), an RTSP relay of the channel's **sub
stream** (H.264 and H.265 passthrough), and the NVR's per-channel alarms as ONVIF PullPoint events.
See [PLAN.md](PLAN.md) for the design and status.

## Run

```sh
cp config.example.yaml config.yaml   # fill in NVR host/credentials (config.yaml is gitignored)
uv sync
uv run python main.py --config config.yaml
```

- Camera for channel *N*: ONVIF at `http://<host>:<http_base_port + N>/onvif/device_service`,
  video at `rtsp://<host>:<rtsp_port>/cam<N>`.
- Clients on the same LAN find all cameras through WS-Discovery (`discovery.ws_discovery: false`
  to stay off the multicast group, e.g. while testing).
- The NVR is pulled only while someone watches a channel, and all viewers share one upstream
  session. The NVR caps remote streams (`453 Not Enough Bandwidth`); when it is full the proxy
  answers `503` for that channel.
- Requires GStreamer 1.x with `gst-rtsp-server`, plus `x264enc`/`avdec_h265` only for `transcode_to_h264`.

## Test

```sh
uv run pytest        # relay tests need ffprobe and the GStreamer x264/x265 plugins
uv run python scripts/discover.py config.yaml   # list the channels that would be exposed
```
