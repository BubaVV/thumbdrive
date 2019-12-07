import usb.core
import usb.util

from base64 import b16decode

# ID 0a16:1111 Trek Technology (S) PTE, Ltd ThumbDrive
VENDOR = 0x0a16
PRODUCT = 0x1111

# To ease fiddling with device, set up constant access rights:
# $ sudo vi /etc/udev/rules.d/50-trek.rules
#
# SUBSYSTEMS=="usb", ATTRS{idVendor}=="0a16", ATTRS{idProduct}=="1111", GROUP="users", MODE="0666"

dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
if dev is None:
    raise ValueError('Device not found')


# set the active configuration. With no arguments, the first
# configuration will be the active one
dev.set_configuration()

# get an endpoint instance
cfg = dev.get_active_configuration()
print(cfg)

interface = cfg[(0,0)]
print(interface)

raw_msg = 'e0:ff:00:00:20:00:00:00'
msg = bytes.fromhex(raw_msg.replace(':',''))
# ctrl_transfer( bmRequestType, bmRequest, wValue, wIndex, nBytes)
ret = dev.ctrl_transfer(0x42, 17, 0, 0, len(msg))
print(ret)

res = ""

for i in range(20):
    try:
        res += dev.read(0x82, 8, 100)
    except usb.core.USBError as e:
        if e.errno == 110:
            pass
        else:
            raise


print(res)

# >>> interface[0]
# <ENDPOINT 0x2: Bulk OUT>
# >>> interface[1]
# <ENDPOINT 0x82: Bulk IN>
# >>> interface[2]
# <ENDPOINT 0x0: Control OUT>
