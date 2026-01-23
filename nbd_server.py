#!/usr/bin/env python3
"""
Simple NBD (Network Block Device) Server
Serves disk.img file with 0x2000 byte blocks
"""

import socket
import struct
import logging
import os
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Hardcoded configuration
DISK_IMAGE = "disk.img"
BLOCK_SIZE = 0x2000  # 8192 bytes
NBD_PORT = 10809

# NBD Protocol Constants
NBD_MAGIC = 0x4e42444d41474943  # "NBDMAGIC"
NBD_OPTS_MAGIC = 0x49484156454F5054  # "IHAVEOPT"
NBD_CLISERV_MAGIC = 0x3E889045565A9  # Client-server magic
NBD_REQUEST_MAGIC = 0x25609513
NBD_REPLY_MAGIC = 0x67446698

# NBD Commands
NBD_CMD_READ = 0
NBD_CMD_WRITE = 1
NBD_CMD_DISC = 2
NBD_CMD_FLUSH = 3
NBD_CMD_TRIM = 4

# NBD Reply flags
NBD_REPLY_FLAG_DONE = (1 << 0)

# NBD Handshake flags
NBD_FLAG_FIXED_NEWSTYLE = (1 << 0)
NBD_FLAG_NO_ZEROES = (1 << 1)

# NBD Transmission flags
NBD_FLAG_HAS_FLAGS = (1 << 0)
NBD_FLAG_READ_ONLY = (1 << 1)
NBD_FLAG_SEND_FLUSH = (1 << 2)
NBD_FLAG_SEND_FUA = (1 << 3)
NBD_FLAG_ROTATIONAL = (1 << 4)
NBD_FLAG_SEND_TRIM = (1 << 5)


class NBDServer:
    def __init__(self, disk_image, block_size):
        self.disk_image = disk_image
        self.block_size = block_size
        self.disk_size = os.path.getsize(disk_image)
        self.disk_fd = None
        
        logger.info(f"Initializing NBD server for {disk_image}")
        logger.info(f"Disk size: {self.disk_size} bytes")
        logger.info(f"Block size: {self.block_size} bytes (0x{self.block_size:x})")
    
    def open_disk(self):
        """Open the disk image file"""
        self.disk_fd = open(self.disk_image, 'rb+')
        logger.info(f"Opened disk image: {self.disk_image}")
    
    def close_disk(self):
        """Close the disk image file"""
        if self.disk_fd:
            self.disk_fd.close()
            logger.info("Closed disk image")
    
    def send_all(self, sock, data):
        """Send all data to socket"""
        sock.sendall(data)
    
    def recv_all(self, sock, size):
        """Receive exactly size bytes from socket"""
        data = b''
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("Connection closed")
            data += chunk
        return data
    
    def handshake(self, client_sock):
        """Perform NBD handshake with client"""
        logger.info("Starting NBD handshake")
        
        # Send initial greeting: oldstyle magic + opts magic
        greeting = struct.pack('>QQ', NBD_MAGIC, NBD_OPTS_MAGIC)
        # Send handshake flags
        handshake_flags = NBD_FLAG_FIXED_NEWSTYLE | NBD_FLAG_NO_ZEROES
        greeting += struct.pack('>H', handshake_flags)
        self.send_all(client_sock, greeting)
        logger.info("Sent NBD greeting")
        
        # Receive client flags
        client_flags = struct.unpack('>I', self.recv_all(client_sock, 4))[0]
        logger.info(f"Received client flags: 0x{client_flags:x}")
        
        # Simple negotiation - wait for NBD_OPT_EXPORT_NAME or NBD_OPT_GO
        while True:
            # Receive option header: magic + option + length
            opt_data = self.recv_all(client_sock, 16)
            magic, option, length = struct.unpack('>QII', opt_data)
            
            logger.info(f"Received option: magic=0x{magic:x}, option={option}, length={length}")
            
            if length > 0:
                export_name = self.recv_all(client_sock, length)
                logger.info(f"Export name: {export_name.decode('utf-8', errors='ignore')}")
            
            # NBD_OPT_EXPORT_NAME = 1
            # NBD_OPT_GO = 7
            if option == 1:  # NBD_OPT_EXPORT_NAME
                # Send export size and flags
                transmission_flags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH
                response = struct.pack('>QH', self.disk_size, transmission_flags)
                # Pad with 124 zeros if client doesn't support NO_ZEROES
                if not (client_flags & NBD_FLAG_NO_ZEROES):
                    response += b'\x00' * 124
                self.send_all(client_sock, response)
                logger.info(f"Sent export info: size={self.disk_size}, flags=0x{transmission_flags:x}")
                break
            elif option == 7:  # NBD_OPT_GO
                # Send INFO replies with export size
                transmission_flags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_SEND_FLUSH
                
                # Payload: type(16)=NBD_INFO_EXPORT(0), size(64), flags(16)
                info_payload = struct.pack('>HQH', 0, self.disk_size, transmission_flags)
                
                # Send INFO reply header: magic, option, type(3=INFO), length
                info_header = struct.pack('>QIII', 
                    NBD_CLISERV_MAGIC, 
                    option, 
                    3,  # NBD_REP_INFO
                    len(info_payload)
                )
                self.send_all(client_sock, info_header + info_payload)
                
                # Send NBD_REP_ACK (1) to finish
                reply = struct.pack('>QIII', NBD_CLISERV_MAGIC, option, 1, 0)
                self.send_all(client_sock, reply)
                
                logger.info(f"Sent NBD_OPT_GO reply")
                break
            else:
                # Unsupported option, send error
                reply = struct.pack('>QIII', NBD_CLISERV_MAGIC, option, 1, 0)  # Error
                self.send_all(client_sock, reply)
                logger.warning(f"Unsupported option: {option}")
        
        logger.info("Handshake completed")
        return True
    
    def handle_request(self, client_sock):
        """Handle a single NBD request"""
        try:
            # Read request header
            header = self.recv_all(client_sock, 28)
            magic, flags, cmd_type, cookie, offset, length = struct.unpack('>IHHQQI', header)
            
            if magic != NBD_REQUEST_MAGIC:
                logger.error(f"Invalid request magic: 0x{magic:x}")
                return False
            
            logger.info(f"Request: cmd={cmd_type}, flags={flags}, offset={offset}, length={length}, cookie={cookie}")
            
            if cmd_type == NBD_CMD_READ:
                # Read data from disk
                self.disk_fd.seek(offset)
                data = self.disk_fd.read(length)
                
                # Send reply: magic + error + handle + data
                reply = struct.pack('>IIQ', NBD_REPLY_MAGIC, 0, cookie)
                self.send_all(client_sock, reply + data)
                logger.info(f"READ: offset={offset}, length={length} bytes - SUCCESS")
                
            elif cmd_type == NBD_CMD_WRITE:
                # Receive data to write
                data = self.recv_all(client_sock, length)
                
                # Write data to disk
                self.disk_fd.seek(offset)
                self.disk_fd.write(data)
                self.disk_fd.flush()
                
                # Send reply
                reply = struct.pack('>IIQ', NBD_REPLY_MAGIC, 0, cookie)
                self.send_all(client_sock, reply)
                logger.info(f"WRITE: offset={offset}, length={length} bytes - SUCCESS")
                
            elif cmd_type == NBD_CMD_DISC:
                logger.info("Received disconnect command")
                return False
                
            elif cmd_type == NBD_CMD_FLUSH:
                # Flush disk
                self.disk_fd.flush()
                os.fsync(self.disk_fd.fileno())
                
                # Send reply
                reply = struct.pack('>IIQ', NBD_REPLY_MAGIC, 0, cookie)
                self.send_all(client_sock, reply)
                logger.info("FLUSH - SUCCESS")
                
            else:
                logger.warning(f"Unsupported command: {cmd_type}")
                # Send error reply
                reply = struct.pack('>IIQ', NBD_REPLY_MAGIC, 1, cookie)
                self.send_all(client_sock, reply)
            
            return True
            
        except Exception as e:
            logger.error(f"Error handling request: {e}")
            return False
    
    def handle_client(self, client_sock, client_addr):
        """Handle a client connection"""
        logger.info(f"Client connected from {client_addr}")
        
        try:
            # Perform handshake
            if not self.handshake(client_sock):
                logger.error("Handshake failed")
                return
            
            # Open disk image
            self.open_disk()
            
            # Handle requests in a loop
            while True:
                if not self.handle_request(client_sock):
                    break
            
        except Exception as e:
            logger.error(f"Error handling client: {e}")
        finally:
            self.close_disk()
            client_sock.close()
            logger.info(f"Client disconnected from {client_addr}")
    
    def serve(self, host='0.0.0.0', port=NBD_PORT):
        """Start the NBD server"""
        logger.info(f"Starting NBD server on {host}:{port}")
        
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((host, port))
        server_sock.listen(1)
        
        logger.info(f"NBD server listening on {host}:{port}")
        logger.info(f"Connect with: nbd-client {host} {port} /dev/nbd0")
        
        try:
            while True:
                client_sock, client_addr = server_sock.accept()
                self.handle_client(client_sock, client_addr)
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
        finally:
            server_sock.close()


def main():
    # Check if disk image exists
    if not os.path.exists(DISK_IMAGE):
        logger.error(f"Disk image not found: {DISK_IMAGE}")
        sys.exit(1)
    
    # Create and start server
    server = NBDServer(DISK_IMAGE, BLOCK_SIZE)
    server.serve()


if __name__ == "__main__":
    main()
