# Trek ThumbDrive USB Protocol Documentation

## Device Information

- **Vendor ID:** 0x0a16 (Trek Technology)
- **Product ID:** 0x1111 (ThumbDrive)
- **Device Class:** 0xFF (Vendor Specific)
- **Interface Class:** 0xFF (Vendor Specific)
- **Endpoints:**
  - 0x02: Bulk OUT (Host → Device) - Write operations
  - 0x82: Bulk IN (Device → Host) - Read operations
  - Max Packet Size: 64 bytes

## Device Capacity

**Total Storage:** 32 MB (calculated from device info response)

### Capacity Detection Response

**Important:** This is a vendor-specific initialization query that must occur **after standard USB enumeration** and **before any read/write operations**.

Control transfer parameters:
- **bRequest:** 16 (0x10) - **Note: Different from I/O operations which use bRequest=17**
- **bmRequestType:** 0xC2 (Class request, IN direction, Endpoint recipient)
- **wValue:** 0x0000
- **wIndex:** 0x0000
- **wLength:** 31 bytes

**Response Payload (31 bytes):**
```
1f:16:0a:11:11:10:01:02:00:20:00:00:08:00:00:20:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00
```

**Payload Structure:**
| Offset | Length | Value (hex) | Description |
|--------|--------|-------------|-------------|
| 0x00 | 2 | `1f:16` | Response length (31 bytes) |
| 0x02 | 2 | `0a:16` | Vendor ID (little-endian: 0x0a16) |
| 0x04 | 2 | `11:11` | Product ID (0x1111) |
| 0x06 | 4 | `10:01:02:00` | Unknown/Reserved |
| 0x0A | 1 | `20` | Unknown |
| **0x0B** | **4** | **`00:08:00:00`** | **Size parameter 1 (little-endian: 0x00000800 = 2,048)** |
| **0x0F** | **4** | **`20:00:00:00`** | **Size parameter 2 (little-endian: 0x00000020 = 32)** |
| 0x13 | 18 | `00:00:00:...` | Padding/Reserved |

**Capacity Calculation (from driver disassembly):**

Both DWORDs are **little-endian** (native x86 word order — the XP driver
uses plain `mov`/`imul`, no `bswap`).  Their product is the **total
sector count**; multiply by 512 for bytes.

```
Sectors = (DWORD at 0x0B) * (DWORD at 0x0F)
        = 0x00000800 * 0x00000020
        = 2,048 * 32
        = 65,536 sectors (0x10000)

Bytes   = 65,536 * 512
        = 33,554,432 (0x2000000)
        = 32 MB total capacity
```

## Block/Sector Size

- **Logical Block Size:** 512 bytes
- **Typical Transfer Size:** 8192 bytes (16 blocks)
- **Total Blocks:** 65,536 blocks (32 MB / 512 bytes)

## Command Protocol

### Command Structure

All disk operations use **vendor-specific control transfers** followed by **bulk transfers**.

**Control Transfer Setup:**
- **bmRequestType:** 
  - `0x42` for READ operations (vendor, IN, device-to-host)
  - `0xC2` for WRITE operations (vendor, OUT, host-to-device)
  - `0xC2` also used for device info query (distinguished by wLength)
- **bRequest:** 17 (vendor-specific command)
- **wValue:** 0x0000
- **wIndex:** 0x0000
- **wLength:** 
  - **8 bytes** for READ/WRITE commands (sends command payload)
  - **31 bytes** for device info query (requests info response)

**Command Payload (8 bytes):**
```
Byte Offset | Field        | Type               | Description
------------|--------------|--------------------|---------------------------------
0x00 - 0x03 | LBA          | uint32 (LE)        | Logical Block Address
0x04 - 0x07 | Block Count  | uint32 (LE)        | Number of 512-byte blocks
```

- **LBA (Logical Block Address):** Starting block number (0-based)
- **Block Count:** Number of consecutive 512-byte blocks to read/write
- **Byte Offset:** LBA × 512
- **Transfer Size:** Block Count × 512 bytes

### READ Operation

**Sequence:**
1. **Control Transfer OUT** (bmRequestType=0x42, bRequest=17)
   - Send 8-byte command: `[LBA:4][Count:4]`
2. **Bulk Transfer IN** (endpoint 0x82)
   - Receive `Count × 512` bytes of data

**Example: Read 16 KB starting at offset 0**
```
Control Transfer:
  Command: 00:00:00:00:20:00:00:00
           |_LBA=0___|_Count=32_|
  
Bulk Transfer IN (endpoint 0x82):
  Receive: 16,384 bytes (32 blocks × 512 bytes)
```

**Example: Read Master Boot Record (first 512 bytes)**
```
Control Transfer:
  Command: 00:00:00:00:01:00:00:00
           |_LBA=0___|_Count=1__|
  
Bulk Transfer IN (endpoint 0x82):
  Receive: 512 bytes
  Data starts with: 55:aa:55:aa... (MBR signature)
```

### WRITE Operation

**Note:** Uses bmRequestType=0xC2 (same as device info query) but with wLength=8 to send command, followed by bulk OUT transfer.

**Sequence:**
1. **Control Transfer OUT** (bmRequestType=0xC2, bRequest=17, wLength=8)
   - Send 8-byte command: `[LBA:4][Count:4]`
   - **Direction:** Host → Device (OUT, sends command payload)
2. **Bulk Transfer OUT** (endpoint 0x02)
   - Send `Count × 512` bytes of data
   - **Direction:** Host → Device (actual data to write)

**Example: Write 8 KB starting at block 100**
```
Control Transfer:
  Command: 64:00:00:00:10:00:00:00
           |_LBA=100_|_Count=16_|
  
Bulk Transfer OUT (endpoint 0x02):
  Send: 8,192 bytes (16 blocks × 512 bytes)
```

## Observed Read Patterns

### Sequential Read (e.g., dd operation)

From packet captures, sequential reads show monotonic LBA progression:

```
LBA    Block Count    Bytes Read    Cumulative Offset
----------------------------------------------------------
0      32             16,384        0x0000
32     32             16,384        0x4000
64     32             16,384        0x8000
96     32             16,384        0xC000
128    32             16,384        0x10000
...
```

### Random Access (e.g., filesystem operations)

Single-block reads for FAT/directory access:
```
LBA 0    → MBR (Master Boot Record)
LBA 1-32 → FAT table
LBA 33+  → Directory entries and file data
```

Multi-block reads for file data:
```
LBA 512, Count=16 → Read 8 KB file chunk
```

## Filesystem Structure

The device contains a standard FAT filesystem:

- **Sector 0:** MBR with signature `55:aa:55:aa`
- **Partition Type:** FAT12 or FAT16 (typical for small capacity)
- **Bootable:** Single FAT partition

## Special Addresses/Commands

### Device Info Query

**Note:** This operation uses the same bmRequestType (0xC2) as WRITE operations but is distinguished by the wLength parameter.

- **Control Transfer IN:** bmRequestType=0xC2, bRequest=17
- **wValue:** 0x0000
- **wIndex:** 0x0000  
- **wLength:** 31 (requests 31-byte response, NOT 8-byte command)
- **Data Direction:** Device → Host (IN transfer)
- **Response:** 31-byte device information structure (see Capacity Detection)

**Key Difference from WRITE:** The wLength=31 indicates this is an info query expecting a response, not a command setup expecting a bulk transfer.

## Implementation Notes

### USB Communication Flow

#### 1. Device Enumeration (Standard USB)

The device uses **standard USB enumeration** - no special vendor-specific steps required:

```
1. GET_DESCRIPTOR (Device, 18 bytes)
   → Returns: VID=0x0a16, PID=0x1111, bDeviceClass=0xFF
   
2. GET_DESCRIPTOR (Configuration, 9 bytes)
   → Returns: Configuration header
   
3. GET_DESCRIPTOR (Configuration, 32 bytes)
   → Returns: Full configuration with 2 endpoints (0x02, 0x82)
   
4. SET_CONFIGURATION (1)
   → Activates configuration
   
5. GET_STATUS (Device)
   → Optional: Returns 0x0000
```

**Note:** A ~100ms delay is commonly observed before requesting the full configuration descriptor, though this may not be strictly required.

#### 2. Device Initialization (Vendor-Specific)

**After enumeration completes**, the device requires a vendor-specific initialization query:

**Control Transfer IN:**
- **bmRequestType:** 0xC2 (Class request, IN direction, Endpoint recipient)
- **bRequest:** 16 (0x10) - **Different from normal I/O operations which use bRequest=17**
- **wValue:** 0x0000
- **wIndex:** 0x0000
- **wLength:** 31 bytes

**Response (31 bytes):**
```
1f:16:0a:11:11:10:01:02:00:20:00:00:08:00:00:20:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00
```

**Timing:**
- Must occur **after SET_CONFIGURATION**
- Observed delays: 4.8s to 151s after enumeration (timing not critical)
- Should be performed **before attempting any read/write operations**

**Purpose:**
- Returns device capacity and identification information
- Likely enables vendor-specific bulk I/O functionality
- Required by Windows driver before allowing disk access

#### 3. Normal Operations

Once initialization is complete:
- Use 8-byte command format for all reads/writes (bRequest=17)
- Typical transfer size: 8-16 KB (16-32 blocks)
- LBA range: 0 to 65,535 (for 32 MB device)

### Error Handling

- **USB Timeouts:** Standard USB timeout values (5000 ms recommended)
- **Invalid LBA:** Reads beyond device capacity may return zeros or stall
- **Partial Transfers:** Handle short reads/writes gracefully

### Performance Optimization

- **Bulk Transfer Size:** 8-16 KB transfers provide good performance
- **Read-Ahead:** Pre-fetch sequential blocks for better throughput
- **Write Caching:** Batch small writes when possible
- **Alignment:** Transfers aligned to 512-byte boundaries

## Comparison with USB Mass Storage Class

| Feature | USB Mass Storage | Trek ThumbDrive |
|---------|-----------------|-----------------|
| **Interface Class** | 0x08 (Mass Storage) | 0xFF (Vendor Specific) |
| **Protocol** | SCSI/UFI Bulk-Only | Vendor bRequest=17 |
| **Command Format** | 31-byte CBW | 8-byte custom format |
| **Block Addressing** | SCSI LBA in CBW | Direct LBA in command |
| **Data Transfer** | Bulk only | Control setup + Bulk data |
| **Driver** | OS built-in | Vendor-specific required |

## References

- USB packet captures: `dumps/trek.json`, `dumps/defrag.json`
- Driver files: `win_driver/TREKTH2K.sys`
- Working implementations: `first_try.py`, `second_try.py`

---

*Document compiled from USB packet capture analysis and Windows driver disassembly.*
*Last updated: January 28, 2026*
