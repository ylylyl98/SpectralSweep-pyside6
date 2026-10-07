/* Temporary, fixed-build XP x86 experiment. Nothing is installed on disk.
 * Only program #2 is eligible. Failed/unknown writes park the Start thread;
 * returning a false program result is insufficient in the original caller.
 */
#include <windows.h>
#define EXPORT __declspec(dllexport)
#define CAPACITY 16384
typedef DWORD (WINAPI *F1)(DWORD);
typedef DWORD (WINAPI *F3)(DWORD,DWORD,DWORD);
typedef DWORD (WINAPI *F4)(DWORD,DWORD,DWORD,DWORD);
typedef BOOL (WINAPI *WF)(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED);
typedef BOOL (WINAPI *GR)(HANDLE,LPOVERLAPPED,LPDWORD,BOOL);
typedef DWORD (WINAPI *WAIT)(HANDLE,DWORD);
typedef BOOL (WINAPI *CANCEL)(HANDLE);
#pragma pack(push,4)
typedef struct {DWORD committed,id,api,frame,program,thread,caller,command,value,result,error,length;
 LARGE_INTEGER begin,end;BYTE bytes[76];} ROW;
typedef struct {DWORD operation,cm,pipp,controller,port,pipe,handle,mode,frame,result,
 fatal,active,records,row_size,rows,programs,batches,uncertain;} CONTROL;
#pragma pack(pop)
static ROW rows[CAPACITY];
static volatile LONG reserved,active,fatal,installed,armed,current_program,program_thread,pending_unknown;
static DWORD cm,pb,controller,port,pipe_object,frame_id,mode,programs,batches;
static DWORD data_index,header_count,output_count,current_batch,serial_count;
static BYTE pulse_bits[40],pending_values[24],packet[75];
static OVERLAPPED write_ov;
static HANDLE command_handle,write_event;
static F1 original_program;static F3 original_output;static F4 original_multiple;
static WF api_write=WriteFile;static GR api_get=GetOverlappedResult;
static WAIT api_wait=WaitForSingleObject;static CANCEL api_cancel=CancelIo;

static int copy(DWORD address,void*buffer,DWORD size){SIZE_T got=0;
 return address&&ReadProcessMemory((HANDLE)-1,(void*)address,buffer,size,&got)&&got==size;
}
static int word(DWORD address,DWORD*value){return copy(address,value,4);}
static ROW*begin_row(DWORD api,DWORD caller,DWORD command,DWORD value){
 LONG n=InterlockedIncrement(&reserved)-1;ROW*r;
 if(n<0||n>=CAPACITY){InterlockedExchange(&fatal,90);return 0;}
 r=&rows[n];r->id=n;r->api=api;r->frame=frame_id;r->program=current_program;
 r->thread=GetCurrentThreadId();r->caller=caller;r->command=command;r->value=value;
 QueryPerformanceCounter(&r->begin);return r;
}
static void end_row(ROW*r,DWORD result,DWORD error){
 if(r){r->result=result;r->error=error;QueryPerformanceCounter(&r->end);InterlockedExchange((LONG*)&r->committed,1);}
}
static DWORD expected_site(DWORD index){
 static DWORD a[3]={0x39553,0x39578,0x3959a};
 static DWORD b[3]={0x392ff,0x3932a,0x39352};
 static DWORD c[3]={0x3938c,0x393ac,0x393c9};
 if(index<48)return a[index%3];if(index<84)return b[(index-48)%3];
 if(index<120)return c[(index-84)%3];return 0;
}
static int is_data_site(DWORD rva){DWORD i;for(i=0;i<120;i++)if(expected_site(i)==rva)return 1;return 0;}
static int pulse_valid(DWORD index,DWORD value){
 DWORD phase=index%3;
 if(value!=(phase==1?14:12)&&value!=(phase==1?15:13))return 0;
 if(index>=84&&value!=(phase==1?14:12))return 0;
 if(!phase){pulse_bits[index/3]=(BYTE)(value&1);return 1;}
 return (value&1)==pulse_bits[index/3];
}
static int serial_valid(DWORD index,DWORD site,DWORD value){
 if(index==0||index==52)return site==0x391a3&&value==0;
 if(index==1||index==53)return site==0x391b9&&value==8;
 if(index==50)return site==0x39242&&value==8;
 if(index==51)return site==0x39257&&value==0;
 if(index==126)return site==0x393e1&&value==8;
 if(index==127)return site==0x393f7&&value==0;
 if(index>=128)return 0;
 index-=index<50?2:6;
 return expected_site(index)==site&&pulse_valid(index,value);
}
static int header_valid(DWORD index,BYTE*bytes){
 DWORD i,value=index==0?5:4,bit;if(index>=2)return 0;
 for(i=0;i<8;i++){bit=(value>>(7-i))&1;
  if(bytes[i*3]!=(12|bit)||bytes[i*3+1]!=(14|bit)||bytes[i*3+2]!=(12|bit))return 0;}
 return 1;
}

static int write_batch(BYTE*values){
 DWORD i,got=0,error=0,wait=0;BOOL ok;ROW*r;
 if(pending_unknown)return 0;
 ZeroMemory(&write_ov,sizeof(write_ov));write_ov.hEvent=write_event;
 if(!ResetEvent(write_event))return 0;
 packet[0]=1;packet[1]=0x4a;packet[2]=0;
 for(i=0;i<24;i++){packet[3+3*i]=2;packet[4+3*i]=values[i];packet[5+3*i]=0;}
 r=begin_row(4,0,0x4a,24);if(!r)return 0;
 r->length=75;CopyMemory(r->bytes,packet,75);
 ok=api_write(command_handle,packet,75,&got,&write_ov);error=GetLastError();
 if(!ok&&error==ERROR_IO_PENDING){
  pending_unknown=1;wait=api_wait(write_event,1000);
  if(wait==WAIT_OBJECT_0){
   ok=api_get(command_handle,&write_ov,&got,FALSE);error=GetLastError();
   if(ok||error!=ERROR_IO_INCOMPLETE)pending_unknown=0;
  }else{
   api_cancel(command_handle); /* Same initiating thread; never cancel from controller thread. */
   wait=api_wait(write_event,1000);
   if(wait==WAIT_OBJECT_0){
    BOOL finished=api_get(command_handle,&write_ov,&got,FALSE);error=GetLastError();
    if(finished||error!=ERROR_IO_INCOMPLETE)pending_unknown=0;
   }
   ok=FALSE;error=ERROR_TIMEOUT; /* No retry, even if completion raced cancellation. */
  }
 }
 end_row(r,ok?got:0,error);
 return ok&&got==75&&!pending_unknown;
}

#ifndef UNIT_TEST
/* Only these 30 words are shared with the debugger. It publishes endpoint
 * identity/phase and a generation-bound permission while every thread is
 * suspended; no controller state, original code or program pointer is changed. */
#pragma pack(push,4)
typedef struct {DWORD magic,version,size,generation,frame,armed,mode,phase,tid,entry_esp,caller_ebp,controller,permit,completed,hold,observed_phase,programs,outputs,headers,others,serial,data,buffered,batches,started,candidate,fatal,active,uncertain,prefix_ok;} GATE;
#pragma pack(pop)
static volatile GATE gate={0x42534754,1,sizeof(GATE)};
static DWORD generation,candidate,started,pipe_flags,pipe_index,other_multiple_count;
static DWORD observed_phase,prefix_ok,shape_valid,first_short,eligible_decided;
static DWORD next_serial;

static void publish(void){
 gate.programs=programs;gate.outputs=output_count;gate.headers=header_count;
 gate.others=other_multiple_count;gate.serial=serial_count;gate.data=data_index;
 gate.buffered=current_batch;gate.batches=batches;gate.started=started;
 gate.candidate=candidate;gate.fatal=fatal;gate.active=active;
 gate.uncertain=pending_unknown;gate.prefix_ok=prefix_ok;gate.observed_phase=observed_phase;
}
static void park(DWORD code){
 InterlockedCompareExchange(&fatal,code,0);gate.hold=1;publish();
 for(;;)Sleep(1000); /* Owner and remote buffers are intentionally pinned. */
}
static int pipe_valid(void){
 DWORD a,b,c,d,e;BYTE keep;WORD signature;
 return word(controller+0x6e58,&a)&&a==port&&word(port+0x4f0,&b)&&b==pipe_object
  &&copy(port+0x367,&keep,1)&&keep==1&&word(pipe_object,&a)&&a==pipe_index
  &&word(pipe_object+4,&b)&&b==(DWORD)command_handle
  &&word(pipe_object+8,&c)&&c==pipe_flags&&(c&FILE_FLAG_OVERLAPPED)
  &&copy(pipe_object+12,&signature,2)&&signature==0x4950
  &&word(port+0x18,&d)&&d==pb+0xdafb&&word(port+0x48,&e)&&e==pb+0xdc92;
}
static void check_live(void){if(fatal||gate.hold)park(fatal?fatal:106);if(!pipe_valid())park(91);}
static int exact_scope(DWORD ebp,DWORD phase){
 DWORD at=ebp,saved,ret,arg,n,target=gate.entry_esp-4;
 if(gate.tid!=GetCurrentThreadId()||!target||target<ebp||target-ebp>=1048576)return 0;
 for(n=0;n<64;n++){
  if((at&3)||at<ebp||at-ebp>=1048576||!word(at,&saved))return 0;
  if(at==target)return saved==gate.caller_ebp&&saved>at&&saved-ebp<1048576
    &&word(at+4,&ret)&&ret==cm+0xde32&&word(at+8,&arg)&&arg==controller;
  if(saved<=at||saved>target)return 0;at=saved;
 }
 return 0;
}
static int contains_original(DWORD ebp){
 DWORD at=ebp,saved,ret,arg,n;
 for(n=0;n<64;n++){
  if((at&3)||at<ebp||at-ebp>=1048576||!word(at,&saved))return 0;
  if(word(at+4,&ret)&&ret==cm+0xde32&&word(at+8,&arg)&&arg==controller)return 1;
  if(saved<=at||saved-ebp>=1048576)return 0;at=saved;
 }
 return 0;
}
static void enter_scope(DWORD p,DWORD ebp){
 DWORD phase=gate.phase,x;
 if(fatal||gate.hold)park(fatal?fatal:106);
 current_program=0;
 if(!armed)return;
 if(gate.magic!=0x42534754||gate.version!=1||gate.size!=sizeof(GATE)||gate.generation!=generation
  ||gate.frame!=frame_id||gate.armed!=1||gate.mode!=mode||gate.controller!=controller)park(107);
 if(!phase){if(contains_original(ebp))park(108);return;}
 if(phase>2||p!=port||!exact_scope(ebp,phase)||!word(controller+0x618,&x)||x!=(DWORD)original_program)park(109);
 if(observed_phase!=phase){
  if((phase==1&&(observed_phase||programs||gate.completed))
    ||(phase==2&&(observed_phase!=1||programs!=1||gate.completed!=1)))park(110);
  first_short=phase==2&&output_count==2&&prefix_ok&&!header_count&&!other_multiple_count&&!serial_count&&!data_index&&!batches;
  observed_phase=phase;programs++;output_count=header_count=other_multiple_count=serial_count=next_serial=0;
  data_index=current_batch=candidate=started=eligible_decided=prefix_ok=0;shape_valid=1;
  ZeroMemory(pulse_bits,sizeof(pulse_bits));
 }
 if((phase==1&&gate.completed)||(phase==2&&gate.completed!=1))park(111);
 current_program=phase;publish();
}
static void mismatch(DWORD code){if(started)park(code);shape_valid=0;candidate=0;}
static int nonserial_valid(DWORD index,DWORD site,DWORD command){
 static DWORD commands[24]={0x40,0x40,0,0x30,0x30,0x30,0x22,0x24,0x22,0x24,0x22,0x24,0x42,0x44,0x40,0x40,0x50,0x34,0x32,0x34,0x38,0x36,0x3a,0x3c};
 static DWORD tail[5]={0x30,0x30,0x30,0,0xfe};
 if(index<24)return site==0x7395&&command==commands[index];
 if(index>=152&&index<157)return site==(index==156?0x77f4:0x7395)&&command==tail[index-152];
 return 0;
}
EXPORT DWORD WINAPI BatchOutput(DWORD p,DWORD command,DWORD value){
 DWORD caller,ebp,site,result,error=GetLastError(),idx,serial_before;ROW*r;
 __asm__("movl 4(%%ebp), %0":"=r"(caller));__asm__("movl %%ebp, %0":"=r"(ebp));
 InterlockedIncrement(&active);
 if(fatal||gate.hold)park(fatal?fatal:106);
 if(!armed){
  /* No record reservation while idle, including a callback that fetched the
   * old IAT target before a completed restore. Keep activity/fault ownership. */
  publish();SetLastError(error);result=original_output(p,command,value);error=GetLastError();
  InterlockedDecrement(&active);publish();SetLastError(error);return result;
 }
 enter_scope(p,ebp);site=caller-cm;
 r=begin_row(2,site,command,value);if(!r)park(90);
 if(current_program){
  idx=output_count++;serial_before=serial_count;
  if(command==0x4a){
   serial_count++;
   if(idx<24||idx>=152||serial_before!=idx-24||!serial_valid(serial_before,site,value))mismatch(94);
  }else if(!nonserial_valid(idx,site,command))mismatch(94);
  if(idx<2&&(site!=0x7395||command!=0x40||value!=(idx?0xd5:0x55)))mismatch(94);
  if(is_data_site(site)){
   if(command!=0x4a||next_serial>=120||site!=expected_site(next_serial))mismatch(95);
   if(!eligible_decided){
    eligible_decided=1;
    candidate=mode&&current_program==2&&first_short&&gate.permit==generation&&shape_valid&&next_serial==0
      &&prefix_ok&&header_count==1&&other_multiple_count==2&&idx==26&&pipe_valid();
   }
   next_serial++;
   if(candidate){
    if(gate.permit!=generation||!shape_valid)park(112);check_live();
    started=1;pending_values[current_batch++]=(BYTE)value;data_index++;
    if(current_batch==24){if(!write_batch(pending_values))park(96);current_batch=0;batches++;}
    end_row(r,6,error);InterlockedDecrement(&active);publish();SetLastError(error);return 6;
   }
  }else if(current_batch)park(97);
 }
 SetLastError(error);result=original_output(p,command,value);error=GetLastError();
 if(current_program&&result!=6){end_row(r,result,error);park(98);}
 if(current_program&&idx==1&&shape_valid)prefix_ok=1;
 end_row(r,result,error);InterlockedDecrement(&active);publish();SetLastError(error);return result;
}
EXPORT DWORD WINAPI BatchMultiple(DWORD p,DWORD command,DWORD values,DWORD length){
 DWORD caller,ebp,site,result,error=GetLastError(),n;BYTE bytes[24];ROW*r;
 __asm__("movl 4(%%ebp), %0":"=r"(caller));__asm__("movl %%ebp, %0":"=r"(ebp));
 InterlockedIncrement(&active);
 if(fatal||gate.hold)park(fatal?fatal:106);
 if(!armed){
  publish();SetLastError(error);result=original_multiple(p,command,values,length);error=GetLastError();
  InterlockedDecrement(&active);publish();SetLastError(error);return result;
 }
 enter_scope(p,ebp);site=caller-cm;
 r=begin_row(3,site,command,length);if(!r)park(90);
 if(current_program){
  if(command!=0x4a||site!=0x394cd){
   n=other_multiple_count++;
   if(site!=0x6e88||command!=0x20||n>=2||length!=(n?512:520)||output_count!=(n?10:8)||header_count||current_batch)mismatch(99);
  }else{
   n=header_count++;
   if(length!=24||!copy(values,bytes,24)||!header_valid(n,bytes)||output_count!=(n?78:26)
      ||serial_count!=(n?54:2)||current_batch||other_multiple_count!=2)mismatch(100);
   else{r->length=24;CopyMemory(r->bytes,bytes,24);}
  }
 }
 SetLastError(error);result=original_multiple(p,command,values,length);error=GetLastError();
 end_row(r,result,error);InterlockedDecrement(&active);publish();SetLastError(error);return result;
}
static int replace(DWORD address,DWORD before,DWORD after){
 DWORD protection,previous,now;MEMORY_BASIC_INFORMATION mbi;
 if(!word(address,&now)||now!=before||(address&3))return 0;
 if(!VirtualQuery((void*)address,&mbi,sizeof(mbi))||mbi.State!=MEM_COMMIT||
  (mbi.Protect!=PAGE_READONLY&&mbi.Protect!=PAGE_READWRITE))return 0;
 previous=mbi.Protect;
 if(!VirtualProtect((void*)address,4,PAGE_READWRITE,&protection)||protection!=previous)return 0;
 now=InterlockedCompareExchange((LONG*)address,after,before);
 if(!VirtualProtect((void*)address,4,protection,&previous))return 0;
 return now==before&&word(address,&now)&&now==after;
}
EXPORT DWORD WINAPI BatchControl(CONTROL*a){
 DWORD x,i,ok=0,reasons=0;CONTROL cfg;ROW*observation;
 if(!a||!copy((DWORD)a,&cfg,sizeof(cfg)))return 0;
 if(cfg.operation==1){
  if(installed||active||fatal||armed||pending_unknown||current_batch||gate.hold||gate.phase||gate.permit||(gate.controller&&gate.controller!=cfg.controller))return 0;
  cm=cfg.cm;pb=cfg.pipp;controller=cfg.controller;port=cfg.port;pipe_object=cfg.pipe;command_handle=(HANDLE)cfg.handle;
  original_program=(F1)(cm+0xc65fa);original_output=(F3)(pb+0x1199d);original_multiple=(F4)(pb+0x11778);
  if(!word(pipe_object,&pipe_index)||!word(pipe_object+8,&pipe_flags)||!pipe_valid())return 0;
  if(!word(controller+0x618,&x)||x!=(DWORD)original_program||!word(cm+0xf41e0,&x)||x!=(DWORD)original_output||!word(cm+0xf41e8,&x)||x!=(DWORD)original_multiple)return 0;
  gate.controller=controller; /* Permanent identity after this first validated installation. */
  if(!write_event){write_event=CreateEventA(0,TRUE,FALSE,0);if(!write_event)return 0;}
  else if(!ResetEvent(write_event))return 0; /* Retain and reuse this process-lifetime event. */
  if(!replace(cm+0xf41e0,(DWORD)original_output,(DWORD)BatchOutput)||!replace(cm+0xf41e8,(DWORD)original_multiple,(DWORD)BatchMultiple)){fatal=103;publish();return 0;}
  installed=1;ok=1;
 }else if(cfg.operation==2){
  if(!installed||active||fatal||armed||pending_unknown||gate.hold||cfg.mode>1||!pipe_valid()||reserved>CAPACITY-1500||generation==0xffffffff)return 0;
  mode=cfg.mode;frame_id=cfg.frame;programs=batches=data_index=current_batch=current_program=0;
  candidate=started=observed_phase=output_count=header_count=other_multiple_count=serial_count=next_serial=prefix_ok=first_short=eligible_decided=0;
  shape_valid=1;generation++;
  /* The asynchronous fault observer may read at any instruction. Keep magic,
   * version, size and controller permanent; publish armed only after the new
   * generation and every mutable per-frame field have been initialized. */
  gate.generation=generation;gate.frame=frame_id;gate.mode=mode;
  gate.phase=gate.tid=gate.entry_esp=gate.caller_ebp=gate.permit=gate.completed=gate.hold=0;
  publish();InterlockedExchange(&armed,1);InterlockedExchange((LONG*)&gate.armed,1);ok=1;
 }else if(cfg.operation==3){
  reasons=(active?1:0)|((fatal||gate.hold)?2:0)|(pending_unknown?4:0)|(current_batch?8:0)|((gate.phase||gate.permit)?16:0)|((programs!=2||gate.completed!=3)?32:0);
  current_program=gate.phase;observation=begin_row(5,0,3,reasons);end_row(observation,!reasons,reasons);
  if(reasons)goto report;
  InterlockedExchange(&armed,0);InterlockedExchange((LONG*)&gate.armed,0);ok=1;
 }else if(cfg.operation==4){
  if(!installed||active||fatal||armed||pending_unknown||current_batch||gate.hold||gate.phase)return 0;
  if(!word(controller+0x618,&x)||x!=(DWORD)original_program||!replace(cm+0xf41e8,(DWORD)BatchMultiple,(DWORD)original_multiple)
     ||!replace(cm+0xf41e0,(DWORD)BatchOutput,(DWORD)original_output)){fatal=104;publish();return 0;}
  if(active){fatal=105;publish();return 0;}installed=0;ok=1;
 }else if(cfg.operation==5){
  if(!installed||!armed||active||fatal||pending_unknown||programs||batches||current_batch||gate.phase||gate.completed||gate.permit||gate.hold)return 0;
  InterlockedExchange(&armed,0);InterlockedExchange((LONG*)&gate.armed,0);ok=1;
 }else if(cfg.operation==6){
  /* The owner attests that these exact records were durably archived. The
   * helper checks identity/quiescence, never discards a fault or unknown I/O.
   * Rejection returns without even publishing over the supplied gate state. */
  if(installed||armed||active||fatal||pending_unknown||current_batch||current_program
     ||gate.armed||gate.active||gate.fatal||gate.uncertain||gate.buffered||gate.phase||gate.permit||gate.hold
     ||gate.magic!=0x42534754||gate.version!=1||gate.size!=sizeof(GATE)||gate.controller!=controller
     ||gate.generation!=generation||gate.frame!=frame_id||cfg.mode!=generation||cfg.frame!=frame_id
     ||reserved<0||reserved>CAPACITY||cfg.records!=(DWORD)reserved||cfg.rows!=(DWORD)rows||cfg.row_size!=sizeof(ROW))return 0;
  if(!word(controller+0x618,&x)||x!=(DWORD)original_program
     ||!word(cm+0xf41e0,&x)||x!=(DWORD)original_output
     ||!word(cm+0xf41e8,&x)||x!=(DWORD)original_multiple)return 0;
  for(i=0;i<(DWORD)reserved;i++)if(rows[i].committed!=1||rows[i].id!=i)return 0;
  /* No armed wrappers exist; any late detached callback only forwards and
   * cannot reserve or mutate rows. Publish the empty count after clearing. */
  ZeroMemory(rows,reserved*sizeof(ROW));InterlockedExchange(&reserved,0);ok=1;
 }else if(cfg.operation==0)ok=1;
 report:
 publish();if(cfg.operation==3)a->mode=reasons;
 a->result=ok;a->fatal=fatal;a->active=active;a->records=reserved;a->row_size=sizeof(ROW);a->rows=(DWORD)rows;
 a->programs=programs;a->batches=batches;a->uncertain=pending_unknown;
 return ok;
}
EXPORT DWORD WINAPI BatchFaultAddress(LPVOID ignored){return (DWORD)&fatal;}
EXPORT DWORD WINAPI BatchGateAddress(LPVOID ignored){return (DWORD)&gate;}
EXPORT DWORD WINAPI BatchEventHandle(LPVOID ignored){return (DWORD)write_event;}
BOOL WINAPI _dllstart(HINSTANCE h,DWORD reason,LPVOID p){return TRUE;}
#endif
