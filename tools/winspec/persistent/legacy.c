/* Keep the existing export ABI; only opcode12 adds success-only frame rearm. */
#include "legacy_acquisition.h"
#define WsPippFrameV1 frozen_WsPippFrameV1
#include "legacy_acquisition.c"
#undef WsPippFrameV1
BOOL ws_pipp_frame_rearm(ws_pipp_frame*);
BOOL ws_pipp_frame_quiescent(ws_pipp_frame*);
DWORD WINAPI WsPippFrameV1(BYTE*port,DWORD operation,DWORD a,DWORD b,DWORD c){
 DWORD e=0;BOOL ok=FALSE;context*owner;
 if(operation!=12&&operation!=13)return frozen_WsPippFrameV1(port,operation,a,b,c);
 binding_init();EnterCriticalSection(&binding_gate);
 if(!port||port!=binding.port||a||b||c){e=ERROR_INVALID_PARAMETER;goto done;}
 owner=binding.owner;EnterCriticalSection(&owner->transaction);
 if(operation==13){ok=ws_pipp_frame_quiescent(binding.frame);if(!ok)e=error();}
 else{e=binding.error?binding.error:ws_control_fault(owner->control);
  if(!e){ok=ws_pipp_frame_rearm(binding.frame);if(!ok)e=error();}}
 LeaveCriticalSection(&owner->transaction);
done:LeaveCriticalSection(&binding_gate);SetLastError(e);return ok;
}
