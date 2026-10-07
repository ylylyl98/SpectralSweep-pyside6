/* Actual DLL lifecycle checks. All handles belong to this synthetic process. */
#define LIFECYCLE_FIXTURE 1
#define main replay_setup_main
#include "replay_test.c"
#undef main
#include <stdlib.h>
typedef BOOL (WINAPI *COUNT_HANDLES)(HANDLE,PDWORD);
static F3 direct_output;
static DWORD observed_rows,observed_records;
static DWORD WINAPI outside_detached(LPVOID ignored){return direct_output((DWORD)fake_port,0x22,0);}
static CONTROL ack_for(CONTROL*c){CONTROL a=*c;observed_rows=c->rows;observed_records=c->records;a.operation=6;a.mode=gate->generation;a.frame=gate->frame;return a;}
static int unchanged_rejection(CONTROL*a){
 BYTE*before;DWORD length=observed_records*140;REPLAY_GATE saved;CONTROL attempted=*a;int ok;
 /* An altered descriptor must not choose the snapshot address. Do not issue
  * a status call here: its normal publication would overwrite injected gate
  * faults and conceal whether op6 itself preserves a rejected request. */
 before=malloc(length?length:1);if(!before)return 0;
 CopyMemory(before,(void*)observed_rows,length);CopyMemory(&saved,(void*)gate,sizeof(saved));
 ok=!ctl(&attempted)&&!memcmp(before,(void*)observed_rows,length)&&!memcmp(&saved,(void*)gate,sizeof(saved));
 free(before);return ok;
}
static int read_packet(HANDLE server){BYTE bytes[375];DWORD n=0,i;
 if(!ReadFile(server,bytes,sizeof(bytes),&n,0)||n!=375)return 0;
 for(i=0;i<120;i++){
  DWORD at=(i/24)*75+3*(i%24)+3,bit=i<48?((0x88>>(15-i/3))&1):0;
  if(bytes[at]!=2||bytes[at+1]!=((i%3==1?14:12)|bit)||bytes[at+2])return 0;
 }
 return 1;
}
static int run_lifecycle(CONTROL*c,HANDLE server,HANDLE client,int argc,char**argv){
 HMODULE dll=GetModuleHandleA("batch_probe.dll");F1 event_handle;
 COUNT_HANDLES count_handles;DWORD original_event,handles_before,handles_after,i,j,total=0,previous=0;
 CONTROL ack,bad,status;BYTE*archived;DWORD length,available;HANDLE worker;
 event_handle=(F1)GetProcAddress(dll,"_BatchEventHandle@4");
 if(argc>1&&!strcmp(argv[1],"reset-only")){
  c->operation=4;if(!ctl(c))return 90;ack=ack_for(c);
  if(!ctl(&ack)){puts("missing archive-reset operation");return 91;}
  puts("{\"status\":\"passed\",\"case\":\"reset-only\"}");return 0;
 }
 if(!event_handle){puts("missing retained-event export");return 92;}
 original_event=event_handle(0);if(!original_event)return 93;
 if(argc>1&&(!strcmp(argv[1],"idle-hold")||!strcmp(argv[1],"idle-fault"))){
  if(!strcmp(argv[1],"idle-hold"))gate->hold=1;
  else{F1 fault_address=(F1)GetProcAddress(dll,"_BatchFaultAddress@4");*(DWORD*)fault_address(0)=83;}
  worker=CreateThread(0,0,run_outside,0,0,0);
  if(WaitForSingleObject(worker,200)!=WAIT_TIMEOUT)return 132;
  c->operation=0;if(!ctl(c)||c->records||c->active!=1||!c->fatal||!gate->hold)return 133;
  ack=ack_for(c);if(!unchanged_rejection(&ack))return 134;
  printf("{\"status\":\"passed\",\"case\":\"%s\",\"fatal\":%lu,\"unarmed\":true,\"records\":0,\"camera_sdk_loaded\":false}\n",argv[1],c->fatal);
  fflush(stdout);ExitProcess(0);
 }
 count_handles=(COUNT_HANDLES)GetProcAddress(GetModuleHandleA("kernel32.dll"),"GetProcessHandleCount");
 if(!count_handles||!count_handles((HANDLE)-1,&handles_before))return 94;
 if(argc>1&&!strcmp(argv[1],"events")){
  for(i=0;i<2000;i++){
   c->operation=4;if(!ctl(c))return 95;ack=ack_for(c);if(!ctl(&ack))return 96;
   c->operation=1;if(!ctl(c)||event_handle(0)!=original_event)return 97;
  }
  if(!count_handles((HANDLE)-1,&handles_after)||handles_after!=handles_before)return 98;
  printf("{\"status\":\"passed\",\"case\":\"events\",\"cycles\":2000,\"event_handle\":%lu,\"handles_before\":%lu,\"handles_after\":%lu}\n",original_event,handles_before,handles_after);
  return 0;
 }
 /* Idle callbacks must neither consume capacity nor leave rows after restore. */
 for(i=0;i<20000;i++)run_outside(0);
 c->operation=0;if(!ctl(c)||c->records)return 99;
 ack=ack_for(c);if(!unchanged_rejection(&ack))return 100; /* still installed */
 skip_first=1;
 for(i=0;i<120;i++){
  c->operation=2;c->mode=1;c->frame=i+1;if(!ctl(c)||gate->generation!=previous+1)return 101;
  previous=gate->generation;ack=ack_for(c);if(!unchanged_rejection(&ack))return 102;
  program_calls=2; /* Both real original frames execute; only the first body is omitted. */
  if(invoke_program((DWORD)fake_controller)!=1||invoke_program((DWORD)fake_controller)!=1)return 103;
  c->operation=3;if(!ctl(c)||c->batches!=5||!read_packet(server))return 104;
  if((i+1)%30)continue;
  c->operation=4;if(!ctl(c))return 105;ack=ack_for(c);length=ack.records*140;total+=ack.records;
  archived=malloc(length);if(!archived)return 106;CopyMemory(archived,(void*)ack.rows,length);
  bad=ack;bad.mode--;if(!unchanged_rejection(&bad))return 107;
  bad=ack;bad.frame++;if(!unchanged_rejection(&bad))return 108;
  bad=ack;bad.records--;if(!unchanged_rejection(&bad))return 109;
  bad=ack;bad.row_size++;if(!unchanged_rejection(&bad))return 110;
  bad=ack;bad.rows+=4;if(!unchanged_rejection(&bad))return 111;
  {volatile DWORD*fields[]={&gate->armed,&gate->active,&gate->phase,&gate->permit,&gate->hold,&gate->uncertain,&gate->fatal,&gate->buffered};
   for(j=0;j<8;j++){DWORD old=*fields[j];*fields[j]=1;if(!unchanged_rejection(&ack))return 112;*fields[j]=old;}}
  {ROW*r=(ROW*)ack.rows;DWORD old=r[0].committed;r[0].committed=0;
   if(!unchanged_rejection(&ack))return 113;r[0].committed=old;
   old=r[0].id;r[0].id=17;if(!unchanged_rejection(&ack))return 114;r[0].id=old;}
  {DWORD*pointer=(DWORD*)(fake_cm+0xf41e0),old=*pointer;*pointer=0;
   if(!unchanged_rejection(&ack))return 115;*pointer=old;}
  if(memcmp(archived,(void*)ack.rows,length))return 116;
  free(archived);
  /* A stale callback may enter after IAT restoration: it still counts active,
   * but cannot append records while the lifecycle is unarmed. */
  direct_output=(F3)GetProcAddress(dll,"_BatchOutput@12");
  outside_entered=CreateEventA(0,TRUE,FALSE,0);outside_release=CreateEventA(0,TRUE,FALSE,0);block_outside=1;
  worker=CreateThread(0,0,outside_detached,0,0,0);
  if(WaitForSingleObject(outside_entered,1000)!=WAIT_OBJECT_0)return 117;
  status=*c;status.operation=0;if(!ctl(&status)||status.active!=1||status.records!=ack.records)return 118;
  if(!unchanged_rejection(&ack))return 119;
  SetEvent(outside_release);if(WaitForSingleObject(worker,2000)!=WAIT_OBJECT_0)return 120;
  CloseHandle(worker);CloseHandle(outside_entered);CloseHandle(outside_release);block_outside=0;
  if(!ctl(&ack)||ack.records||gate->generation!=previous||gate->completed!=3||gate->batches!=5)return 121;
  for(j=0;j<length;j++)if(((BYTE*)ack.rows)[j])return 122;
  c->operation=1;if(!ctl(c)||event_handle(0)!=original_event)return 123;
 }
 if(total<=16384)return 124;
 /* Reset does not reset generation or make the previous nonce valid again. */
 c->operation=2;c->mode=1;c->frame=121;if(!ctl(c)||gate->generation!=previous+1)return 125;
 stale_permit=1;program_calls=2;
 if(invoke_program((DWORD)fake_controller)!=1||invoke_program((DWORD)fake_controller)!=1)return 126;
 c->operation=3;if(!ctl(c)||c->batches||gate->started||gate->data)return 127;
 if(!PeekNamedPipe(server,0,0,0,&available,0)||available)return 128;
 c->operation=4;if(!ctl(c))return 129;ack=ack_for(c);if(!ctl(&ack))return 130;
 if(!count_handles((HANDLE)-1,&handles_after)||handles_after!=handles_before)return 131;
 printf("{\"status\":\"passed\",\"case\":\"lifecycle\",\"frames\":121,\"archived_rows\":%lu,\"event_handle\":%lu,\"handles_before\":%lu,\"handles_after\":%lu,\"generation\":%lu,\"stale_nonce_packets\":0,\"camera_sdk_loaded\":false}\n",total,original_event,handles_before,handles_after,gate->generation);
 return 0;
}
int main(int argc,char**argv){return replay_setup_main(argc,argv);}
