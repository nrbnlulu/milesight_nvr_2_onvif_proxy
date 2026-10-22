import argparse

from milesight_onvif_proxy.app import main as run_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Milesight NVR -> per-channel ONVIF cameras")
    parser.add_argument("--config", default="config.yaml")
    run_app(parser.parse_args().config)


if __name__ == "__main__":
    main()
