"""List the channels the proxy would expose. Usage: python scripts/discover.py config.yaml"""

import asyncio
import logging
import sys

from milesight_onvif_proxy.config import load_config
from milesight_onvif_proxy.nvr.client import NvrClient
from milesight_onvif_proxy.nvr.discovery import discover_channels


async def main(path: str) -> None:
    config = load_config(path)
    client = NvrClient(config.nvr)
    try:
        channels = await discover_channels(config, client)
    finally:
        await client.close()
    print(f"{len(channels)} channels exposed")


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
asyncio.run(main(sys.argv[1]))
