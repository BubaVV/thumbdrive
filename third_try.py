#!/usr/bin/env python3
"""Minimal demo: connect to Trek ThumbDrive, read a few blocks, disconnect."""

import logging
from trek_usb import TrekDevice

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

with TrekDevice.open() as dev:
    # ── device info (parsed + raw) ──────────────────────────────────
    info = dev.info
    print(f"\n=== Device Info (parsed) ===")
    print(info)
    print(f"\n=== Device Info (raw {len(info.raw)} bytes) ===")
    print(info.raw.hex(":"))

    print(f"\nCapacity : {dev.capacity} bytes ({dev.capacity // (1024*1024)} MB)")
    print(f"Sectors  : {dev.total_sectors}")
    print(f"Sector sz: {dev.sector_size}")

    # ── read first 4 sectors (MBR area) ─────────────────────────────
    print(f"\n=== Reading sectors 0-3 (2048 bytes) ===")
    data = dev.read_blocks(0, 4)
    for i in range(4):
        chunk = data[i * 512 : (i + 1) * 512]
        print(f"  sector {i}: {chunk[:32].hex(':')}  ... ({len(chunk)} bytes)")

    # ── byte-level read: first 64 bytes ─────────────────────────────
    print(f"\n=== Byte read(0, 64) ===")
    head = dev.read(0, 64)
    print(f"  {head.hex(':')}")

print("\nDone — device closed.")
