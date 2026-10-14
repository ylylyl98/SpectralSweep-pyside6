#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
typedef struct {HANDLE owned_events[32];DWORD event_count,event_closed,event_live,native_fault;} registry;
static registry state,*active=&state;
static BOOL fail_close;
static BOOL denied(const char*why){(void)why;active->native_fault=ERROR_ACCESS_DENIED;SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
static BOOL WINAPI test_close(HANDLE h){if(fail_close){SetLastError(ERROR_ACCESS_DENIED);return FALSE;}return CloseHandle(h);}
#define CloseHandle test_close
#include "persistent_events.h"
#undef CloseHandle
#define CHECK(x) do{if(!(x)){printf("event registry failure line %d\n",__LINE__);return 1;}}while(0)
int main(void){DWORD i,before,after;HANDLE h,all[32],foreign;BOOL(WINAPI*count)(HANDLE,PDWORD)=(void*)GetProcAddress(GetModuleHandleA("kernel32.dll"),"GetProcessHandleCount");
 CHECK(count&&count(GetCurrentProcess(),&before));
 CHECK(!private_close(NULL)&&!private_reset(NULL)&&!private_signal(NULL));
 foreign=CreateEventA(NULL,FALSE,FALSE,NULL);CHECK(foreign&&!private_close(foreign)&&CloseHandle(foreign));
 for(i=0;i<32;i++){all[i]=private_event(NULL,TRUE,FALSE,NULL);CHECK(all[i]);}
 CHECK(active->event_live==32&&!private_event(NULL,FALSE,FALSE,NULL));
 fail_close=TRUE;CHECK(!private_close(all[0])&&is_event(all[0])&&active->event_live==32&&active->event_closed==0);
 CHECK(!private_event(NULL,FALSE,FALSE,NULL));fail_close=FALSE;
 CHECK(private_close(all[0])&&active->event_live==31);
 all[0]=private_event(NULL,TRUE,FALSE,NULL);CHECK(all[0]&&active->event_count==33&&active->event_live==32);
 for(i=0;i<32;i++)CHECK(private_close(all[i]));
 for(i=0;i<2000;i++){h=private_event(NULL,TRUE,FALSE,NULL);CHECK(h&&private_signal(h)&&WaitForSingleObject(h,0)==WAIT_OBJECT_0&&private_reset(h)&&private_close(h));}
 CHECK(active->event_count==2033&&active->event_closed==2033&&!active->event_live);
 CHECK(count(GetCurrentProcess(),&after)&&before==after);
 printf("{\"suite\":\"persistent_event_registry\",\"created\":%lu,\"closed\":%lu,\"live\":%lu,\"handles_before\":%lu,\"handles_after\":%lu,\"hardware_calls\":0}\n",state.event_count,state.event_closed,state.event_live,before,after);return 0;
}
