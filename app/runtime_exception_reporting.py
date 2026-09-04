"""Global best-effort reporting for uncaught Python exceptions."""
import sys
import threading


def install(notifier):
    original_sys = sys.excepthook
    original_thread = getattr(threading, "excepthook", None)

    def sys_hook(exc_type, exc_value, traceback):
        try:
            notifier.notify_crash(exc_type, exc_value, threading.current_thread().name)
        except Exception:
            pass
        original_sys(exc_type, exc_value, traceback)

    sys.excepthook = sys_hook
    if original_thread is not None:
        def thread_hook(args):
            try:
                notifier.notify_crash(args.exc_type, args.exc_value,
                                      getattr(args.thread, "name", None))
            except Exception:
                pass
            original_thread(args)
        threading.excepthook = thread_hook

    def uninstall():
        if sys.excepthook is sys_hook:
            sys.excepthook = original_sys
        if original_thread is not None and threading.excepthook is thread_hook:
            threading.excepthook = original_thread
    return uninstall
