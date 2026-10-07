/* Camera-free native checks. The live build uses the same packet/wait core. */
#define UNIT_TEST 1
#include "batch_probe.c"
#include <stdio.h>
static int failures,scenario,writes,get_calls,cancels,waits;
static BYTE sent[75];
static BOOL WINAPI fake_write(HANDLE h,LPCVOID p,DWORD n,LPDWORD got,LPOVERLAPPED ov){
 writes++;if(n!=75)failures++;CopyMemory(sent,p,n);*got=0;
 if(scenario==1){*got=75;return TRUE;}
 if(scenario==2){SetLastError(5);return FALSE;}
 SetLastError(ERROR_IO_PENDING);return FALSE;
}
static DWORD WINAPI fake_wait(HANDLE h,DWORD timeout){
 waits++;if(scenario==3||scenario==4)return waits==1?WAIT_TIMEOUT:(scenario==3?WAIT_OBJECT_0:WAIT_TIMEOUT);
 return WAIT_OBJECT_0;
}
static BOOL WINAPI fake_get(HANDLE h,LPOVERLAPPED ov,LPDWORD got,BOOL wait){
 get_calls++;*got=scenario==5?74:75;
 if(scenario==3){*got=0;SetLastError(ERROR_OPERATION_ABORTED);return FALSE;}
 if(scenario==6){*got=0;SetLastError(ERROR_GEN_FAILURE);return FALSE;}
 return TRUE;
}
static BOOL WINAPI fake_cancel(HANDLE h){cancels++;return TRUE;}
static void check(int yes,const char*name){if(!yes){failures++;printf("FAIL %s\n",name);}}
int main(void){
 DWORD i;BYTE values[24];
 api_write=fake_write;api_wait=fake_wait;api_get=fake_get;api_cancel=fake_cancel;
 write_event=CreateEventA(0,TRUE,FALSE,0);command_handle=(HANDLE)123;
 for(i=0;i<24;i++)values[i]=(i%3==1)?14:12;
 for(scenario=0;scenario<=6;scenario++){
  int ok;writes=get_calls=cancels=waits=0;pending_unknown=0;fatal=0;reserved=0;
  ok=write_batch(values);
  check(ok==(scenario==0||scenario==1),"success only after complete full write");
  check(writes==1,"no replay/retry");
  check(pending_unknown==(scenario==4),"unknown completion retained");
  check(sent[0]==1&&sent[1]==0x4a&&sent[2]==0,"address prefix");
  for(i=0;i<24;i++)check(sent[3+3*i]==2&&sent[4+3*i]==values[i]&&sent[5+3*i]==0,"all 24 ordered values");
  if(scenario==3||scenario==4)check(cancels==1&&waits==2,"cancel and confirm completion");
 }
 for(i=0;i<120;i++){
  DWORD site=expected_site(i);
  check(site!=0,"120 exact sites");
  check(is_data_site(site),"all loop sites recognized");
 }
 check(expected_site(120)==0,"reject excess data");
 check(!is_data_site(0x393e1),"trailing edges are never batched");
 pending_unknown=1;writes=0;
 check(!write_batch(values)&&writes==0,"never reuse uncertain OVERLAPPED/buffer");
 CloseHandle(write_event);
 printf("{\"status\":\"%s\",\"failures\":%d,\"scenarios\":7,\"camera_sdk_loaded\":false}\n",failures?"failed":"passed",failures);
 return failures?1:0;
}
