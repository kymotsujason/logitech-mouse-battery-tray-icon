import os
import struct
import sys

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo)
sys.path.insert(0, os.path.join(repo, "src"))

from tests.fakes.fakeDevice import mouseHandler

UHID_DESTROY = 1
UHID_OUTPUT = 6
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_DATA_MAX = 4096
HID_MAX_DESCRIPTOR_SIZE = 4096
BUS_USB = 0x03
LOGITECH = 0x046D
RECEIVER = 0xC54F
# struct uhid_event is a u32 type and a union whose largest member is uhid_create2_req
EVENT_SIZE = 4 + 128 + 64 + 64 + 2 + 2 + 4 + 4 + 4 + 4 + HID_MAX_DESCRIPTOR_SIZE
# the X3 receiver's HID++ descriptor, the same bytes tests/containers/checkPackage.sh feeds the rule's check
DESCRIPTOR = bytes.fromhex("06 43 ff 0a 01 03 a1 01 85 10 95 06 75 08 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0 06 43 ff 0a 02 03 a1 01 85 11 95 13 75 08 15 00 26 ff 00 09 02 81 00 09 02 91 00 c0")


def createEvent(name, descriptor, vendor=LOGITECH, product=RECEIVER):
    header = struct.pack("<I128s64s64sHHIIII", UHID_CREATE2, name, b"", b"", len(descriptor), BUS_USB, vendor, product, 0, 0)
    return header + descriptor.ljust(HID_MAX_DESCRIPTOR_SIZE, b"\x00")


def inputEvent(data):
    return struct.pack("<IH", UHID_INPUT2, len(data)) + data.ljust(UHID_DATA_MAX, b"\x00")


def destroyEvent():
    return struct.pack("<I", UHID_DESTROY)


def parseEvent(raw):
    kind = struct.unpack_from("<I", raw, 0)[0]
    if (kind != UHID_OUTPUT):
        return (kind, b"")
    size = struct.unpack_from("<H", raw, 4 + UHID_DATA_MAX)[0]
    return (kind, bytes(raw[4:4 + size]))


def main():
    handler = mouseHandler()
    fd = os.open("/dev/uhid", os.O_RDWR)
    os.write(fd, createEvent(b"Logitech USB Receiver", DESCRIPTOR))
    print("created", flush=True)
    try:
        while (True):
            kind, request = parseEvent(os.read(fd, EVENT_SIZE))
            if (kind == UHID_OUTPUT):
                for reply in handler(request):
                    os.write(fd, inputEvent(reply))
    finally:
        os.write(fd, destroyEvent())
        os.close(fd)


if (__name__ == "__main__"):
    main()
