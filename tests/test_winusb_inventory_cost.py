"""Check every file size without repeatedly resolving shared directory chains."""
from pathlib import Path

import pytest

from app.devices.winusb_retention import CacheCapacityError
from tests.test_winusb_retention import cache


def headers(owner):
    folder=owner.root/'sessions/historical/native';folder.mkdir(parents=True)
    for i in range(30):(folder/f'header-{i}.json').write_text('{}')
    owner._inventory()
    return folder


def test_header_admission_checks_shared_directory_once_per_pass(tmp_path,monkeypatch):
    owner=cache(tmp_path);folder=headers(owner)
    checked=[];original=owner._safe
    def safe(path):checked.append(Path(path));return original(path)
    monkeypatch.setattr(owner,'_safe',safe)
    owner.before_capture(defer_cleanup=True)
    assert len(checked)<=8, 'Shared parent paths should be checked once per bounded measurement pass'
    assert all(owner._sizes[folder/f'header-{i}.json']==4096 for i in range(30))


def test_historical_header_growth_is_still_checked_before_every_request(tmp_path):
    owner=cache(tmp_path,max_bytes=2*1024*1024);folder=headers(owner)
    owner.before_capture(defer_cleanup=True)
    (folder/'header-12.json').write_bytes(b'x'*(3*1024*1024))
    with pytest.raises(CacheCapacityError):owner.before_capture(defer_cleanup=True)


def test_redirected_shared_parent_is_rejected_on_next_pass(tmp_path):
    owner=cache(tmp_path);folder=headers(owner)
    owner.before_capture(defer_cleanup=True)
    outside=tmp_path/'outside';folder.rename(outside)
    try:folder.symlink_to(outside,target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(outside),str(folder))
    with pytest.raises(ValueError,match='redirected|link|junction'):
        owner.before_capture(defer_cleanup=True)
    assert (outside/'header-0.json').read_text()=='{}'


def test_file_reparse_point_is_rejected_even_when_parent_is_valid(tmp_path,monkeypatch):
    import stat
    from types import SimpleNamespace
    owner=cache(tmp_path);folder=headers(owner);blocked=folder/'header-0.json'
    original=Path.stat
    def inspect(path,*args,**kwargs):
        if path==blocked:
            return SimpleNamespace(st_mode=stat.S_IFREG,st_file_attributes=0x400,st_size=1024)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',inspect)
    with pytest.raises(ValueError,match='redirected'):
        owner.before_capture(defer_cleanup=True)


def test_failed_measurement_does_not_publish_partial_size_updates(tmp_path,monkeypatch):
    owner=cache(tmp_path);folder=headers(owner)
    first=folder/'header-0.json';blocked=folder/'header-1.json'
    first.write_bytes(b'x'*8192);before=dict(owner._sizes);original=Path.stat
    def inspect(path,*args,**kwargs):
        if path==blocked:raise PermissionError('locked header')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',inspect)
    with pytest.raises(PermissionError):owner._measure_many((first,blocked))
    assert owner._sizes==before
