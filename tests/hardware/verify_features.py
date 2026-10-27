"""Read-only verification of APK-derived device settings and headset status."""
import asyncio
import json
import logging
import argparse
from pathlib import Path
from bleak import BleakClient
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import N1Session, fields

async def main(exercise=False):
    async with BleakClient(get_target_address(), timeout=15) as client:
        session = N1Session(client, timeout=5)
        await session.start()
        try:
            info = await session.read_device_info()
            print("DEVICE_INFO", info, flush=True)
            print("GLOBAL_CHANNEL_SETTINGS", fields(info.get(9, b"")), flush=True)
            if exercise:
                # Test a reversible non-channel setting, always restore it.
                value = info[10]
                try:
                    await session.update_device_info({10: 1 - value})
                    print("VOICE_ALERT_CHANGE_VERIFIED", flush=True)
                finally:
                    await session.update_device_info({10: value})
                    print("VOICE_ALERT_RESTORED_VERIFIED", flush=True)
                for tag in (12, 13, 15, 7):
                    value = info[tag]
                    target = (value + b"-T") if tag == 7 else (2 if value == 1 else 1) if tag == 13 else 1 - value
                    try:
                        await session.update_device_info({tag: target})
                        print(f"DEVICE_FIELD_{tag}_CHANGE_VERIFIED", flush=True)
                    finally:
                        await session.update_device_info({tag: value})
                        print(f"DEVICE_FIELD_{tag}_RESTORED_VERIFIED", flush=True)
                ch = fields(info[9])
                for tag in (7, 12, 13):
                    value = ch[tag]
                    try:
                        await session.update_device_info({}, {tag: 1 - value})
                        print(f"GLOBAL_FIELD_{tag}_CHANGE_VERIFIED", flush=True)
                    finally:
                        await session.update_device_info({}, {tag: value})
                        print(f"GLOBAL_FIELD_{tag}_RESTORED_VERIFIED", flush=True)
                await session.headset_scan(0)
                try:
                    await asyncio.sleep(2)
                finally:
                    await session.headset_scan(2)
                    print("HEADSET_SCAN_START_STOP_VERIFIED", flush=True)
            ear = await session.headset_state()
            print("HEADSET_STATE", ear, flush=True)
            final_info = await session.read_device_info()
            if exercise:
                for tag in (7, 10, 12, 13, 15):
                    assert final_info[tag] == info[tag], f"Device field {tag} not restored"
                assert final_info[9] == info[9], "Global settings not restored"
            (OUTPUT_DIR / "n1_features_verified.json").write_text(json.dumps({"device": final_info, "headset": ear},
                default=lambda x: {"hex": x.hex()}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            await session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(OUTPUT_DIR / "n1_features_verify.log", encoding="utf-8"), logging.StreamHandler()])
    parser = argparse.ArgumentParser()
    parser.add_argument("--exercise", action="store_true")
    asyncio.run(main(parser.parse_args().exercise))
