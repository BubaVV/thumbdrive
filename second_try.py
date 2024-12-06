import usb.core
import usb.util
import struct

def send_usb_control_transfer(device, bmRequestType, bRequest, wValue, wIndex, wLength):
    """Send a USB control transfer."""
    print(f"Sending control transfer: bmRequestType={bmRequestType}, bRequest={bRequest}, "
          f"wValue={wValue}, wIndex={wIndex}, wLength={wLength}")
    try:
        # Perform the control transfer
        response = device.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, wLength)
        print(f"Response: {response}")
    except usb.core.USBError as e:
        print(f"Error during control transfer: {e}")

def bulk_read(device, endpoint, length):
    """Perform a bulk read transfer."""
    print(f"Requesting {length} bytes from endpoint {hex(endpoint)}...")
    try:
        data = device.read(endpoint, length, timeout=5000)  # 5-second timeout
        print(f"Received {len(data)} bytes: {data}")
    except usb.core.USBError as e:
        print(f"Error during bulk read: {e}")

def main():
    # Locate the USB device (Replace with your device's VID and PID)
    VID = 0x0a16  # Replace with the Vendor ID of your device
    PID = 0x1111  # Replace with the Product ID of your device
    device = usb.core.find(idVendor=VID, idProduct=PID)

    if device is None:
        raise ValueError("Device not found")

    # Set the configuration (may need adjustment for your device)
    device.set_configuration()

    # Replay the USB control transfer
    bmRequestType = 0x80  # Direction: Device-to-host, Type: Standard, Recipient: Device
    bRequest = 6          # GET_DESCRIPTOR request
    wValue = 0x0100       # Descriptor Type (1) and Descriptor Index (0)
    wIndex = 0x0000       # Language ID (0)
    wLength = 40          # Requested descriptor length
    send_usb_control_transfer(device, bmRequestType, bRequest, wValue, wIndex, wLength)

    endpoint = 0x82  # Device-to-host, endpoint 2
    length = 8128    # Number of bytes to read
    bulk_read(device, endpoint, length)

if __name__ == "__main__":
    main()
