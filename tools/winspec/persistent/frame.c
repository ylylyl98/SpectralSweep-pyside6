/* Extend the frozen one-attempt adapter; never reset a faulted/live request. */
#include "pipp_frame.c"
BOOL ws_session_rearm_data(ws_session*);
BOOL ws_pipp_frame_quiescent(ws_pipp_frame*f){
 BOOL ok;if(!f)return fail(ERROR_INVALID_PARAMETER);
 EnterCriticalSection(&f->operation);EnterCriticalSection(&f->publication);observe_data(f);
 ok=!f->info.active&&f->info.drained&&(!f->frame||(f->info.data.drained&&
     f->info.data.request_worker_joined&&f->info.data.delivery_worker_joined));
 LeaveCriticalSection(&f->publication);LeaveCriticalSection(&f->operation);
 return ok?good():fail(ERROR_IO_INCOMPLETE);
}
BOOL ws_pipp_frame_rearm(ws_pipp_frame*f){
 DWORD error=0,i;BOOL initialized;
 if(!f)return fail(ERROR_INVALID_PARAMETER);
 EnterCriticalSection(&f->operation);EnterCriticalSection(&f->publication);
 observe_data(f);
 if(f->info.error)error=f->info.error;
 else if(!f->info.initialized||!f->info.attempt_started||f->info.active||!f->info.drained||
   !f->info.published||!f->info.consumed||!f->info.stop.reply_valid||!f->info.stop.write_completed||
   !f->info.data.request_worker_joined||!f->info.data.delivery_worker_joined||!f->frame)
  error=ERROR_INVALID_STATE;
 for(i=0;i<f->blocks;i++)if(f->flags[i])error=ERROR_BUSY;
 initialized=f->info.initialized;
 /* Both workers are joined: holding publication through destroy cannot block
    a callback, and excludes poll's observe_data from the retiring pointer. */
 if(!error&&!ws_frame_destroy(&f->frame))error=GetLastError();
 if(!error&&!ws_session_rearm_data(f->session))error=GetLastError();
 if(!error){
  if(!ResetEvent(EVENT(f,0x4b8)))error=GetLastError();
  if(!error){
   memset(&f->info,0,sizeof(f->info));f->info.initialized=initialized;f->info.drained=TRUE;
   f->stop_sent=FALSE;f->accepting=FALSE;memset(f->flags,0,f->blocks*sizeof(DWORD));
   FIELD(f,0x384)=FIELD(f,0x388)=FIELD(f,0x39c)=0;
  }else first_error(f,error);
 }
 LeaveCriticalSection(&f->publication);
 LeaveCriticalSection(&f->operation);return error?fail(error):good();
}
