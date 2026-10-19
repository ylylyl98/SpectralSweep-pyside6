"""Owned external subscriptions, including callbacks already queued by Qt."""
from PySide6.QtCore import QObject


class SignalConnections:
    def __init__(self, owner):
        self._connections = []
        owner.destroyed.connect(self.clear)

    def connect(self, signal, slot):
        # Clearing the target also neutralizes deliveries queued before disconnect.
        target = [slot]

        def deliver(*args):
            if target[0] is not None:
                target[0](*args)

        connection = signal.connect(deliver)
        self._connections.append((connection, target))

    def clear(self, *_args):
        connections, self._connections = self._connections, []
        for connection, target in connections:
            target[0] = None
            QObject.disconnect(connection)
