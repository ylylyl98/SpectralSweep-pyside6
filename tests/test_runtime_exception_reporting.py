import threading
import unittest
from unittest.mock import Mock

from app.runtime_exception_reporting import install


class RuntimeExceptionReportingTests(unittest.TestCase):
    def test_sys_hook_notifies_and_delegates_identical_args(self):
        original = Mock()
        notifier = Mock()
        import sys
        old = sys.excepthook
        sys.excepthook = original
        try:
            uninstall = install(notifier)
            value = ValueError("boom")
            trace = object()
            sys.excepthook(ValueError, value, trace)
            notifier.notify_crash.assert_called_once()
            original.assert_called_once_with(ValueError, value, trace)
            uninstall()
        finally:
            sys.excepthook = old

    def test_thread_hook_notifies_name_and_delegates_args(self):
        original = Mock()
        notifier = Mock()
        old = threading.excepthook
        threading.excepthook = original
        try:
            uninstall = install(notifier)
            args = threading.ExceptHookArgs((ValueError, ValueError("boom"), None,
                                               threading.current_thread()))
            threading.excepthook(args)
            notifier.notify_crash.assert_called_once()
            self.assertEqual(notifier.notify_crash.call_args.args[2], threading.current_thread().name)
            original.assert_called_once_with(args)
            uninstall()
        finally:
            threading.excepthook = old

    def test_notifier_failure_does_not_prevent_delegate_or_uninstall(self):
        original_sys = Mock(); original_thread = Mock()
        notifier = Mock()
        notifier.notify_crash.side_effect = RuntimeError("offline")
        import sys
        old_sys, old_thread = sys.excepthook, threading.excepthook
        sys.excepthook = original_sys; threading.excepthook = original_thread
        try:
            uninstall = install(notifier)
            value = RuntimeError("x")
            sys.excepthook(RuntimeError, value, None)
            args = threading.ExceptHookArgs((RuntimeError, value, None, None))
            threading.excepthook(args)
            original_sys.assert_called_once_with(RuntimeError, value, None)
            original_thread.assert_called_once_with(args)
            uninstall()
            self.assertIs(sys.excepthook, original_sys)
            self.assertIs(threading.excepthook, original_thread)
        finally:
            sys.excepthook = old_sys; threading.excepthook = old_thread


if __name__ == "__main__":
    unittest.main()
