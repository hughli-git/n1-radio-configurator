"""Reversible real-device tests for individual insert/delete and channel ordering."""
import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from bleak import BleakClient, BleakScanner
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import N1Session, channel_matches


async def main():
    devices = await BleakScanner.discover(timeout=8)
    print("SCANNED", [(d.name,d.address) for d in devices], flush=True)
    device = next((d for d in devices if d.address.upper()==get_target_address()),None)
    async with BleakClient(device or get_target_address(),timeout=15) as client:
        session = N1Session(client,timeout=5)
        await session.start()
        original = None; initial_current = None; temp = None; mutations = False
        try:
            original = await session.read_channels()
            initial_current = await session.read_current()
            (OUTPUT_DIR / "n1_before_channel_operations.json").write_text(json.dumps(dict(
                current=initial_current,channels=[c.to_apk() for c in original]),ensure_ascii=False,indent=2),encoding="utf8")
            custom = {c.seq:c for c in original if c.kind==2}
            temp = replace(custom[4],seq=max(custom)+1,name="TEST_TEMP")
            mutations = True
            inserted = await session.save_channel(temp,False)
            print("SINGLE_INSERT_VERIFIED", inserted,flush=True)
            remaining = await session.delete_channel(inserted)
            assert {c.key for c in remaining} == {c.key for c in custom.values()}
            assert all(channel_matches(custom[c.seq],c) for c in remaining)
            print("SINGLE_DELETE_VERIFIED_OTHER_CHANNELS_UNCHANGED",len(remaining),flush=True)
            temp = None
            await session.switch(custom[4])
            swapped = [replace(c,seq=5 if c.seq==4 else 4) if c.kind==2 and c.seq in (4,5) else replace(c) for c in original]
            swapped.sort(key=lambda c:c.key)
            mapping = {(2,4):(2,5),(2,5):(2,4)}
            await session.sync(swapped,original,mapping)
            assert await session.read_current() == (2,5)
            print("ORDER_SWAP_VERIFIED_CURRENT_CHANNEL_5",flush=True)
        finally:
            try:
                if mutations and original:
                    actual = await session.read_channels()
                    if temp and any(c.key==temp.key for c in actual):
                        await session.delete_channel(temp)
                        actual = await session.read_channels()
                    await session.sync(original,actual)
                    await session.switch(next(c for c in original if c.key==initial_current))
                    restored = await session.read_channels()
                    assert len(restored)==len(original)
                    assert all(channel_matches(c,next(a for a in restored if a.key==c.key)) for c in original)
                    assert await session.read_current()==initial_current
                    print("ALL_ORIGINAL_CONFIG_AND_CURRENT_RESTORED", len(restored),initial_current,flush=True)
            finally:
                await session.close()


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,handlers=[logging.FileHandler(OUTPUT_DIR / "n1_channel_operations.log",encoding="utf8"),logging.StreamHandler()])
    asyncio.run(main())
