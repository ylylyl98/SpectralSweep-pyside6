import hashlib
import struct

import numpy as np
import pytest

from app.devices.winspec_adapter import WinSpecSetup
from tests.test_winspec_adapter import Camera, Optics


class NativeCamera(Camera):
    def request(self, command, *args, **kwargs):
        reply, payload = super().request(command, *args, **kwargs)
        reply.update(acquisition_backend='pvcam', acquisition_settings_version=2,
                     temperature_guard_version=4)
        if command == 'ACQUIRE_GUARDED':
            reply.update(winspec_datatype=5, accumulation_method='software_sum_of_raw_frames',
                         raw_frame_count=self.settings['accumulations'], settings_scope='native_setup_plus_raw')
            payload = struct.pack('<512d', *([130001.] * 512))
            reply['payload_sha256'] = hashlib.sha256(payload).hexdigest()
        return reply, payload


def test_native_sum_uses_existing_normalization_once_and_records_provenance(monkeypatch):
    monkeypatch.setattr('app.devices.winspec_adapter.ensure_output_route',lambda *a,**kw:{})
    camera = NativeCamera(); camera.settings['accumulations'] = 2
    setup = WinSpecSetup(Optics(), client=camera, acquisition_backend='pvcam')
    x, y = setup.acquire()
    np.testing.assert_array_equal(x, np.arange(1,513))
    np.testing.assert_array_equal(y, np.full(512,65000.5))
    assert setup.identity['backend'] == 'winspec_ingaas'
    assert setup.identity['acquisition_backend'] == 'pvcam'
    frame = setup.read_metadata_snapshot()['observed']['last_frame']
    assert frame['intensity_processing']['divisor'] == 2
    assert frame['accumulation_method'] == 'software_sum_of_raw_frames'
    assert frame['raw_accumulated_counts'][0] == 130001


@pytest.mark.parametrize('requested,camera', [('winspec',NativeCamera),('pvcam',Camera)])
def test_wrong_service_is_rejected_at_connection_without_capture(requested,camera):
    client = camera()
    with pytest.raises(RuntimeError,match='backend'):
        WinSpecSetup(Optics(),client=client,acquisition_backend=requested)
    assert all(c[0]=='GET_SETTINGS' for c in client.calls)


def test_payload_corruption_is_rejected_before_transfer_acknowledgement():
    from tests.test_winspec_adapter import exchange
    payload = struct.pack('<512d', *([130001.] * 512))
    metadata = dict(ok=True,receipt_required=True,acquisition_backend='pvcam',
                    width=512,height=1,frame_count=1,winspec_datatype=5,
                    accumulation_method='software_sum_of_raw_frames',raw_frame_count=2,
                    settings_scope='native_setup_plus_raw',settings={'accumulations':2},
                    payload_sha256=hashlib.sha256(payload).hexdigest())
    receipts=[]
    client,_,thread=exchange(metadata,payload[:-1]+b'\x01',receipts=receipts)
    try:
        with pytest.raises(RuntimeError,match='checksum'): client.request('ACQUIRE_GUARDED')
    finally: thread.join(3)
    assert receipts==[b'']


@pytest.mark.parametrize('key,value',[('raw_frame_count',1),('accumulation_method','mean'),
                                     ('winspec_datatype',1),('settings_scope','unknown')])
def test_incompatible_native_frame_cannot_enter_common_processing(key,value):
    camera=NativeCamera();camera.settings['accumulations']=2
    metadata,payload=camera.request('ACQUIRE_GUARDED')
    metadata[key]=value
    with pytest.raises(RuntimeError): WinSpecSetup.decode_frame(metadata,payload)
