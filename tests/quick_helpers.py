"""Keep one QApplication alive across Quick and Widgets test modules."""
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

_APPLICATION = None


def quick_application():
    global _APPLICATION
    if _APPLICATION is None:
        _APPLICATION = QApplication.instance() or QApplication([])
    return _APPLICATION


def drain_qt_deletions():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
