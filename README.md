# Trek Thumbdrive
Some attempts to connect antique Trek Thumdrive USB flash to Linux box

USB ID: 0a16:1111 Trek Technology (S) PTE, Ltd ThumbDrive

`win_driver` holds driver for XP and 2000.

### USB Permissions (udev)

The device needs a udev rule for non-root access. Install the bundled rule:

```bash
sudo cp udev/50-trek.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then re-plug the device. See `udev/50-trek.rules` for details.

Some dumps are collected: for subj and for usual mass storage pendrive

### dd dumps
defrag.json - did defragmentation using standard XP tool

dd_read.json - dump of dd command

trek.img - result of dd

dd_write.json - previous file written back. Not sure that size detected correctly, 
sum glitches at the end are possible

## NBD Server Debug Tool

`nbd_server.py` is a simple NBD server to test network block device sharing.
It expects a file named `disk.img` in the same directory.

To create a dummy 64MB disk image:

```bash
dd if=/dev/zero of=disk.img bs=1M count=64
```

To format and populate with test data:

```bash
# Format as VFAT
mkfs.vfat disk.img

# Mount and copy files
mkdir -p mnt
sudo mount -o loop disk.img mnt
sudo touch mnt/hello.txt
sudo umount mnt
```

Then run the server:

```bash
python3 nbd_server.py
```

### Client Usage

In another terminal, connect the NBD client and mount the device:

```bash
# Connect the client
sudo nbd-client localhost 10809 /dev/nbd0

# Mount the NBD device
sudo mount /dev/nbd0 mnt

# Check contents
ls -l mnt/

# Cleanup
sudo umount mnt
sudo nbd-client -d /dev/nbd0
```


