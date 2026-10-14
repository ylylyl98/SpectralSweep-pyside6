#include "raw_data_tap.c"
BOOL ws_raw_tap_rearm(BOOL drained,BOOL request_joined,BOOL delivery_joined){
 BOOL ok;
 if(!tap.initialized)return fail(ERROR_INVALID_STATE);
 EnterCriticalSection(&tap.lock);
 ok=drained&&request_joined&&delivery_joined&&tap.s.installed&&!tap.s.in_flight&&
    tap.s.valid&&tap.s.terminal&&tap.s.read_returned&&tap.s.data_reads==1&&!tap.s.diagnostic_error;
 if(ok){memset(&tap.s,0,sizeof(tap.s));tap.s.installed=TRUE;tap.interface_=NULL;tap.ov=NULL;tap.buffer=NULL;tap.reader=0;}
 LeaveCriticalSection(&tap.lock);return ok?TRUE:fail(ERROR_IO_INCOMPLETE);
}
