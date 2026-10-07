from types import SimpleNamespace
import threading
import pytest
from tests.test_winspec_bridge_guard import bridge_functions


@pytest.mark.parametrize('ack,close_ok',[(b'WXAK',True),(b'NOPE',True),(b'',True),(b'WXAK',False)])
def test_cleanup_requires_receipt_and_successful_close(ack,close_ok):
    calls=[]
    doc=SimpleNamespace(Save=lambda:calls.append('save') or True,
                        Close=lambda:calls.append('close') or close_ok)
    state=SimpleNamespace(pending=(doc,'owned.spe'))
    ns=bridge_functions(dict(TRANSFER_STATE=state,CAMERA_LOCK=threading.Lock(),
        CLEANUP_FAILED=threading.Event(),TRANSFER_PENDING=threading.Event(),recv_exact=lambda s,n:ack,
        com_return_value=lambda v,n:v,server_log=lambda *a:None,
        remove_if_unlocked=lambda p,attempts:calls.append(('delete',p)) or True),
        'finish_transfer')
    ns['TRANSFER_PENDING'].set()
    ns['finish_transfer'](SimpleNamespace(settimeout=lambda n:None),True)
    assert not ns['TRANSFER_PENDING'].is_set()
    assert calls == (['save','close',('delete','owned.spe')] if ack==b'WXAK' and close_ok else
                     ['save','close'] if ack==b'WXAK' else [])
    assert ns['CLEANUP_FAILED'].is_set() != (ack==b'WXAK' and close_ok)


def test_failed_send_keeps_document_and_file():
    state=SimpleNamespace(pending=(object(),'owned.spe'))
    ns=bridge_functions(dict(TRANSFER_STATE=state,CLEANUP_FAILED=threading.Event(),TRANSFER_PENDING=threading.Event(),server_log=lambda *a:None),
                        'finish_transfer')
    ns['finish_transfer'](None,False)
    assert ns['CLEANUP_FAILED'].is_set()


def test_pending_transfer_blocks_new_acquisition_before_hardware_access():
    pending=threading.Event();pending.set()
    ns=bridge_functions(dict(TRANSFER_PENDING=pending,CLEANUP_FAILED=threading.Event()),'acquire_active')
    with pytest.raises(RuntimeError,match='transfer'):
        ns['acquire_active'](None)
