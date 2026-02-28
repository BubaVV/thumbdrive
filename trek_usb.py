#!/usr/bin/env python3
"""
Trek ThumbDrive USB Communication Library

Userspace driver for the Trek ThumbDrive (VID:0x0a16, PID:0x1111),
a pre-mass-storage-class USB stick that uses vendor-specific control
transfers (bRequest=16/17) plus bulk endpoints for block I/O.

Layer stack
-----------
UsbTransport -- thin pyusb wrapper; find/configure device, issue
                control and bulk transfers.  Knows nothing about the
                Trek command format.

TrekDevice   -- protocol driver built on top of UsbTransport.
                Handles init (bRequest=16), disconnect, and provides
                both sector-level and byte-level I/O:

    dev = TrekDevice.open()
    data = dev.read(offset, length)   # byte-addressed
    dev.write(offset, data)           # byte-addressed
    dev.read_blocks(lba, count)       # sector-addressed
    dev.write_blocks(lba, count, data)
    dev.close()

BlockDevice  -- abstract base so the NBD server can swap between a
                real USB stick and a plain file image.
"""

from __future__ import annotations

import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

logger = logging.getLogger(__name__)

# ── Device identifiers ──────────────────────────────────────────────
VENDOR_ID = 0x0A16   # Trek Technology (S) PTE, Ltd
PRODUCT_ID = 0x1111  # ThumbDrive

# ── USB transfer parameters ─────────────────────────────────────────
BREQUEST_INIT = 16   # 0x10 – device-info / init query
BREQUEST_IO = 17     # 0x11 – read / write I/O commands

BMRT_READ = 0x42     # vendor, OUT, device (sends 8-byte cmd, then bulk IN)
BMRT_WRITE = 0xC2    # vendor, OUT, device (sends 8-byte cmd, then bulk OUT)
BMRT_INFO = 0xC2     # same byte as WRITE; distinguished by wLength=31

EP_BULK_OUT = 0x02   # Host → Device  (write data)
EP_BULK_IN = 0x82    # Device → Host  (read data)

SECTOR_SIZE = 512
DEFAULT_USB_TIMEOUT = 5000  # ms
MAX_CHUNK_SECTORS = 32      # 32 sectors = 16 KiB — matches XP driver behaviour

INFO_RESPONSE_LEN = 31


# ── Data structures ─────────────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Parsed 31-byte device-info response."""

    raw: bytes
    vendor_id: int
    product_id: int
    size_param1: int   # LE DWORD at offset 0x0B (e.g. 0x0800 = 2048)
    size_param2: int   # LE DWORD at offset 0x0F (e.g. 0x20 = 32)
    total_sectors: int # size_param1 * size_param2
    total_bytes: int   # total_sectors * 512

    @classmethod
    def from_bytes(cls, data: bytes) -> DeviceInfo:
        """Parse the 31-byte info response returned by bRequest=16.

        Both DWORDs are **little-endian** — the XP driver reads them
        with plain x86 ``mov``/``imul`` (no bswap).  Their product is
        the total number of 512-byte sectors.
        """
        if len(data) < INFO_RESPONSE_LEN:
            raise ValueError(
                f"Info response too short: {len(data)} bytes "
                f"(expected {INFO_RESPONSE_LEN})"
            )
        vid = (data[2] << 8) | data[3]          # bytes 2-3
        pid = (data[4] << 8) | data[5]          # bytes 4-5
        size1 = struct.unpack_from("<I", data, 0x0B)[0]  # little-endian
        size2 = struct.unpack_from("<I", data, 0x0F)[0]  # little-endian
        total_sectors = size1 * size2
        return cls(
            raw=bytes(data),
            vendor_id=vid,
            product_id=pid,
            size_param1=size1,
            size_param2=size2,
            total_sectors=total_sectors,
            total_bytes=total_sectors * SECTOR_SIZE,
        )

    def __str__(self) -> str:
        return (
            f"TrekDevice VID={self.vendor_id:#06x} PID={self.product_id:#06x}  "
            f"capacity={self.total_bytes} bytes "
            f"({self.total_bytes // (1024 * 1024)} MB, "
            f"{self.total_sectors} sectors)"
        )


# ── USB transport (thin pyusb wrapper) ──────────────────────────────

class UsbTransport:
    """Thin wrapper around pyusb that isolates all raw USB calls.

    Responsible for:
      - finding and configuring the device
      - issuing control transfers (in/out)
      - issuing bulk reads / writes
      - releasing the device on close
    """

    def __init__(
        self,
        dev: usb.core.Device,
        timeout: int = DEFAULT_USB_TIMEOUT,
    ) -> None:
        self._dev = dev
        self._timeout = timeout

    # ── factory ─────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        vid: int = VENDOR_ID,
        pid: int = PRODUCT_ID,
        timeout: int = DEFAULT_USB_TIMEOUT,
    ) -> UsbTransport:
        """Find device by VID/PID, activate first configuration."""
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(
                f"USB device not found (VID={vid:#06x}, PID={pid:#06x})"
            )
        dev.set_configuration()
        logger.info("USB configuration set (VID=%#06x PID=%#06x)", vid, pid)
        return cls(dev, timeout)

    # ── transfers ───────────────────────────────────────────────────

    def control_out(self, bm_request_type: int, b_request: int,
                    data: bytes, w_value: int = 0, w_index: int = 0) -> int:
        """Control transfer host→device (send *data*)."""
        return self._dev.ctrl_transfer(
            bmRequestType=bm_request_type,
            bRequest=b_request,
            wValue=w_value,
            wIndex=w_index,
            data_or_wLength=data,
            timeout=self._timeout,
        )

    def control_in(self, bm_request_type: int, b_request: int,
                   length: int, w_value: int = 0, w_index: int = 0) -> bytes:
        """Control transfer device→host (receive *length* bytes)."""
        raw = self._dev.ctrl_transfer(
            bmRequestType=bm_request_type,
            bRequest=b_request,
            wValue=w_value,
            wIndex=w_index,
            data_or_wLength=length,
            timeout=self._timeout,
        )
        return bytes(raw)

    def bulk_read(self, endpoint: int, length: int) -> bytes:
        """Bulk IN transfer — read *length* bytes from *endpoint*."""
        raw = self._dev.read(endpoint, length, timeout=self._timeout)
        return bytes(raw)

    def bulk_write(self, endpoint: int, data: bytes) -> int:
        """Bulk OUT transfer — write *data* to *endpoint*. Returns bytes written."""
        return self._dev.write(endpoint, data, timeout=self._timeout)

    # ── lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass
        logger.info("USB transport closed")

    def __enter__(self) -> UsbTransport:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── Abstract block-device interface ─────────────────────────────────

class BlockDevice(ABC):
    """Minimal block-device interface consumed by the NBD server."""

    @property
    @abstractmethod
    def sector_size(self) -> int: ...

    @property
    @abstractmethod
    def total_sectors(self) -> int: ...

    @property
    def capacity(self) -> int:
        """Total device size in bytes."""
        return self.sector_size * self.total_sectors

    @abstractmethod
    def read_blocks(self, lba: int, count: int) -> bytes:
        """Read *count* sectors starting at *lba*. Returns count*sector_size bytes."""
        ...

    @abstractmethod
    def write_blocks(self, lba: int, count: int, data: bytes) -> None:
        """Write *count* sectors starting at *lba*."""
        ...

    @abstractmethod
    def close(self) -> None: ...


# ── Trek USB implementation ─────────────────────────────────────────

class TrekDevice(BlockDevice):
    """Userspace driver for the Trek ThumbDrive.

    Wraps a `UsbTransport` and speaks the Trek vendor protocol:
      - init:   bRequest=16, 0xC2 control-in  → 31-byte DeviceInfo
      - read:   bRequest=17, 0x42 control-out (8-byte cmd) + bulk-in
      - write:  bRequest=17, 0xC2 control-out (8-byte cmd) + bulk-out
      - close:  release transport

    Provides **two I/O interfaces**:
      sector-level : read_blocks(lba, count) / write_blocks(lba, count, data)
      byte-level   : read(offset, length)    / write(offset, data)
    """

    def __init__(self, usb: UsbTransport, info: DeviceInfo) -> None:
        self._usb = usb
        self._info = info

    # ── construction helpers ────────────────────────────────────────

    @classmethod
    def open(
        cls,
        vid: int = VENDOR_ID,
        pid: int = PRODUCT_ID,
        timeout: int = DEFAULT_USB_TIMEOUT,
    ) -> TrekDevice:
        """Find the device, set configuration, query info, return ready instance."""
        transport = UsbTransport.open(vid, pid, timeout)
        try:
            info = cls._query_device_info(transport)
            logger.info("Device initialised: %s", info)
            return cls(transport, info)
        except Exception:
            transport.close()
            raise

    @staticmethod
    def _query_device_info(transport: UsbTransport) -> DeviceInfo:
        """Send the bRequest=16 init/info control-in transfer."""
        raw = transport.control_in(
            bm_request_type=BMRT_INFO,
            b_request=BREQUEST_INIT,
            length=INFO_RESPONSE_LEN,
        )
        return DeviceInfo.from_bytes(raw)

    # ── BlockDevice properties ──────────────────────────────────────

    @property
    def sector_size(self) -> int:
        return SECTOR_SIZE

    @property
    def total_sectors(self) -> int:
        return self._info.total_sectors

    @property
    def info(self) -> DeviceInfo:
        return self._info

    # ── I/O (low-level: single USB transaction) ──────────────────────

    def _build_command(self, lba: int, count: int) -> bytes:
        """Build the 8-byte command payload: [LBA:u32le][Count:u32le]."""
        return struct.pack("<II", lba, count)

    def _read_chunk(self, lba: int, count: int) -> bytes:
        """Issue one control-out + bulk-in for ≤ MAX_CHUNK_SECTORS sectors."""
        expected = count * SECTOR_SIZE
        cmd = self._build_command(lba, count)
        self._usb.control_out(BMRT_READ, BREQUEST_IO, cmd)
        data = self._usb.bulk_read(EP_BULK_IN, expected)
        if len(data) != expected:
            logger.warning(
                "Short read: requested %d bytes, got %d (lba=%d, count=%d)",
                expected, len(data), lba, count,
            )
        return data

    def _write_chunk(self, lba: int, count: int, data: bytes) -> None:
        """Issue one control-out + bulk-out for ≤ MAX_CHUNK_SECTORS sectors."""
        expected = count * SECTOR_SIZE
        cmd = self._build_command(lba, count)
        self._usb.control_out(BMRT_WRITE, BREQUEST_IO, cmd)
        written = self._usb.bulk_write(EP_BULK_OUT, data)
        if written != expected:
            logger.warning(
                "Short write: sent %d/%d bytes (lba=%d, count=%d)",
                written, expected, lba, count,
            )

    # ── I/O (public: chunked + bounds-checked) ─────────────────────

    def read_blocks(self, lba: int, count: int) -> bytes:
        """Read *count* 512-byte sectors starting at *lba*.

        Large requests are automatically split into ≤ MAX_CHUNK_SECTORS
        (32 sector / 16 KiB) USB transactions, matching the transfer
        pattern observed in XP driver captures.

        Raises ValueError if the request exceeds device bounds.
        """
        if count <= 0:
            return b""
        if lba < 0 or lba + count > self._info.total_sectors:
            raise ValueError(
                f"read_blocks(lba={lba}, count={count}) out of range "
                f"(device has {self._info.total_sectors} sectors)"
            )

        parts: list[bytes] = []
        remaining = count
        cur_lba = lba
        while remaining > 0:
            chunk = min(remaining, MAX_CHUNK_SECTORS)
            parts.append(self._read_chunk(cur_lba, chunk))
            cur_lba += chunk
            remaining -= chunk
        return b"".join(parts)

    def write_blocks(self, lba: int, count: int, data: bytes) -> None:
        """Write *count* 512-byte sectors starting at *lba*.

        Large requests are automatically split into ≤ MAX_CHUNK_SECTORS
        (32 sector / 16 KiB) USB transactions.

        Raises ValueError if data length doesn't match or request is
        out of range.
        """
        expected = count * SECTOR_SIZE
        if len(data) != expected:
            raise ValueError(
                f"Data length mismatch: expected {expected} bytes, got {len(data)}"
            )
        if lba < 0 or lba + count > self._info.total_sectors:
            raise ValueError(
                f"write_blocks(lba={lba}, count={count}) out of range "
                f"(device has {self._info.total_sectors} sectors)"
            )

        offset = 0
        remaining = count
        cur_lba = lba
        while remaining > 0:
            chunk = min(remaining, MAX_CHUNK_SECTORS)
            chunk_bytes = chunk * SECTOR_SIZE
            self._write_chunk(cur_lba, chunk, data[offset : offset + chunk_bytes])
            cur_lba += chunk
            offset += chunk_bytes
            remaining -= chunk

    # ── byte-addressed convenience I/O ──────────────────────────────

    def read(self, offset: int, length: int) -> bytes:
        """Read *length* bytes starting at byte *offset*.

        Translates the byte range into aligned sector reads and slices
        the result.  Raises ValueError on out-of-range access.
        """
        if length <= 0:
            return b""
        if offset < 0 or offset + length > self.capacity:
            raise ValueError(
                f"read({offset}, {length}) exceeds device capacity {self.capacity}"
            )
        first_lba = offset // SECTOR_SIZE
        last_lba = (offset + length - 1) // SECTOR_SIZE
        count = last_lba - first_lba + 1

        raw = self.read_blocks(first_lba, count)

        start = offset - first_lba * SECTOR_SIZE
        return raw[start : start + length]

    def write(self, offset: int, data: bytes) -> None:
        """Write *data* at byte *offset*.

        Handles sector alignment: if the write is not sector-aligned
        at either end, the boundary sectors are read-modified-written.
        """
        length = len(data)
        if length == 0:
            return
        if offset < 0 or offset + length > self.capacity:
            raise ValueError(
                f"write({offset}, {length}B) exceeds device capacity {self.capacity}"
            )
        first_lba = offset // SECTOR_SIZE
        last_lba = (offset + length - 1) // SECTOR_SIZE
        count = last_lba - first_lba + 1

        start_within = offset - first_lba * SECTOR_SIZE
        aligned = (start_within == 0) and (length % SECTOR_SIZE == 0)

        if aligned:
            # Fast path: data already sector-aligned
            self.write_blocks(first_lba, count, data)
        else:
            # Slow path: read-modify-write boundary sectors
            buf = bytearray(self.read_blocks(first_lba, count))
            buf[start_within : start_within + length] = data
            self.write_blocks(first_lba, count, bytes(buf))

    # ── lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        """Release the USB transport."""
        self._usb.close()
        logger.info("Device closed")

    # ── utilities ───────────────────────────────────────────────────

    def dump_image(self, path: str, progress: bool = True) -> None:
        """Read the entire device and write a raw disk image to *path*.

        Reads in MAX_CHUNK_SECTORS-sector chunks.  If *progress* is True,
        prints a progress line to stderr every 64 chunks (~1 MiB).
        """
        import sys
        total = self._info.total_sectors
        written = 0
        with open(path, "wb") as f:
            while written < total:
                chunk = min(MAX_CHUNK_SECTORS, total - written)
                data = self.read_blocks(written, chunk)
                f.write(data)
                written += chunk
                if progress and written % (MAX_CHUNK_SECTORS * 64) == 0:
                    pct = written * 100 // total
                    print(
                        f"\r  {written}/{total} sectors ({pct}%)",
                        end="", file=sys.stderr, flush=True,
                    )
        if progress:
            print(
                f"\r  {total}/{total} sectors (100%)  ",
                file=sys.stderr, flush=True,
            )
        logger.info("Dump complete: %s (%d bytes)", path, total * SECTOR_SIZE)

    def __enter__(self) -> TrekDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<TrekDevice {self._info}>"


# ── File-image implementation (for testing without hardware) ────────

class FileBlockDevice(BlockDevice):
    """Block-device backed by a plain file (drop-in for NBD testing)."""

    def __init__(self, path: str, sector_size: int = SECTOR_SIZE) -> None:
        import os
        self._path = path
        self._sector_size = sector_size
        self._fd = open(path, "rb+")
        size = os.path.getsize(path)
        self._total_sectors = size // sector_size

    @property
    def sector_size(self) -> int:
        return self._sector_size

    @property
    def total_sectors(self) -> int:
        return self._total_sectors

    def read_blocks(self, lba: int, count: int) -> bytes:
        self._fd.seek(lba * self._sector_size)
        return self._fd.read(count * self._sector_size)

    def write_blocks(self, lba: int, count: int, data: bytes) -> None:
        self._fd.seek(lba * self._sector_size)
        self._fd.write(data)
        self._fd.flush()

    def close(self) -> None:
        self._fd.close()

    def __enter__(self) -> FileBlockDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── CLI smoke-test ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Trek ThumbDrive utility")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("info", help="Print device info")

    p_read = sub.add_parser("read", help="Read sectors")
    p_read.add_argument("lba", type=int, help="Starting LBA")
    p_read.add_argument("count", type=int, help="Number of sectors")
    p_read.add_argument("-o", "--output", help="Write raw data to file")

    p_dump = sub.add_parser("dump", help="Dump entire device to image file")
    p_dump.add_argument("output", help="Output file path")

    args = parser.parse_args()

    with TrekDevice.open() as dev:
        if args.cmd == "info" or args.cmd is None:
            print(dev.info)
            print(f"Capacity: {dev.capacity} bytes")
            mbr = dev.read_blocks(0, 1)
            print(f"MBR first 16 bytes: {mbr[:16].hex(':')}")

        elif args.cmd == "read":
            data = dev.read_blocks(args.lba, args.count)
            if args.output:
                with open(args.output, "wb") as f:
                    f.write(data)
                print(f"Wrote {len(data)} bytes to {args.output}")
            else:
                for i in range(args.count):
                    sec = data[i * 512 : (i + 1) * 512]
                    print(f"sector {args.lba + i}: {sec[:32].hex(':')}")

        elif args.cmd == "dump":
            dev.dump_image(args.output)
