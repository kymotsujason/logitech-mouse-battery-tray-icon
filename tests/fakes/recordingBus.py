from PyQt6.QtDBus import QDBusMessage


class RecordingBus:
    # stands in for a QDBusConnection when a test only needs to see what was subscribed, and every call it gets fails
    def __init__(self):
        self.subscriptions = []

    def connect(self, service, path, interface, name, slot):
        self.subscriptions.append((service, path, interface, name))
        return True

    def call(self, message, mode=None, timeout=None):
        return QDBusMessage()
