/* Reuse only successfully closed slots; totals remain lifetime accounting.
 * Native calls are admitted serially and frame workers join before rearm.
 */
static BOOL is_event(HANDLE h){DWORD i;if(!h)return FALSE;for(i=0;i<32;i++)if(active->owned_events[i]==h)return TRUE;return FALSE;}
static HANDLE WINAPI private_event(LPSECURITY_ATTRIBUTES a,BOOL manual,BOOL initial,LPCSTR name){
 DWORD i;HANDLE h;(void)name;
 if(a||active->event_live>=32||active->event_count==0xffffffffUL){denied("forbidden_event_shape");return NULL;}
 for(i=0;i<32;i++)if(!active->owned_events[i]){
  h=CreateEventA(NULL,manual,initial,NULL);
  if(h){active->owned_events[i]=h;active->event_count++;active->event_live++;}return h;
 }
 denied("event_registry_inconsistent");return NULL;
}
static BOOL WINAPI private_reset(HANDLE h){return is_event(h)?ResetEvent(h):denied("forbidden_ResetEvent");}
static BOOL WINAPI private_signal(HANDLE h){return is_event(h)?SetEvent(h):denied("forbidden_SetEvent");}
static BOOL WINAPI private_close(HANDLE h){DWORD i;if(!h)return denied("forbidden_CloseHandle");
 for(i=0;i<32;i++)if(active->owned_events[i]==h){
  if(!CloseHandle(h))return FALSE;
  active->owned_events[i]=NULL;active->event_closed++;active->event_live--;return TRUE;
 }return denied("forbidden_CloseHandle");
}
