"""Read/verify the N1 using the same protocol implementation as the GUI."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from bleak import BleakClient, BleakScanner
from tests.hardware._support import OUTPUT_DIR, get_target_address
from n1_protocol import (Channel, N1Session, FrameDecoder, fields, read_varint,
                         packet, parse_response, COMMAND, NOTIFICATIONS)

TARGET_NAME = ""
TARGET_ADDRESS = ""
WRITE = COMMAND
NOTIFY, NOTIFY2 = NOTIFICATIONS
# Compatibility for capture verification, generated rather than a CRC lookup table.
SETUP = [packet(cmd, seq, bytes.fromhex(body)).hex() for seq, (cmd, body) in enumerate([
    (0x4e21, "080110eb82c8c902"), (0x4e23, "08011801"),
    (0x4e26, "08011801"), (0x4e23, "08011801"),
    (0x4e2f, "08011800"), (0x4e29, "0801180322021001")])]

def frame(seq):
    return packet(0x4e29, seq, bytes.fromhex("0801180322021002" if seq == 0x1a else "080110001803"))

def report(raw):
    r = parse_response(raw)
    ch = r["channel"]
    return dict(command=r["command"], result=r["result"], option=r["option"], seq=r["seq"],
                chNo=ch.seq if ch else None, kind=ch.kind if ch else None,
                rx=ch.rx if ch else None, tx=ch.tx if ch else None,
                rx_tone=ch.rx_tone if ch else 0, tx_tone=ch.tx_tone if ch else 0,
                name=ch.name if ch else "")

async def main(args):
    log = logging.getLogger("probe")
    if not args.address and not args.name:
        args.address = get_target_address()
    devices = await BleakScanner.discover(timeout=8)
    hit = next((d for d in devices if (bool(args.name) and (d.name or "").upper() == args.name.upper())
                or d.address.upper() == args.address.upper()), None)
    # Never silently connect to another N1 radio.
    if hit is None and not args.address:
        raise RuntimeError("Named N1 device not found; specify --address or retry scan")
    if hit is None:
        log.warning("Target not found in scan; attempting saved address %s", args.address)
    async with BleakClient(hit or args.address) as client:
        session = N1Session(client, emit=log.info)
        await session.start()
        try:
            channels = await session.read_channels(lambda c: log.info("CHANNEL %s", c))
            current = await session.read_current()
            if args.repeat:
                again = await session.read_channels()
                assert [c.to_apk() for c in again] == [c.to_apk() for c in channels], "Repeated read changed"
                log.info("Repeated read verified")
            if args.switch:
                key = tuple(map(int, args.switch.split(":")))
                target = next(c for c in channels if c.key == key)
                try:
                    switched = await session.switch(target)
                    log.info("SWITCH VERIFIED %s", switched)
                finally:
                    original = next(c for c in channels if c.key == current)
                    restored = await session.switch(original)
                    log.info("ORIGINAL CURRENT RESTORED %s", restored)
            result = {"current": current, "preset_count": sum(c.kind == 1 for c in channels),
                      "custom_count": sum(c.kind == 2 for c in channels),
                      "channels": [c.to_apk() for c in channels]}
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("SUCCESS preset=%d custom=%d current=%s output=%s", result["preset_count"],
                     result["custom_count"], current, args.output)
        finally:
            await session.close()
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default=TARGET_NAME)
    parser.add_argument("--address", default=TARGET_ADDRESS)
    parser.add_argument("--output", default=str(OUTPUT_DIR / "n1_channels_verified.json"))
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--switch", help="kind:seq; verifies switching then restores original current channel")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(OUTPUT_DIR / "n1_review_verify.log", encoding="utf8")])
    try:
        raise SystemExit(asyncio.run(main(args)))
    except Exception:
        logging.exception("Verification failed")
        raise SystemExit(1)
