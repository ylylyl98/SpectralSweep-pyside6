"""Temporary cleanup guard: unconfirmed Stop retains the existing owner."""
import threading
STOP_LOCK=threading.Lock()
class StopUncertain(RuntimeError):pass

def confirmed_stop(exp,unhealthy,decode,report=None):
    if unhealthy.is_set():raise StopUncertain('Owner already uncertain; no further Stop')
    if report is not None:report['cleanup_stop_confirmed']=False
    if not STOP_LOCK.acquire(False):
        unhealthy.set()
        if report is not None:report.update(status='recovery_required',recovery_required=True,
            owner_retained_reason='Another Stop is still pending')
        raise StopUncertain('Another Stop is pending; retain owner and do not queue another Stop')
    try:
        if unhealthy.is_set():raise StopUncertain('Owner became uncertain before Stop')
        result=decode(exp.Stop(),'Stop')
        if not result:raise RuntimeError('Stop did not confirm success')
        if unhealthy.is_set():raise RuntimeError('Owner became uncertain during Stop')
    except BaseException as error:
        unhealthy.set()
        if report is not None:
            report.update(status='recovery_required',recovery_required=True,
                owner_retained_reason='Cleanup Stop unconfirmed: '+type(error).__name__+': '+str(error))
        raise StopUncertain('Retain COM references and camera lease: '+str(error))
    finally:
        STOP_LOCK.release()
    if report is not None:report['cleanup_stop_confirmed']=True
    return result
