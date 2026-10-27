"""Read-only real N1 validation of scoped reads and scoped save verification."""
import asyncio
import logging
from bleak import BleakClient, BleakScanner
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import N1Session, channel_matches


async def main():
    devices = await BleakScanner.discover(timeout=6)
    device = next((d for d in devices if d.address.upper()==get_target_address()),None)
    async with BleakClient(device or get_target_address(),timeout=15) as client:
        session = N1Session(client,timeout=5)
        await session.start()
        try:
            presets = await session.read_channels(kinds=(1,))
            print("PRESET_ONLY_READ",len(presets),flush=True)
            custom = await session.read_channels(kinds=(2,))
            print("CUSTOM_ONLY_READ",len(custom),flush=True)
            current = await session.read_current()
            verified = await session.sync(custom,custom,kinds=(2,))
            assert len(verified)==len(custom)
            assert all(channel_matches(c,a) for c,a in zip(custom,verified))
            assert await session.read_current()==current
            print("CUSTOM_SCOPED_VERIFY_NO_MUTATIONS_CURRENT_UNCHANGED",current,flush=True)
            presets_after = await session.read_channels(kinds=(1,))
            assert len(presets_after)==len(presets)
            assert all(channel_matches(c,a) for c,a in zip(presets,presets_after))
            print("PRESETS_UNCHANGED_VERIFIED",len(presets_after),flush=True)
        finally:
            await session.close()


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,handlers=[logging.FileHandler(OUTPUT_DIR / "n1_scoped_channels.log",encoding="utf8"),logging.StreamHandler()])
    asyncio.run(main())
