# USB Thumb Drive Protocol Analysis

## Summary
This document describes the custom USB protocol used by a Trek Technology thumbdrive. The device uses **vendor-specific control transfers** followed by **bulk data transfers** to perform block-level read/write operations.

---

## Protocol Structure

### 1. Initial Device Information (31-byte response)

**Control Transfer:**
- **bmRequestType:** `0xc2` (Device-to-Host, Class, Endpoint)
- **bRequest:** `16` (Get Device Info)
- **wValue:** `0x0000`
- **wIndex:** `0`
- **wLength:** `31` bytes

**Response (31 bytes):**
```
1f:16:0a:11:11:10:01:02:00:20:00:00:08:00:00:20:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00
```

**Interpretation:**
```
Byte 0:     0x1f       = Response length (31 bytes)
Byte 1:     0x16       = Request number echo (bRequest=16)
Bytes 2-3:  0a:16      = Vendor ID (big-endian) = 0x0a16 (Trek Technology)
Bytes 4-5:  11:11      = Product ID (big-endian) = 0x1111 (ThumbDrive)
Bytes 6-7:  10:01      = Device version or USB spec (0x0110 = USB 1.1?)
Bytes 8-9:  02:00      = Unknown
Byte 10:    0x20       = Unknown (possibly sectors per block multiplier)
Bytes 11-14:00:08:00:00 = Size parameter 1 (little-endian: 0x00000800 = 2,048)
Bytes 15-18:20:00:00:00 = Size parameter 2 (little-endian: 0x00000020 = 32)
Bytes 19-30:           = Reserved / Padding (all zeros)
```

**Note:** Both DWORDs at offsets 0x0B and 0x0F are **little-endian**, matching
the native x86 word order used by the XP driver (`mov`/`imul`, no `bswap`).
Their product gives the **total sector count**, not a byte count.

- DWORD at offset 0x0B (bytes 11-14): 0x00000800 (little-endian) = 2,048
- DWORD at offset 0x0F (bytes 15-18): 0x00000020 (little-endian) = 32
- **Total Sectors: 2,048 × 32 = 65,536 (0x10000)**
- **Total Capacity: 65,536 × 512 = 33,554,432 bytes = 32 MB**

**Disk Capacity Calculation:**
- Size parameter 1: 0x00000800 = 2,048
- Size parameter 2: 0x00000020 = 32
- **Total Sectors: 2,048 × 32 = 65,536**
- **Total Capacity: 65,536 × 512 = 33,554,432 bytes = 32 MB**
- **Total 512-byte blocks: 65,536 (0x10000)**

---

## 2. Read/Write Operations

### Command Structure

**Control Transfer (Setup Phase):**
- **bmRequestType:** `0x42` (Host-to-Device, Vendor, Endpoint)
- **bRequest:** `17` (Read/Write Command)
- **wValue:** `0x0000`
- **wIndex:** `0`
- **wLength:** `8` bytes
- **Data Fragment (8 bytes):** `[LBA offset: 4 bytes][Block count: 4 bytes]`

**Both offset and count are little-endian 32-bit integers.**

**Bulk Transfer (Data Phase):**
- **Endpoint 0x82 (Bulk IN):** Device → Host (READ operations)
- **Endpoint 0x02 (Bulk OUT):** Host → Device (WRITE operations)
- **Transfer Size:** `block_count × 512` bytes

### Block Addressing
- **Block Size:** 512 bytes (standard sector size)
- **LBA (Logical Block Address):** Zero-indexed
- **Maximum Transfer Size:** Typically 8192-16384 bytes (16-32 blocks)

---

## 3. Example Operations

### Example 1: Read from LBA 0, 32 blocks
**Control OUT (bRequest=17):**
```
Data: 00:00:00:00:20:00:00:00
      ^^^^^^^^^^^ ^^^^^^^^^^^
      LBA 0       32 blocks (0x20)
```
**Bulk IN (Endpoint 0x82):**
```
Expected: 32 × 512 = 16384 bytes
```

### Example 2: Read from LBA 32, 32 blocks
**Control OUT (bRequest=17):**
```
Data: 20:00:00:00:20:00:00:00
      ^^^^^^^^^^^ ^^^^^^^^^^^
      LBA 32      32 blocks (0x20)
```
**Bulk IN (Endpoint 0x82):**
```
Expected: 32 × 512 = 16384 bytes
```

### Example 3: Read from LBA 65504 (0xFFE0), 32 blocks
**Control OUT (bRequest=17):**
```
Data: e0:ff:00:00:20:00:00:00
      ^^^^^^^^^^^ ^^^^^^^^^^^
      LBA 65504   32 blocks (0x20)
```
**Bulk IN (Endpoint 0x82):**
```
Expected: 32 × 512 = 16384 bytes
```

### Example 4: Write to LBA 1, 31 blocks
**Control OUT (bRequest=17):**
```
Data: 01:00:00:00:1f:00:00:00
      ^^^^^^^^^^^ ^^^^^^^^^^^
      LBA 1       31 blocks (0x1f)
```
**Bulk OUT (Endpoint 0x02):**
```
Transfer: 31 × 512 = 15872 bytes of data to write
```

---

## 4. Sequential Read Pattern (dd-like operations)

In the `defrag.json` capture, sequential block reads show **monotonic offset progression**:

| Frame | LBA (decimal) | LBA (hex) | Block Count | Bytes | Data Fragment |
|-------|---------------|-----------|-------------|-------|---------------|
| 20133 | 65504 | 0xFFE0 | 32 | 16384 | `e0:ff:00:00:20:00:00:00` |
| 20139 | 0 | 0x0000 | 32 | 16384 | `00:00:00:00:20:00:00:00` |
| 20143 | 32 | 0x0020 | 32 | 16384 | `20:00:00:00:20:00:00:00` |
| (continues with LBA += 32...) ||||

This demonstrates:
1. **Sequential access:** LBA increases by exactly 32 (0x20) each iteration
2. **Fixed block count:** Always reading 32 blocks (16 KB) per request
3. **Wraparound read:** First read at end of disk (LBA 65504), then starts from LBA 0

---

## 5. Write Operations

Write operations follow the same control transfer pattern but use **Bulk OUT** instead of Bulk IN:

**Sequence:**
1. **Control OUT (bRequest=17)** with LBA and block count
2. **Bulk OUT (Endpoint 0x02)** with actual data to write

**Example from defrag.json (Frame 22249):**
```
Control OUT: 01:00:00:00:1f:00:00:00
             LBA=1, Count=31 blocks (15872 bytes)

Bulk OUT: FAT16 boot sector data, filesystem metadata, etc.
```

---

## 6. USB Protocol Details

### Transfer Types
- **Control Transfers (Endpoint 0x00):** Commands and device info
- **Bulk Transfers (Endpoints 0x02/0x82):** Data payload

### URB (USB Request Block) Observations
- **urb_type 'S':** Submit (request initiated)
- **urb_type 'C':** Complete (request finished)
- **urb_status -115:** Request in progress (EINPROGRESS)
- **urb_status 0:** Success

### Timing
- Control transfers: ~0.7-2 ms latency
- Bulk IN transfers: ~9-19 ms for 16 KB
- Bulk OUT transfers: Similar timing

---

## 7. Key Findings

1. **Custom Protocol:** Not standard USB Mass Storage (SCSI/BOT)
2. **Block-Based:** LBA addressing with 512-byte sectors
3. **Little-Endian:** LBA/count values use little-endian byte order
4. **Little-Endian Capacity:** Both DWORDs in device info (offsets 0x0B, 0x0F) are little-endian; their product is the total sector count (× 512 for bytes)
5. **Fixed Command:** bRequest=17 for all read/write operations
6. **8-byte Command Block:** `[LBA:4][Count:4]` format
7. **Disk Size:** 32 MB (65,536 sectors × 512 bytes)
8. **Efficient Transfers:** 16-32 KB chunks (32-64 blocks) typical

---

## 8. Protocol Summary Table

| Operation | bmRequestType | bRequest | wLength | Data Format | Bulk Endpoint |
|-----------|---------------|----------|---------|-------------|---------------|
| Get Info | 0xc2 (IN) | 16 | 31 | Device returns 31-byte info | - |
| Read Blocks | 0x42 (OUT) | 17 | 8 | `[LBA:4][Count:4]` | 0x82 (IN) |
| Write Blocks | 0x42 (OUT) | 17 | 8 | `[LBA:4][Count:4]` | 0x02 (OUT) |

---

## 9. Offset Encoding Format

```
Little-Endian 32-bit LBA, Little-Endian 32-bit Block Count

Example: 20:00:00:00:20:00:00:00
         ├─────┬────┴─────┬────┘
         │     │          │
    Byte 0-3   │     Byte 4-7
    (LBA)      │     (Count)
               │
    0x00000020 = 32 (LBA)
    0x00000020 = 32 blocks (16384 bytes)
```

---

## 10. Implementation Notes

To implement a driver for this device:
1. Use **control transfers with bRequest=17** to send LBA/count commands
2. Follow with **bulk transfers** on endpoint 0x82 (read) or 0x02 (write)
3. Respect the **512-byte block size**
4. Handle transfers in **chunks** (max ~32-64 blocks recommended)
5. Parse the **31-byte device info** response to determine total capacity

---

## Files Analyzed
- `trek.json` - Initial enumeration and device info
- `defrag.json` - Read/write operations during defragmentation
- `dd_read.json` - Sequential read operations

## Date
Analysis completed: 2024
