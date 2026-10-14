/* A second request must never reuse publication or a live worker from the first. */
#define main baseline_frame_tests
#include "pipp_frame_test.c"
#undef main
BOOL ws_pipp_frame_rearm(ws_pipp_frame*);
BOOL ws_pipp_frame_quiescent(ws_pipp_frame*);
static HANDLE retire_enter,retire_continue;
static volatile LONG retiring,retired_reads;
BOOL ws_rearm_test_destroy(ws_frame**f){
 if(retire_enter){InterlockedExchange(&retiring,1);SetEvent(retire_enter);CHECK(WaitForSingleObject(retire_continue,5000)==WAIT_OBJECT_0);}
 {BOOL ok=ws_frame_destroy(f);InterlockedExchange(&retiring,0);return ok;}
}
BOOL ws_rearm_test_status(ws_frame*f,ws_frame_info*q){if(retiring)InterlockedIncrement(&retired_reads);return ws_frame_status(f,q);}
static DWORD WINAPI rearm_thread(void*v){CHECK(ws_pipp_frame_rearm(v));return 0;}
static DWORD WINAPI poll_thread(void*v){DWORD n;CHECK(ws_pipp_frame_poll(v,&n)&&!n);return 0;}
int main(void){
 fixture f;DWORD i,j,n,before,after;WORD reply;ws_pipp_frame_info q;
 BOOL(WINAPI*count)(HANDLE,PDWORD)=(void*)GetProcAddress(GetModuleHandleA("kernel32.dll"),"GetProcessHandleCount");
 CHECK(!baseline_frame_tests());CHECK(count(GetCurrentProcess(),&before));
 setup(&f,TRUE);CHECK(ws_pipp_frame_initialize(f.frame));CHECK(!ws_pipp_frame_rearm(f.frame));
 for(i=0;i<4;i++){
  CHECK(ws_pipp_frame_start(f.frame,&reply));CHECK(!ws_pipp_frame_rearm(f.frame));CHECK(!ws_pipp_frame_quiescent(f.frame));
  for(j=0;j<1024;j++)f.buffer[0][j]=(BYTE)(j+17*i);
  terminal(&f,0,1024,0);wait_published(&f);
  CHECK(ws_pipp_frame_poll(f.frame,&n)&&n==1024);
  CHECK(ws_pipp_frame_stop(f.frame));
  CHECK(ws_pipp_frame_quiescent(f.frame));
  CHECK(!ws_pipp_frame_rearm(f.frame)); /* The native consumer still owns flags[0]. */
  CHECK(ws_pipp_frame_set_long(f.frame,0x33,0));
  CHECK(ws_pipp_frame_status(f.frame,&q)&&q.drained&&q.data.request_worker_joined&&q.data.delivery_worker_joined);
  for(j=0;j<1024;j++)CHECK(f.image[j]==(BYTE)(j+17*i));
  CHECK(f.log_count==6+5*(i+1)); /* Sizing commands occurred once. */
  if(i==3){HANDLE a,b;
   retire_enter=CreateEventA(NULL,TRUE,FALSE,NULL);retire_continue=CreateEventA(NULL,TRUE,FALSE,NULL);
   a=CreateThread(NULL,0,rearm_thread,f.frame,0,NULL);CHECK(WaitForSingleObject(retire_enter,5000)==WAIT_OBJECT_0);
   b=CreateThread(NULL,0,poll_thread,f.frame,0,NULL);Sleep(30);SetEvent(retire_continue);
   CHECK(WaitForSingleObject(a,5000)==WAIT_OBJECT_0&&WaitForSingleObject(b,5000)==WAIT_OBJECT_0);
   CHECK(!retired_reads);CloseHandle(a);CloseHandle(b);CloseHandle(retire_enter);CloseHandle(retire_continue);retire_enter=NULL;
  }else CHECK(ws_pipp_frame_rearm(f.frame));
  CHECK(ws_pipp_frame_poll(f.frame,&n)&&!n);
  CHECK(ws_pipp_frame_status(f.frame,&q)&&q.initialized&&q.drained&&!q.published&&!q.attempt_started);
 }
 done(&f);
 setup(&f,TRUE);CHECK(ws_pipp_frame_start(f.frame,&reply));
 terminal(&f,0,1023,0);wait_terminal(&f);ws_pipp_frame_stop(f.frame);
 CHECK(!ws_pipp_frame_rearm(f.frame));done(&f);
 setup(&f,TRUE);f.stale_data=TRUE;CHECK(!ws_pipp_frame_start(f.frame,&reply));
 ws_pipp_frame_stop(f.frame);CHECK(ws_pipp_frame_quiescent(f.frame));CHECK(!ws_pipp_frame_rearm(f.frame));done(&f);
 CHECK(count(GetCurrentProcess(),&after)&&before==after);
 printf("{\"suite\":\"persistent_frame\",\"checks\":%ld,\"handles_before\":%lu,\"handles_after\":%lu,\"hardware_calls\":0}\n",checks,before,after);return 0;
}
