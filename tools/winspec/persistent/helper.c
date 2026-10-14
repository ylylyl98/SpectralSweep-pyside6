/* One initialization, serialized requests, success-only rearm; never force exit. */
static unsigned long ws_exposure_ms=100;
#define main unused_frozen_entry
#include "persistent_physical_native.c"
#undef main
#if WS_PORTABLE_BINDING
#include "portable_binding.h"
#define WS_READY_CAPACITY 1024
#else
#define WS_READY_CAPACITY 512
#endif
#include "raw_data_tap.h"
BOOL ws_raw_tap_rearm(BOOL,BOOL,BOOL);
static BOOL uuid_valid(const char*s){DWORD i;if(strlen(s)!=36)return FALSE;for(i=0;i<36;i++){
 if(i==8||i==13||i==18||i==23){if(s[i]!='-')return FALSE;}
 else if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return FALSE;}return TRUE;}
static BOOL receipt(physical*p,const char*name,const char*text){
 char temporary[80],source[MAX_PATH],target[MAX_PATH];
 sprintf(temporary,"%s.partial",name);
 if(!save_bytes(p,temporary,(const BYTE*)text,(DWORD)strlen(text)))return FALSE;
 sprintf(source,"%s\\%s",p->output_dir,temporary);sprintf(target,"%s\\%s",p->output_dir,name);
 return MoveFileExA(source,target,MOVEFILE_WRITE_THROUGH);
}
/* stdin is the owning Python pipe. EOF means orderly close, never reset. */
static int command_line(char*out,DWORD capacity){
 HANDLE input=GetStdHandle(STD_INPUT_HANDLE);DWORD start=GetTickCount(),available,n,i=0;char c;
 for(;;){
  if(!PeekNamedPipe(input,NULL,0,NULL,&available,NULL))return GetLastError()==ERROR_BROKEN_PIPE&&!i?0:-1;
  if(!available){if(i&&GetTickCount()-start>=30000)return -1;Sleep(5);continue;}
  if(!ReadFile(input,&c,1,&n,NULL))return GetLastError()==ERROR_BROKEN_PIPE&&!i?0:-1;
  if(!n)return i?-1:0;
  if(!i)start=GetTickCount();
  if(c=='\n'){if(i&&out[i-1]=='\r')i--;out[i]=0;return 1;}
  if(i+1>=capacity)return -1;out[i++]=c;
 }
}
static BOOL capture_one(physical*p,lifecycle*l,const char*root,const char*session,DWORD seq,const char*id){
 char text[1500];DWORD stage,i;ws_raw_tap_info tap;
 sprintf(p->output_dir,"%s\\frame-%06lu",root,seq);
 if(!CreateDirectoryA(p->output_dir,NULL))return FALSE;
 p->captured=p->started=p->stopped=FALSE;memset(p->image,0xa5,1024);memset(p->snapshot,0,1024);
 l->capture_valid=l->saved=l->drained=FALSE;
 log_line(p,"request_sequence",seq,0);
 if(p->native_fault)return failure(p->native_fault);
 for(stage=START;stage<=CAPTURE;stage++){
  SetLastError(0);if(!native_step(l,stage)){remember(l,stage);break;}
  if(stage==CAPTURE)l->capture_valid=TRUE;
 }
 SetLastError(0);if(!native_step(l,STOP))remember(l,STOP);
 SetLastError(0);if(!native_step(l,DRAIN)){remember(l,DRAIN);park(p,"retained_frame_drain");}l->drained=TRUE;
 if(l->first_error)return FALSE;
 if(!p->info.data.request_worker_joined||!p->info.data.delivery_worker_joined||!p->info.stop.reply_valid||!p->info.stop.write_completed)return failure(ERROR_IO_INCOMPLETE);
 if(!ws_raw_tap_status(&tap)||!tap.valid||tap.in_flight||tap.diagnostic_error||!tap.terminal||tap.terminal_error||
    tap.terminal_bytes!=1024||tap.data_reads!=1||tap.copies!=1||tap.matched_results!=1)return failure(ERROR_INVALID_DATA);
 for(i=0;i<1024;i++)if(tap.bytes[i]!=p->snapshot[i])return failure(ERROR_INVALID_DATA);
 if(!native_step(l,SAVE)||!save_bytes(p,"data82-transport.bin",tap.bytes,1024))return FALSE;l->saved=TRUE;
#if WS_INJECTED_TRANSPORT
 if(injected_mode==23)p->native_fault=ERROR_WRITE_FAULT; /* Offline late-log failure injection. */
#endif
 if(p->native_fault)return failure(p->native_fault);
 sprintf(text,"{\"protocol\":1,\"recipe_protocol\":1,\"session_id\":\"%s\",\"request_id\":\"%s\",\"sequence\":%lu,\"exposure_ms\":%lu,\"exposure_readback_verified\":true,\"native_frame_count\":1,\"bytes\":1024,\"data_reads\":1,\"copies\":1,\"matched_results\":1,\"drained\":true,\"request_worker_joined\":true,\"delivery_worker_joined\":true,\"stop_reply_valid\":true,\"stop_write_completed\":true,\"transport_equal\":true,\"error\":0,\"log_segment\":%lu,\"session_elapsed_ms\":%.3f}\n",session,id,seq,ws_exposure_ms,persistent_log.segment,milliseconds(p));
 return receipt(p,"frame.json",text);
}
static int session_main(const char*directory,const char*output,const char*session,DWORD log_limit){
 physical*p=calloc(1,sizeof(*p));lifecycle*l=calloc(1,sizeof(*l));char path[MAX_PATH],root[MAX_PATH],line[96],id[64],extra[2],text[WS_READY_CAPACITY];
 DWORD i,n,seq=0,requested=0,result=0;BOOL setup=FALSE,attached=FALSE;int input;
 HMODULE observer=NULL;ws_raw_tap_info tap;
#if WS_PORTABLE_BINDING
 char binding_json[512];
 if(!ws_bound_json(binding_json,sizeof(binding_json)))return 64;
#endif
 if(!p||!l||!uuid_valid(session))return 64;
 active=p;l->ctx=p;l->step=native_step;QueryPerformanceFrequency(&p->frequency);QueryPerformanceCounter(&p->epoch);
 p->startup_mutex=CreateMutexA(NULL,TRUE,"Local\\Codex.WinSpec.Recognition.Startup.v1");
 if(!p->startup_mutex||GetLastError()==ERROR_ALREADY_EXISTS)return 1;
 n=GetFullPathNameA(output,MAX_PATH,root,NULL);
 if(!n||n>=MAX_PATH||lstrcmpiA(root,output)||strlen(root)>MAX_PATH-100||!CreateDirectoryA(root,NULL))return 64;
 strcpy(p->output_dir,root);
 if(!pl_open(root,session,log_limit))return 1;
 p->log=persistent_log.handle;
 setup=runtime_setup(p,directory);
 if(setup){
#if WS_INJECTED_TRANSPORT
  observer=p->usb;
#else
  n=GetSystemDirectoryA(path,MAX_PATH);if(!n||n>MAX_PATH-20)setup=FALSE;
  else{strcat(path,"\\winusb.dll");observer=LoadLibraryExA(path,NULL,LOAD_WITH_ALTERED_SEARCH_PATH);if(!observer||!same_module(observer,path))setup=FALSE;}
#endif
  if(setup){setup=ws_raw_tap_install(p->usb,observer);if(ws_raw_tap_status(&tap))attached=tap.installed;}
 }
 if(setup)for(i=IDENTITY;i<=CONFIGURE;i++){SetLastError(0);if(!native_step(l,i)){remember(l,i);setup=FALSE;break;}}
 if(!setup&&!l->first_error){l->first_error=last_error();l->failed_stage=99;}
 if(setup){
  sprintf(text,"{\"protocol\":1,\"recipe_protocol\":1,\"session_id\":\"%s\",\"initialized\":true,\"exposure_ms\":%lu,\"exposure_readback_verified\":true,\"log_protocol\":1,\"rotation_bytes\":%lu}\n",session,ws_exposure_ms,log_limit);
#if WS_PORTABLE_BINDING
  sprintf(text+strlen(text)-2,",\"binding_protocol\":1,\"device_instance\":%s}\n",binding_json);
#endif
  setup=receipt(p,"ready.json",text);if(!setup)l->first_error=last_error();
 }
 while(setup){
  input=command_line(line,sizeof(line));if(!input)break;
  if(input<0){l->first_error=ERROR_INVALID_DATA;break;}
  if(!strcmp(line,"Q"))break;
  if(sscanf(line,"C %lu %63s %1s",&requested,id,extra)!=2||requested!=seq+1||!uuid_valid(id)||requested>999999){l->first_error=ERROR_INVALID_DATA;break;}
  if(!pl_rotate(seq)){l->first_error=last_error();break;}
  p->log=persistent_log.handle;
  if(seq){
   if(!p->dispatch(p->port,12,0,0,0)||!ws_raw_tap_rearm(TRUE,TRUE,TRUE)){l->first_error=last_error();break;}
   log_line(p,"session_rearmed",seq,0);
  }
  if(!capture_one(p,l,root,session,requested,id)){if(!l->first_error)l->first_error=last_error();break;}
  seq=requested;
 }
 /* Close admission by leaving the command loop, then apply existing barriers. */
 SetLastError(0);if(!native_step(l,STOP))remember(l,STOP);
 SetLastError(0);if(!native_step(l,DRAIN)){remember(l,DRAIN);park(p,"retained_session_drain");}l->drained=TRUE;
 if(attached){
  if(!ws_raw_tap_status(&tap))park(p,"retained_observer_status");
  /* Failed prepare may already have destroyed its request and left no frame
     history. Query the actual binding barrier rather than inventing history. */
  if(tap.accepted&&(!p->port||!p->dispatch(p->port,13,0,0,0)))park(p,"retained_binding_workers");
  if(!ws_raw_tap_detach(TRUE,TRUE,TRUE))park(p,"retained_observer_detach");
 }
 SetLastError(0);if(!native_step(l,DESTROY)){remember(l,DESTROY);park(p,"retained_native_destroy");}l->destroyed=TRUE;
 if(p->controller&&!FreeLibrary(p->controller))park(p,"retained_controller_module");
 if(p->usb&&!FreeLibrary(p->usb))park(p,"retained_usb_module");
#if !WS_INJECTED_TRANSPORT
 if(observer&&!FreeLibrary(observer))park(p,"retained_observer_module");
#endif
 for(i=0;i<WS_PIN_COUNT;i++)if(p->pins[i])CloseHandle(p->pins[i]);
 strcpy(p->output_dir,root);result=l->first_error?1:0;
 log_line(p,"safe_process_exit",result,l->first_error);
 if(!pl_finish(seq,l->first_error)&&!l->first_error)l->first_error=last_error();p->log=NULL;result=l->first_error?1:0;
 sprintf(text,"{\"protocol\":1,\"session_id\":\"%s\",\"cleanup_verified\":true,\"completed_frames\":%lu,\"error\":%lu,\"log_segment\":%lu}\n",session,seq,l->first_error,persistent_log.segment);
 if(!receipt(p,"closed.json",text))result=1;
 ReleaseMutex(p->startup_mutex);CloseHandle(p->startup_mutex);
 free(p);free(l);active=NULL;return result;
}
static BOOL parse_exposure(const char*s){
 DWORD value=0,i;if(!s||!*s)return FALSE;
 for(i=0;s[i];i++){if(s[i]<'0'||s[i]>'9'||i>=4)return FALSE;value=value*10+(s[i]-'0');}
 if(value<10||value>1000)return FALSE;ws_exposure_ms=value;return TRUE;
}
int main(int argc,char**argv){
#if WS_INJECTED_TRANSPORT
 if(argc==8&&!strcmp(argv[1],"--offline-recipe-session")){if(!parse_exposure(argv[6]))return 64;injected_mode=(DWORD)strtoul(argv[7],NULL,10);return session_main(argv[2],argv[3],argv[4],(DWORD)strtoul(argv[5],NULL,10));}
 if((argc==6||argc==7)&&!strcmp(argv[1],"--offline-session")){injected_mode=(DWORD)strtoul(argv[argc-1],NULL,10);return session_main(argv[2],argv[3],argv[4],argc==7?(DWORD)strtoul(argv[5],NULL,10):67108864);}
#else
 if(argc==7&&!strcmp(argv[1],"--recipe-session")){if(!parse_exposure(argv[6]))return 64;return session_main(argv[2],argv[3],argv[4],(DWORD)strtoul(argv[5],NULL,10));}
 if((argc==5||argc==6)&&!strcmp(argv[1],"--session"))return session_main(argv[2],argv[3],argv[4],argc==6?(DWORD)strtoul(argv[5],NULL,10):67108864);
#endif
 return 64;
}
