import errno
import os
import socket
import threading

import hidpp

NAME = b"PRO X3 SUPERSTRIKE"
UNIT_ID = bytes.fromhex("02bc524c")


def reply(request, params):
    return (bytes(request[:4]) + bytes(params)).ljust(20, b"\x00")


def receiverError(request, code=0x09):
    return bytes([0x10, request[1], 0x8F, request[2], request[3], code, 0x00])


def deviceError(request):
    return bytes([0x11, request[1], 0xFF, request[2], request[3], 0x05]).ljust(20, b"\x00")


def mouseHandler(slot=1, features=None, answers=None, kind=3, unitId=UNIT_ID, name=NAME):
    # answers like the X3 does: device information at index 3, name at index 2, unified battery at index 5
    if (features is None):
        features = {0x0003: 3, 0x0005: 2, 0x1004: 5}
    if (answers is None):
        answers = {(5, 0): [0x0F, 0x02], (5, 1): [81, 0x04, 0, 0]}

    def handle(request):
        deviceIndex = request[1]
        featureIndex = request[2]
        function = request[3] >> 4
        if (deviceIndex != slot):
            return []
        if (featureIndex == 0 and function == 1):
            return [reply(request, [4, 2, request[6]])]
        if (featureIndex == 0 and function == 0):
            featureId = (request[4] << 8) | request[5]
            return [reply(request, [features.get(featureId, 0)])]
        if (featureIndex == 2 and function == 0):
            return [reply(request, [len(name)])]
        if (featureIndex == 2 and function == 1):
            return [reply(request, name[request[4]:request[4] + 16])]
        if (featureIndex == 2 and function == 2):
            return [reply(request, [kind])]
        if (0x0003 in features and featureIndex == features[0x0003] and function == 0):
            return [reply(request, [7] + list(unitId) + [0x00, 0x0C])]
        if ((featureIndex, function) in answers):
            return [reply(request, answers[(featureIndex, function)])]
        return [deviceError(request)]

    return handle


class FakeDevice:
    def __init__(self, handler):
        # SOCK_SEQPACKET keeps report boundaries, the way a hidraw node does
        self.appSide, self.deviceSide = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.handler = handler
        self.requests = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while (True):
            try:
                request = self.deviceSide.recv(64)
            except OSError:
                return
            if (not request):
                return
            self.requests.append(request)
            for report in self.handler(request):
                if (not self.send(report)):
                    return

    def send(self, report):
        try:
            self.deviceSide.send(report)
        except OSError:
            return False
        return True

    def unplug(self):
        # the app side then reads end of file, the way a hidraw node errors once its device is gone
        self.deviceSide.shutdown(socket.SHUT_RDWR)

    def node(self, onEvent=None):
        # a duplicate lets the app close its own copy without closing a number another test may reuse
        fd = os.dup(self.appSide.fileno())
        # HidppNode.open opens a real node with O_NONBLOCK, and the worker loop counts on a read with nothing waiting returning at once
        os.set_blocking(fd, False)
        return hidpp.HidppNode(fd, onEvent)

    def close(self):
        try:
            self.deviceSide.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(1)
        self.deviceSide.close()
        self.appSide.close()


class FakeNodes:
    def __init__(self):
        self.devices = {}
        self.everyDevice = []
        self.denied = set()
        self.failing = set()

    def add(self, path, handler):
        device = FakeDevice(handler)
        self.devices[path] = device
        self.everyDevice.append(device)
        return device

    def remove(self, path):
        self.devices.pop(path).unplug()

    def replace(self, path, handler):
        # the new device goes in first, thus discovery still lists the path when the old one errors
        old = self.devices[path]
        device = self.add(path, handler)
        old.unplug()
        return device

    def failOnce(self, path):
        self.failing.add(path)

    def deny(self, path):
        self.denied.add(path)

    def allow(self, path, handler):
        self.denied.discard(path)
        return self.add(path, handler)

    def find(self, sysRoot="/sys/class/hidraw", devRoot="/dev"):
        return sorted(set(self.devices) | self.denied)

    def open(self, path, onEvent=None):
        if (path in self.denied):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        if (path in self.failing):
            self.failing.discard(path)
            raise OSError(errno.EIO, "Input/output error", path)
        if (path not in self.devices):
            raise FileNotFoundError(path)
        return self.devices[path].node(onEvent)

    def closeAll(self):
        for device in self.everyDevice:
            device.close()
