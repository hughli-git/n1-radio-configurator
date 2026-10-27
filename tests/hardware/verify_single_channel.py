"""Verify single-channel reads and reversible saves on the connected N1."""
import argparse
import asyncio
import logging
import time
from dataclasses import replace
from bleak import BleakClient, BleakScanner
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import N1Session

async def main(exercise):
    devices = await BleakScanner.discover(timeout=8)
    device = next((d for d in devices if d.address.upper() == get_target_address()), None)
    print("SCANNED_DEVICES", [(d.name, d.address) for d in devices], flush=True)
    async with BleakClient(device or get_target_address(), timeout=15) as client:
        session = N1Session(client, timeout=5)
        await session.start()
        try:
            original = await session.read_channel(2, 3)
            print("READ_ONE_VERIFIED", original, flush=True)
            if exercise:
                target = replace(original, name=original.name + "T")
                start = time.monotonic()
                try:
                    await session.save_channel(target, existing=True)
                    print("SAVE_ONE_VERIFIED_SECONDS", round(time.monotonic()-start, 3), flush=True)
                finally:
                    await session.save_channel(original, existing=True)
                    print("ORIGINAL_CHANNEL_RESTORED_VERIFIED", flush=True)
        finally:
            await session.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exercise", action="store_true")
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(OUTPUT_DIR / "n1_single_channel_verify.log", encoding="utf8"), logging.StreamHandler()])
    asyncio.run(main(parser.parse_args().exercise))
