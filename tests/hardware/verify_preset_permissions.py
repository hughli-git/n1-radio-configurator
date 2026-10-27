"""Probe preset edits/insert/delete and restore every accepted change."""
import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from bleak import BleakClient, BleakScanner
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import N1Session, uint, blob

REPORT = (OUTPUT_DIR / "n1_preset_permissions.json")

async def operate(session, option, channel):
    reply = await session.request(0x4e2a, uint(1, 1) + uint(3, option) + blob(4, channel.encode()), 0x4e2b)
    return {"result": reply["result"], "option": reply["option"]}

def same(a, b):
    return (a.key, a.name, a.rx, a.tx, a.rx_tone, a.tx_tone) == (b.key, b.name, b.rx, b.tx, b.rx_tone, b.tx_tone)

async def main():
    report = {}
    devices = await BleakScanner.discover(timeout=8)
    target = next((d for d in devices if d.address.upper() == get_target_address()), None)
    async with BleakClient(target or get_target_address(), timeout=15) as client:
        session = N1Session(client, timeout=5)
        await session.start()
        try:
            before = await session.read_channels()
            (OUTPUT_DIR / "n1_before_preset_probe.json").write_text(json.dumps([c.to_apk() for c in before], ensure_ascii=False, indent=2), encoding="utf-8")
            presets = [c for c in before if c.kind == 1]
            original = presets[0]
            values = dict(name=(original.name or "Preset") + "T", rx=original.rx + 12500,
                          tx=original.tx + 12500, rx_tone=260 if original.rx_tone != 260 else 261,
                          tx_tone=260 if original.tx_tone != 260 else 261)
            for field, value in values.items():
                candidate = replace(original, **{field: value})
                reply = await operate(session, 2, candidate)
                actual = await session.read_channel(*original.key)
                changed = not same(actual, original)
                report[field] = dict(reply=reply, requested=value, before=getattr(original, field),
                                     actual=getattr(actual, field), editable=getattr(actual, field) == value)
                print("FIELD", field, report[field], flush=True)
                if changed:
                    restore = await operate(session, 2, original)
                    restored = await session.read_channel(*original.key)
                    if not same(restored, original):
                        raise RuntimeError(f"Failed restoring {field}: {restore}")
                    print("RESTORED", field, flush=True)
                REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            candidate = replace(presets[-1], seq=max(c.seq for c in presets)+1, name="PresetTest")
            inserted = await operate(session, 0, candidate)
            after = await session.read_channels()
            added = [c for c in after if c.key not in {x.key for x in before}]
            report["insert"] = dict(reply=inserted, added=[c.to_apk() for c in added], allowed=bool(added))
            print("INSERT", report["insert"], flush=True)
            for channel in added:
                await operate(session, 1, channel)
            last = presets[-1]
            deleted = await operate(session, 1, last)
            after = await session.read_channels()
            missing = not any(c.key == last.key for c in after)
            report["delete"] = dict(reply=deleted, target=last.key, allowed=missing)
            print("DELETE", report["delete"], flush=True)
            if missing:
                restored = await operate(session, 0, last)
                if restored["result"] != 0:
                    raise RuntimeError(f"Failed restoring deleted preset: {restored}")
            final = await session.read_channels()
            report["restored"] = len(final) == len(before) and all(same(a, b) for a,b in zip(final, before))
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if not report["restored"]:
                raise RuntimeError("Final channel snapshot differs from backup")
            print("ALL_CHANNELS_RESTORED_VERIFIED", flush=True)
        finally:
            await session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(OUTPUT_DIR / "n1_preset_permissions.log", encoding="utf-8"), logging.StreamHandler()])
    asyncio.run(main())
