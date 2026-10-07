/* Run the actual diagnostic DLL and calling conventions against a named pipe.
 * The synthetic address space contains no camera SDK and never opens a camera.
 */
#define UNIT_TEST 1
#include "batch_probe.c"
#include <stdio.h>
static BYTE *fake_cm,*fake_pb,*fake_controller,*fake_port,*fake_pipe;
typedef struct {DWORD magic,version,size,generation,frame,armed,mode,phase,tid,entry_esp,caller_ebp,controller,permit,completed,hold,observed_phase,programs,outputs,headers,others,serial,data,buffered,batches,started,candidate,fatal,active,uncertain,prefix_ok;} REPLAY_GATE;
static volatile REPLAY_GATE *gate;
static F1 invoke_program;static DWORD single_calls,multiple_calls,program_calls,bad_sequence,pre_failure,bad_shape,skip_first;
typedef DWORD (WINAPI *CTL)(CONTROL*);
static CTL ctl;
#ifdef LIFECYCLE_FIXTURE
static int run_lifecycle(CONTROL*c,HANDLE server,HANDLE client,int argc,char**argv);
#endif
#ifdef DEBUG_FIXTURE
static int run_debug_fixture(CONTROL*c,HANDLE server,HANDLE client,int argc,char**argv);
#endif
static HANDLE outside_entered,outside_release;
static DWORD block_outside,missing_permit,stale_permit,bad_scope,changed_bits,synthetic_phase,revoke_permit,bad_padding,bad_phase,bad_thread,held_entry,full_first,mode_zero,first_pulse_bad,second_header_bad;
static void jump(BYTE*address,void*fn){address[0]=0xe9;*(DWORD*)(address+1)=(DWORD)fn-(DWORD)address-5;}
static DWORD WINAPI output(DWORD p,DWORD c,DWORD v){single_calls++;if(block_outside){SetEvent(outside_entered);WaitForSingleObject(outside_release,3000);}if(pre_failure&&program_calls==4&&c==0x4a&&v==0)return 0;return 6;}
static DWORD WINAPI multiple(DWORD p,DWORD c,DWORD bytes,DWORD n){multiple_calls++;return 0;}
static void*thunk(DWORD site,DWORD argc,DWORD slot){
 BYTE*p=fake_cm+site-(4*argc+6);DWORD i;
 for(i=0;i<argc;i++){p[4*i]=0xff;p[4*i+1]=0x74;p[4*i+2]=0x24;p[4*i+3]=(BYTE)(4*argc);}
 p+=4*argc;p[0]=0xff;p[1]=0x15;*(DWORD*)(p+2)=slot;
 p[6]=0xc2;*(WORD*)(p+7)=(WORD)(4*argc);
 return fake_cm+site-(4*argc+6);
}
static void emit(DWORD site,DWORD command,DWORD value){F3 f=(F3)(fake_cm+site-18);f((DWORD)fake_port,command,value);}
static void header(DWORD value){BYTE values[24];DWORD i,bit;F4 f=(F4)(fake_cm+0x394cd-22);
 if(bad_shape==2||(second_header_bad&&value==4))value=6;
 for(i=0;i<8;i++){bit=(value>>(7-i))&1;values[3*i]=(BYTE)(12|bit);values[3*i+1]=(BYTE)(14|bit);values[3*i+2]=(BYTE)(12|bit);}
 f((DWORD)fake_port,0x4a,(DWORD)values,24);
}
static DWORD WINAPI program(DWORD c){DWORD i,j,v,site,ebp;DWORD prefix_commands[24]={0x40,0x40,0,0x30,0x30,0x30,0x22,0x24,0x22,0x24,0x22,0x24,0x42,0x44,0x40,0x40,0x50,0x34,0x32,0x34,0x38,0x36,0x3a,0x3c};
 DWORD prefix_values[24]={0x55,0xd5,0,2,0,1,0,1,126,1,0,1,1,1,214,213,128,128,250,1,1,0,2,0};
 DWORD trailing_commands[5]={0x30,0x30,0x30,0,0xfe};
 BYTE block[520];F4 block_output=(F4)(fake_cm+0x6e88-22);ZeroMemory(block,sizeof(block));
 __asm__("movl %%ebp, %0":"=r"(ebp));
 program_calls++;synthetic_phase=(program_calls&1)?1:2;
 gate->phase=synthetic_phase;gate->tid=GetCurrentThreadId();gate->entry_esp=ebp+4;gate->caller_ebp=*(DWORD*)ebp;
 if(bad_scope&&program_calls==4)gate->entry_esp+=4;
 if(bad_thread&&program_calls==4)gate->tid++;
 if(bad_phase&&program_calls==4)gate->phase=0;
 if(held_entry&&program_calls==4)gate->hold=1;
 for(i=0;i<2;i++)emit(0x7395,prefix_commands[i],prefix_values[i]);
 if(gate->outputs!=2||!gate->prefix_ok||gate->headers||gate->others||gate->serial){bad_sequence=99;return 0;}
 if(skip_first&&synthetic_phase==1)goto complete;
 if(synthetic_phase==2&&!missing_permit&&program_calls==4)gate->permit=stale_permit?gate->generation-1:gate->generation;
 for(i=2;i<24;i++){emit(0x7395,prefix_commands[i],prefix_values[i]);
  if(i==7)block_output((DWORD)fake_port,0x20,(DWORD)block,520);
  if(i==9)block_output((DWORD)fake_port,0x20,(DWORD)block,512);}
 emit(0x391a3,0x4a,0);emit(0x391b9,0x4a,8);header(5);
 for(i=0;i<16;i++)for(j=0;j<3;j++){v=((changed_bits?0xa531:0x88)>>(15-i))&1;site=expected_site(i*3+j);
  if(bad_sequence&&program_calls==4&&i*3+j==25)v^=1;
  if(revoke_permit&&program_calls==4&&i*3+j==25)gate->permit=0;
  emit(site,0x4a,(first_pulse_bad&&i==0&&j==0)?2:((bad_shape==1&&j==1?12:(j==1?14:12))|v));}
 emit(0x39242,0x4a,8);emit(0x39257,0x4a,0);
 emit(0x391a3,0x4a,0);emit(0x391b9,0x4a,8);header(4);
 for(i=0;i<72;i++)emit(expected_site(i+48),0x4a,(i%3==1?14:12)|((bad_padding&&program_calls==4&&i>=36)?1:0));
 emit(0x393e1,0x4a,8);emit(0x393f7,0x4a,0);
 for(i=0;i<5;i++)emit(i==4?0x77f4:0x7395,trailing_commands[i],i<3?i:0);
 if(gate->outputs!=157||gate->headers!=2||gate->others!=2||gate->serial!=128)return 0;
 if(gate->started&&(gate->data!=120||gate->buffered||gate->batches!=5))return 0;
 complete:
 gate->completed|=1<<(synthetic_phase-1);gate->phase=0;gate->permit=0;gate->tid=gate->entry_esp=gate->caller_ebp=0;
 return 1;
}
static volatile LONG identity_stop,identity_reads,identity_bad;
static HANDLE identity_ready;
static DWORD WINAPI observe_identity(LPVOID ignored){
 while(!identity_stop){
  if(gate->magic!=0x42534754||gate->version!=1||gate->size!=120||gate->controller!=(DWORD)fake_controller)InterlockedIncrement(&identity_bad);
  {LONG n=InterlockedIncrement(&identity_reads);
   if(n==1)SetEvent(identity_ready); /* XP single-CPU: signal after the first actual read. */
   if(!(n&255))SwitchToThread();}
 }
 return 1;
}
static DWORD WINAPI run_program(LPVOID ignored){return invoke_program((DWORD)fake_controller);}
static DWORD WINAPI run_outside(LPVOID ignored){emit(0x7395,0x22,0);return 1;}
int main(int argc,char**argv){
 HMODULE dll;CONTROL c;DWORD i,got,result,before,old_protection;HANDLE server,client,worker;BYTE received[375];char name[100];
 dll=LoadLibraryA("batch_probe.dll");if(!dll){printf("load failed %lu\n",GetLastError());return 2;}
 ctl=(CTL)GetProcAddress(dll,"_BatchControl@4");if(!ctl)return 3;
 {F1 address=(F1)GetProcAddress(dll,"_BatchGateAddress@4");if(!address){puts("missing combined gate export");return 34;}gate=(REPLAY_GATE*)address(0);
 if(gate->magic!=0x42534754||gate->version!=1||gate->size!=120)return 35;}
 fake_cm=VirtualAlloc(0,0x120000,MEM_COMMIT|MEM_RESERVE,PAGE_EXECUTE_READWRITE);
 fake_pb=VirtualAlloc(0,0x40000,MEM_COMMIT|MEM_RESERVE,PAGE_EXECUTE_READWRITE);
 fake_controller=VirtualAlloc(0,0x10000,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
 fake_port=VirtualAlloc(0,4096,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);fake_pipe=fake_port+2048;
 sprintf(name,"\\\\.\\pipe\\ss-batch-replay-%lu",GetCurrentProcessId());
 server=CreateNamedPipeA(name,PIPE_ACCESS_INBOUND,PIPE_TYPE_BYTE|PIPE_WAIT,1,4096,4096,1000,0);
 client=CreateFileA(name,GENERIC_WRITE,0,0,OPEN_EXISTING,FILE_FLAG_OVERLAPPED,0);
 if(server==INVALID_HANDLE_VALUE||client==INVALID_HANDLE_VALUE)return 4;
 ConnectNamedPipe(server,0);
 *(DWORD*)(fake_pipe)=2;*(DWORD*)(fake_pipe+4)=(DWORD)client;*(DWORD*)(fake_pipe+8)=FILE_FLAG_OVERLAPPED;*(WORD*)(fake_pipe+12)=0x4950;
 *(DWORD*)(fake_port+0x18)=(DWORD)fake_pb+0xdafb;*(DWORD*)(fake_port+0x48)=(DWORD)fake_pb+0xdc92;
 *(DWORD*)(fake_port+0x4f0)=(DWORD)fake_pipe;fake_port[0x367]=1;
 *(DWORD*)(fake_controller+0x6e58)=(DWORD)fake_port;*(DWORD*)(fake_controller+0x618)=(DWORD)fake_cm+0xc65fa;
 *(DWORD*)(fake_cm+0xf41e0)=(DWORD)fake_pb+0x1199d;*(DWORD*)(fake_cm+0xf41e8)=(DWORD)fake_pb+0x11778;
 jump(fake_cm+0xc65fa,program);jump(fake_pb+0x1199d,output);jump(fake_pb+0x11778,multiple);
 for(i=0;i<120;i++)thunk(expected_site(i),3,(DWORD)fake_cm+0xf41e0);
 {DWORD sites[]={0x77f4,0x7395,0x391a3,0x391b9,0x39242,0x39257,0x393e1,0x393f7};
  for(i=0;i<8;i++)thunk(sites[i],3,(DWORD)fake_cm+0xf41e0);}
 thunk(0x394cd,4,(DWORD)fake_cm+0xf41e8);
 thunk(0x6e88,4,(DWORD)fake_cm+0xf41e8);
 invoke_program=(F1)thunk(0xde32,1,(DWORD)fake_controller+0x618);
 if(!VirtualProtect(fake_cm+0xf4000,4096,PAGE_READWRITE,&old_protection))return 20;
 ZeroMemory(&c,sizeof(c));c.operation=1;c.cm=(DWORD)fake_cm;c.pipp=(DWORD)fake_pb;
 c.controller=(DWORD)fake_controller;c.port=(DWORD)fake_port;c.pipe=(DWORD)fake_pipe;c.handle=(DWORD)client;
 if(!ctl(&c)||c.row_size!=140)return 5;
 if(*(DWORD*)(fake_controller+0x618)!=(DWORD)fake_cm+0xc65fa)return 31;
#ifdef LIFECYCLE_FIXTURE
 return run_lifecycle(&c,server,client,argc,argv);
#endif
#ifdef DEBUG_FIXTURE
 return run_debug_fixture(&c,server,client,argc,argv);
#endif
 if(argc>1&&!strcmp(argv[1],"identity-race")){
  DWORD previous=gate->generation;
  identity_ready=CreateEventA(0,TRUE,FALSE,0);worker=CreateThread(0,0,observe_identity,0,0,0);
  if(WaitForSingleObject(identity_ready,1000)!=WAIT_OBJECT_0)return 40;
  for(i=0;i<10000;i++){
   c.operation=2;c.mode=i&1;c.frame=i;
   if(!ctl(&c)||gate->generation!=previous+1||gate->armed!=1||gate->frame!=i||gate->mode!=(i&1))return 41;
   previous=gate->generation;
   c.operation=5;if(!ctl(&c)||gate->armed)return 42;
   if(!(i&31))SwitchToThread();
  }
  InterlockedExchange(&identity_stop,1);
  if(WaitForSingleObject(worker,1000)!=WAIT_OBJECT_0)return 43;
  c.operation=4;if(!ctl(&c))return 44;
  printf("{\"status\":\"%s\",\"case\":\"identity-race\",\"rounds\":10000,\"reads\":%ld,\"identity_errors\":%ld,\"camera_sdk_loaded\":false}\n",identity_bad?"failed":"passed",identity_reads,identity_bad);
  return identity_bad||!identity_reads?45:0;
 }
 c.operation=2;c.mode=1;c.frame=0;if(!ctl(&c))return 22;
 c.operation=5;if(!ctl(&c)||c.programs||c.batches)return 23;
 c.operation=2;c.mode=0;c.frame=1;if(!ctl(&c))return 6;
 if(invoke_program((DWORD)fake_controller)!=1||invoke_program((DWORD)fake_controller)!=1)return 7;
 c.operation=3;if(!ctl(&c)||single_calls!=314||multiple_calls!=8||c.batches)return 8;
 mode_zero=argc>1&&!strcmp(argv[1],"mode-zero");
 c.operation=2;c.mode=mode_zero?0:1;c.frame=2;if(!ctl(&c))return 9;
 skip_first=1;
 if(argc>1&&!strcmp(argv[1],"full-first")){full_first=1;skip_first=0;}
 if(argc>1&&!strcmp(argv[1],"first-pulse"))first_pulse_bad=1;
 if(argc>1&&!strcmp(argv[1],"second-header"))second_header_bad=1;
 if(argc>1&&!strcmp(argv[1],"pulse-shape"))bad_shape=1;
 if(argc>1&&!strcmp(argv[1],"header-shape"))bad_shape=2;
 if(argc>1&&!strcmp(argv[1],"missing-permit"))missing_permit=1;
 if(argc>1&&!strcmp(argv[1],"stale-permit"))stale_permit=1;
 if(argc>1&&!strcmp(argv[1],"changed-bits"))changed_bits=1;
 if(argc>1&&!strcmp(argv[1],"scope"))bad_scope=1;
 if(argc>1&&!strcmp(argv[1],"thread"))bad_thread=1;
 if(argc>1&&!strcmp(argv[1],"phase"))bad_phase=1;
 if(argc>1&&!strcmp(argv[1],"hold"))held_entry=1;
 if(argc>1&&!strcmp(argv[1],"revoke-permit"))revoke_permit=1;
 if(argc>1&&!strcmp(argv[1],"padding"))bad_padding=1;
 if(argc>1&&!strcmp(argv[1],"sequence"))bad_sequence=1;
 if(argc>1&&!strcmp(argv[1],"pre-failure"))pre_failure=1;
 before=single_calls;if(invoke_program((DWORD)fake_controller)!=1)return 10;
 if(argc>1&&(bad_sequence||pre_failure||bad_scope||bad_thread||bad_phase||held_entry||revoke_permit||bad_padding||bad_shape==1||second_header_bad||!strcmp(argv[1],"io-error"))){
  if(!strcmp(argv[1],"io-error"))CloseHandle(client);
  worker=CreateThread(0,0,run_program,0,0,0);
  if(WaitForSingleObject(worker,2200)!=WAIT_TIMEOUT)return 11;
  c.operation=0;if(!ctl(&c)||!c.fatal||!c.active||!gate->fatal||!gate->hold)return 12;
  if((bad_sequence||revoke_permit)&&(c.batches!=1||gate->buffered!=1))return 36;
  if(bad_padding&&(c.batches!=3||gate->buffered!=12))return 37;
  if(bad_shape==1&&(c.batches||gate->buffered!=1))return 38;
  if(second_header_bad&&(c.batches!=2||gate->buffered))return 39;
  if(pre_failure&&(c.batches||gate->started))return 24;
  c.operation=4;if(ctl(&c))return 13;
  c.operation=6;if(ctl(&c)||!gate->fatal||!gate->hold)return 46;
  printf("{\"status\":\"passed\",\"case\":\"%s\",\"fatal\":%lu,\"retained\":true,\"camera_sdk_loaded\":false}\n",argv[1],c.fatal);
  fflush(stdout);ExitProcess(0);
 }
 if(invoke_program((DWORD)fake_controller)!=1)return 14;
 if(bad_shape||missing_permit||stale_permit||full_first||mode_zero||first_pulse_bad){
  c.operation=3;if(!ctl(&c)||c.batches||single_calls-before!=(full_first?314:159)||gate->data||gate->started)return 25;
  c.operation=4;if(!ctl(&c))return 26;
  printf("{\"status\":\"passed\",\"case\":\"%s\",\"packets\":0,\"camera_sdk_loaded\":false}\n",argv[1]);return 0;
 }
 c.operation=3;if(!ctl(&c)||c.batches!=5||single_calls-before!=39||multiple_calls!=12)return 15;
 if(!ReadFile(server,received,375,&got,0)||got!=375)return 16;
 for(i=0;i<5;i++)if(received[i*75]!=1||received[i*75+1]!=0x4a)return 17;
 for(i=0;i<120;i++){
  DWORD bit=i<48?(((changed_bits?0xa531:0x88)>>(15-i/3))&1):0;DWORD offset=(i/24)*75+3*(i%24)+3;
  if(received[offset]!=2||received[offset+1]!=((i%3==1?14:12)|bit)||received[offset+2]!=0)return 21;
 }
 if(argc>1&&!strcmp(argv[1],"busy-disarm")){
  outside_entered=CreateEventA(0,TRUE,FALSE,0);outside_release=CreateEventA(0,TRUE,FALSE,0);block_outside=1;
  worker=CreateThread(0,0,run_outside,0,0,0);
  if(WaitForSingleObject(outside_entered,1000)!=WAIT_OBJECT_0)return 27;
  c.operation=3;if(ctl(&c)||c.result||c.row_size!=140||!c.active||c.mode!=1||c.programs!=2||c.fatal||c.uncertain)return 28;
  SetEvent(outside_release);if(WaitForSingleObject(worker,2000)!=WAIT_OBJECT_0)return 29;
  c.operation=3;if(!ctl(&c)||c.active)return 30;
 }
 c.operation=4;if(!ctl(&c))return 18;
 if(*(DWORD*)(fake_controller+0x618)!=(DWORD)fake_cm+0xc65fa||*(DWORD*)(fake_cm+0xf41e0)!=(DWORD)fake_pb+0x1199d||*(DWORD*)(fake_cm+0xf41e8)!=(DWORD)fake_pb+0x11778)return 19;
 printf("{\"status\":\"passed\",\"single_calls\":%lu,\"header_calls\":%lu,\"received_bytes\":%lu,\"packets\":5,\"pointers_restored\":true,\"camera_sdk_loaded\":false}\n",single_calls,multiple_calls,got);
 CloseHandle(client);CloseHandle(server);return 0;
}
