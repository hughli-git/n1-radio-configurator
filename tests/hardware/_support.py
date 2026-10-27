"""Local target selection and ignored output paths for hardware checks."""
import configparser
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "archive" / "test_runs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_target_address():
    address = os.environ.get("N1_TEST_ADDRESS", "").strip()
    if address:
        return address.upper()
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "config.ini", encoding="utf-8")
    addresses = {
        config.get(section, "address", fallback="").strip()
        for section in config.sections()
        if section.startswith("device_")
        and config.get(section, "name", fallback="").startswith("N1")
    } - {""}
    if len(addresses) == 1:
        return addresses.pop().upper()
    raise RuntimeError("Set N1_TEST_ADDRESS, or keep exactly one N1 device in config.ini")
