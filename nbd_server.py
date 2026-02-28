#!/usr/bin/env python3
"""
NBD (Network Block Device) Server for Trek ThumbDrive

Exposes a BlockDevice (Trek USB stick or file image) as an NBD export.

Usage:
  # Serve the Trek ThumbDrive directly:
  sudo python nbd_server.py --trek

  # Serve a file image (original behaviour):
  python nbd_server.py --file disk.img

  # Then on the client side:
  sudo modprobe nbd
  sudo nbd-client localhost 10809 /dev/nbd0
  sudo mount /dev/nbd0 /mnt
"""

import argparse
import logging
import os
import socket
import struct
import sys

from trek_usb import BlockDevice, FileBlockDevice, TrekDevice, SECTOR_SIZE

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── NBD constants ───────────────────────────────────────────────────
NBD_PORT = 10809

NBD_MAGIC           = 0x4E42444D41474943  # "NBDMAGIC"
NBD_OPTS_MAGIC      = 0x49484156454F5054  # "IHAVEOPT"
NBD_CLISERV_MAGIC   = 0x3E889045565A9
NBD_REQUEST_MAGIC   = 0x25609513
NBD_REPLY_MAGIC     = 0x67446698

NBD_CMD_READ  = 0
NBD_CMD_WRITE = 1
NBD_CMD_DISC  = 2
NBD_CMD_FLUSH = 3
NBD_CMD_TRIM  = 4

NBD_FLAG_FIXED_NEWSTYLE = 1 << 0
NBD_FLAG_NO_ZEROES      = 1 << 1

NBD_FLAG_HAS_FLAGS   = 1 << 0
NBD_FLAG_READ_ONLY   = 1 << 1
NBD_FLAG_SEND_FLUSH  = 1 << 2

NBD_REP_INFO = 3
NBD_REP_ACK  = 1

# NBD error codes
NBD_EIO    = 5   # I/O error
NBD_EINVAL = 22  # Invalid argument


# ── NBD Server ──────────────────────────────────────────────────────

class NBDServer:
    """NBD server that delegates I/O to any :class:`BlockDevice`."""

    def __init__(self, device: BlockDevice, read_only: bool = False) -> None:
        self._dev = device
        self._read_only = read_only

        logger.info(
            "NBD server: capacity=%d bytes (%d MB), sector_size=%d, sectors=%d, ro=%s",
            device.capacity,
            device.capacity // (1024 * 1024),
            device.sector_size,
            device.total_sectors,
            read_only,
        )

    # ── socket helpers ──────────────────────────────────────────────

    @staticmethod
    def _send(sock: socket.socket, data: bytes) -> None:
        sock.sendall(data)

    @staticmethod
    def _recv(sock: socket.socket, size: int) -> bytes:
        buf = bytearray()
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    # ── handshake ───────────────────────────────────────────────────

    def _handshake(self, sock: socket.socket) -> bool:
        disk_size = self._dev.capacity

        # Greeting
        greeting = struct.pack(">QQ", NBD_MAGIC, NBD_OPTS_MAGIC)
        greeting += struct.pack(">H", NBD_FLAG_FIXED_NEWSTYLE | NBD_FLAG_NO_ZEROES)
        self._send(sock, greeting)

        # Client flags
        client_flags = struct.unpack(">I", self._recv(sock, 4))[0]
        logger.debug("Client flags: 0x%x", client_flags)

        # Option negotiation loop
        while True:
            header = self._recv(sock, 16)
            magic, option, length = struct.unpack(">QII", header)

            payload = self._recv(sock, length) if length else b""

            if option == 1:  # NBD_OPT_EXPORT_NAME
                tx_flags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH
                if self._read_only:
                    tx_flags |= NBD_FLAG_READ_ONLY
                resp = struct.pack(">QH", disk_size, tx_flags)
                if not (client_flags & NBD_FLAG_NO_ZEROES):
                    resp += b"\x00" * 124
                self._send(sock, resp)
                break

            elif option == 7:  # NBD_OPT_GO
                tx_flags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH
                if self._read_only:
                    tx_flags |= NBD_FLAG_READ_ONLY
                info = struct.pack(">HQH", 0, disk_size, tx_flags)
                hdr = struct.pack(">QIII", NBD_CLISERV_MAGIC, option, NBD_REP_INFO, len(info))
                self._send(sock, hdr + info)
                # ACK
                self._send(sock, struct.pack(">QIII", NBD_CLISERV_MAGIC, option, NBD_REP_ACK, 0))
                break

            else:
                # Unsupported → ACK (benign)
                self._send(sock, struct.pack(">QIII", NBD_CLISERV_MAGIC, option, NBD_REP_ACK, 0))
                logger.warning("Unsupported option %d — sent ACK", option)

        logger.info("Handshake complete (export size=%d)", disk_size)
        return True

    # ── request handling ────────────────────────────────────────────

    def _handle_request(self, sock: socket.socket) -> bool:
        """Process one NBD request.  Returns False on disconnect."""
        header = self._recv(sock, 28)
        magic, flags, cmd, cookie, offset, length = struct.unpack(">IHHQQI", header)

        if magic != NBD_REQUEST_MAGIC:
            logger.error("Bad request magic: 0x%x", magic)
            return False

        sector_size = self._dev.sector_size

        if cmd == NBD_CMD_READ:
            try:
                if offset % sector_size or length % sector_size:
                    data = self._byte_read(offset, length)
                else:
                    lba = offset // sector_size
                    count = length // sector_size
                    data = self._dev.read_blocks(lba, count)
                reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, 0, cookie)
                self._send(sock, reply + data)
            except Exception as e:
                logger.error("READ error at offset=%d len=%d: %s", offset, length, e)
                reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, NBD_EIO, cookie)
                self._send(sock, reply + b"\x00" * length)

        elif cmd == NBD_CMD_WRITE:
            data = self._recv(sock, length)
            if self._read_only:
                reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, NBD_EINVAL, cookie)
                self._send(sock, reply)
            else:
                try:
                    if offset % sector_size or length % sector_size:
                        self._byte_write(offset, data)
                    else:
                        lba = offset // sector_size
                        count = length // sector_size
                        self._dev.write_blocks(lba, count, data)
                    reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, 0, cookie)
                    self._send(sock, reply)
                except Exception as e:
                    logger.error("WRITE error at offset=%d len=%d: %s", offset, length, e)
                    reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, NBD_EIO, cookie)
                    self._send(sock, reply)

        elif cmd == NBD_CMD_DISC:
            logger.info("Client disconnected")
            return False

        elif cmd == NBD_CMD_FLUSH:
            reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, 0, cookie)
            self._send(sock, reply)

        else:
            logger.warning("Unsupported command %d", cmd)
            reply = struct.pack(">IIQ", NBD_REPLY_MAGIC, NBD_EINVAL, cookie)
            self._send(sock, reply)

        return True

    # ── byte-level helpers for unaligned NBD requests ───────────────

    def _byte_read(self, offset: int, length: int) -> bytes:
        ss = self._dev.sector_size
        first_lba = offset // ss
        last_lba = (offset + length - 1) // ss
        count = last_lba - first_lba + 1
        raw = self._dev.read_blocks(first_lba, count)
        start = offset - first_lba * ss
        return raw[start : start + length]

    def _byte_write(self, offset: int, data: bytes) -> None:
        ss = self._dev.sector_size
        length = len(data)
        first_lba = offset // ss
        last_lba = (offset + length - 1) // ss
        count = last_lba - first_lba + 1
        start = offset - first_lba * ss
        aligned = (start == 0) and (length % ss == 0)
        if aligned:
            self._dev.write_blocks(first_lba, count, data)
        else:
            buf = bytearray(self._dev.read_blocks(first_lba, count))
            buf[start : start + length] = data
            self._dev.write_blocks(first_lba, count, bytes(buf))

    # ── client / server loops ───────────────────────────────────────

    def _handle_client(self, sock: socket.socket, addr) -> None:
        logger.info("Client connected from %s", addr)
        try:
            if not self._handshake(sock):
                return
            while self._handle_request(sock):
                pass
        except ConnectionError:
            logger.info("Client connection lost")
        except Exception as e:
            logger.error("Client error: %s", e)
        finally:
            sock.close()
            logger.info("Client session ended")

    def serve(self, host: str = "0.0.0.0", port: int = NBD_PORT) -> None:
        """Listen for NBD clients in a loop (single-threaded)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)

        logger.info("NBD server listening on %s:%d", host, port)
        logger.info("Connect with:  sudo nbd-client %s %d /dev/nbd0", host, port)

        try:
            while True:
                client_sock, client_addr = srv.accept()
                self._handle_client(client_sock, client_addr)
        except KeyboardInterrupt:
            logger.info("Server stopped")
        finally:
            srv.close()
            self._dev.close()


# ── CLI ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NBD server backed by Trek ThumbDrive or file image",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--trek", action="store_true",
        help="Use the Trek ThumbDrive (requires USB device present)",
    )
    group.add_argument(
        "--file", metavar="PATH",
        help="Use a raw disk image file",
    )
    parser.add_argument(
        "--port", type=int, default=NBD_PORT,
        help=f"TCP port (default {NBD_PORT})",
    )
    parser.add_argument(
        "--ro", action="store_true",
        help="Export as read-only",
    )
    args = parser.parse_args()

    if args.trek:
        device: BlockDevice = TrekDevice.open()
    else:
        if not os.path.exists(args.file):
            logger.error("File not found: %s", args.file)
            sys.exit(1)
        device = FileBlockDevice(args.file)

    server = NBDServer(device, read_only=args.ro)
    server.serve(port=args.port)


if __name__ == "__main__":
    main()
