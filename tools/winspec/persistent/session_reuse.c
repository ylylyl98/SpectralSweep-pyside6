#include "session.c"
/* Called only after frame STOP, consume and both joins, then request release. */
BOOL ws_session_rearm_data(ws_session*s){
 DWORD i,e=0;if(!s)return fail(ERROR_INVALID_PARAMETER);
 EnterCriticalSection(&s->gate);
 if(s->control_fault)e=s->control_fault;
 else if(s->stopping||s->closing||s->provider_released||s->provider_users||!s->data_started)e=ERROR_INVALID_STATE;
 for(i=0;i<4;i++)if(s->slots[i])e=ERROR_BUSY;
 if(!e)s->data_started=FALSE;
 LeaveCriticalSection(&s->gate);return e?fail(e):good();
}
