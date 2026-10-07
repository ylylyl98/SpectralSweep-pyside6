from types import SimpleNamespace

import pytest

from tests.test_winspec_bridge_guard import bridge_functions


def display_functions(exp, enum=True):
    return bridge_functions(dict(const=lambda name:name if enum else None,
        server_log=lambda *a:None, com_return_value=lambda result,name:result,
        get_param=lambda exp,key:exp.GetParam(key)),
        'read_acquisition_display', 'set_acquisition_display')


@pytest.mark.parametrize('original', [-1,1,0])
def test_display_control_hides_and_restores_original_boolean(original):
    state=[original]
    exp=SimpleNamespace(GetParam=lambda key:state[0],
                        SetParam=lambda key,value:state.__setitem__(0,value) or 0)
    ns=display_functions(exp)
    saved=ns['read_acquisition_display'](exp)
    assert saved == ('EXP_BSHOWWINDOW',original)
    ns['set_acquisition_display'](exp,saved[0],0)
    assert state[0] == 0
    ns['set_acquisition_display'](exp,*saved)
    assert state[0] == original


@pytest.mark.parametrize('failure', ['missing_enum','getter','invalid_value'])
def test_unsupported_display_is_detected_without_any_write(failure):
    def get(key):
        if failure == 'getter':
            raise RuntimeError('Unavailable')
        return 7
    exp=SimpleNamespace(GetParam=get,SetParam=lambda *args:pytest.fail('Must not write unsupported parameter'))
    ns=display_functions(exp,enum=failure!='missing_enum')
    assert ns['read_acquisition_display'](exp) is None


@pytest.mark.parametrize('status,actual', [(1,0),(0,-1)])
def test_display_set_rejects_error_status_or_wrong_readback(status,actual):
    exp=SimpleNamespace(GetParam=lambda key:actual,SetParam=lambda *args:status)
    ns=display_functions(exp)
    with pytest.raises(RuntimeError,match='display'):
        ns['set_acquisition_display'](exp,'EXP_BSHOWWINDOW',0)
