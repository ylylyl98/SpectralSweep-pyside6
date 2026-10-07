import gc,weakref
from types import SimpleNamespace
import pytest

def owner(exp=None,cancel=lambda:False):
    try:from tools.winspec.acquisition_owner import Owner
    except ImportError:pytest.fail('Acquisition apartment ownership is missing')
    return Owner(exp or SimpleNamespace(Start=lambda d:True),cancel,lambda:None,lambda v,n:v)

class Doc:
    def __init__(self,close=True):self.closed=False;self.close_ok=close;self.saved=[]
    def SaveAs(self,path,kind):self.saved.append(path);return True
    def Save(self):return True
    def Close(self):self.closed=self.close_ok;return self.close_ok

def test_stop_before_start_does_not_issue_start():
    calls=[];o=owner(SimpleNamespace(Start=lambda d:calls.append(d)),lambda:True)
    with pytest.raises(RuntimeError):o.Start(Doc())
    assert not calls

def test_byref_document_is_retained_even_if_stop_arrives_during_start():
    stopped=[False];factory=Doc();returned=Doc()
    def start(doc):stopped[0]=True;return True,returned
    o=owner(SimpleNamespace(Start=start),lambda:stopped[0])
    with pytest.raises(RuntimeError):o.Start(factory)
    assert [r['doc'] for r in o.documents]==[factory,returned]

def test_failed_close_keeps_com_references_alive_for_recovery():
    d=Doc(close=False);ref=weakref.ref(d);o=owner();o.retain(d,True);del d;gc.collect()
    with pytest.raises(RuntimeError):o.cleanup_rejected(lambda i:'failed-%d.spe'%i)
    assert o.failed and ref() is not None and o.exp is not None

def test_confirmed_rejection_archives_only_owned_documents_and_releases():
    o=owner();a=Doc();b=Doc();user=Doc();o.retain(a,True);o.retain(b,False)
    paths=o.cleanup_rejected(lambda i:'rejected-%d.spe'%i)
    assert paths==['rejected-0.spe'] and a.closed and b.closed and not user.closed
    assert not o.documents and o.exp is None

def test_successful_transfer_preserves_distinct_factory_doc_until_cleanup():
    o=owner();factory=Doc();actual=Doc();o.retain(factory,True);o.retain(actual,True)
    actual.Close();o.mark_closed(actual)
    assert o.documents[0]['doc'] is factory
    o.cleanup_rejected(lambda i:'factory-%d.spe'%i)
    assert factory.closed and not o.documents


def test_factory_document_survives_failure_before_timed_start():
    from tests.test_winspec_bridge_guard import bridge_functions
    from tools.winspec.start_acceleration.manager import TimedExperiment
    refs=[];o=owner()
    def factory():
        d=Doc();refs.append(weakref.ref(d));return d
    def counter():raise RuntimeError('QPC failed')
    ns=bridge_functions(dict(create_document=factory,com_return_value=lambda v,n:v),'start_new_document')
    with pytest.raises(RuntimeError,match='QPC failed'):
        ns['start_new_document'](TimedExperiment(o,counter))
    gc.collect()
    assert refs[0]() is not None and o.documents[0]['doc'] is refs[0]()
