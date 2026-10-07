/* Actual helper + named pipe, controlled only by the external XP debugger.
 * No SDK, COM, camera or physical instrument handle exists in this process. */
#define DEBUG_FIXTURE 1
#define main replay_setup_main
#include "replay_test.c"
#undef main

static void WINAPI fixture_prefix(void){
 emit(0x7395,0x40,0x55);emit(0x7395,0x40,0xd5);
}
static void WINAPI fixture_body(void){
 DWORD i,j,v;
 DWORD commands[24]={0x40,0x40,0,0x30,0x30,0x30,0x22,0x24,0x22,0x24,0x22,0x24,0x42,0x44,0x40,0x40,0x50,0x34,0x32,0x34,0x38,0x36,0x3a,0x3c};
 DWORD values[24]={0x55,0xd5,0,2,0,1,0,1,126,1,0,1,1,1,214,213,128,128,250,1,1,0,2,0};
 DWORD tail[5]={0x30,0x30,0x30,0,0xfe};
 BYTE block[520];F4 bulk=(F4)(fake_cm+0x6e88-22);
 ZeroMemory(block,sizeof(block));
 for(i=2;i<24;i++){
  emit(0x7395,commands[i],values[i]);
  if(i==7)bulk((DWORD)fake_port,0x20,(DWORD)block,520);
  if(i==9)bulk((DWORD)fake_port,0x20,(DWORD)block,512);
 }
 emit(0x391a3,0x4a,0);emit(0x391b9,0x4a,8);header(5);
 for(i=0;i<16;i++)for(j=0;j<3;j++){
  v=(0x88>>(15-i))&1;emit(expected_site(i*3+j),0x4a,(j==1?14:12)|v);
 }
 emit(0x39242,0x4a,8);emit(0x39257,0x4a,0);
 emit(0x391a3,0x4a,0);emit(0x391b9,0x4a,8);header(4);
 for(i=0;i<72;i++)emit(expected_site(i+48),0x4a,i%3==1?14:12);
 emit(0x393e1,0x4a,8);emit(0x393f7,0x4a,0);
 for(i=0;i<5;i++)emit(i==4?0x77f4:0x7395,tail[i],i<3?i:0);
}
static int fixture_result(const char*path,const char*status,DWORD code,CONTROL*c,BYTE*bytes,DWORD length){
 FILE*f=fopen(path,"wb");DWORD i;if(!f)return 71;
 fprintf(f,"{\"status\":\"%s\",\"code\":%lu,\"pid\":%lu,\"single_calls\":%lu,\"multiple_calls\":%lu,\"programs\":%lu,\"packets\":%lu,\"fatal\":%lu,\"uncertain\":%lu,\"received_bytes\":%lu,\"packet_hex\":\"",
  status,code,GetCurrentProcessId(),single_calls,multiple_calls,c->programs,c->batches,c->fatal,c->uncertain,length);
 for(i=0;i<length;i++)fprintf(f,"%02x",bytes[i]);
 fprintf(f,"\",\"camera_sdk_loaded\":false}\n");
 fflush(f);fclose(f);return code;
}
static int run_debug_fixture(CONTROL*c,HANDLE server,HANDLE client,int argc,char**argv){
 BYTE*code=fake_cm+0xc65fa;BYTE received[375];FILE*f;DWORD start,got=0,available=0,i,first,second;
 F1 address;HMODULE dll=GetModuleHandleA("batch_probe.dll");
 if(argc!=4){puts("usage: debug_fixture.exe ready.json go-file result.json");return 70;}
 /* Exact original-frame shape used by the existing redirect/TF machinery. */
 {BYTE stub[]={0x55,0x8b,0xec,0x83,0xec,0x64,0xc7,0x45,0xec,1,0,0,0,
              0xe8,0,0,0,0,0x8b,0x55,8,0xe8,0,0,0,0,0x90,
              0xb8,1,0,0,0,0x8b,0xe5,0x5d,0xc2,4,0};
  CopyMemory(code,stub,sizeof(stub));
  *(DWORD*)(code+14)=(DWORD)fixture_prefix-(DWORD)(code+18);
  *(DWORD*)(code+22)=(DWORD)fixture_body-(DWORD)(code+26);
  if(!FlushInstructionCache((HANDLE)-1,code,sizeof(stub)))return 72;
 }
 c->operation=2;c->mode=1;c->frame=1;
 if(!ctl(c))return fixture_result(argv[3],"failed",73,c,received,0);
 f=fopen(argv[1],"wb");if(!f)return 74;
 address=(F1)GetProcAddress(dll,"_BatchFaultAddress@4");
 fprintf(f,"{\"status\":\"ready\",\"pid\":%lu,\"cm\":%lu,\"pipp\":%lu,\"controller\":%lu,\"port\":%lu,\"pipe\":%lu,\"handle\":%lu,\"dll_base\":%lu,\"gate_address\":%lu,\"gate_size\":120,\"magic\":%lu,\"version\":1,\"frame\":%lu,\"generation\":%lu,\"entry_address\":%lu,\"body_address\":%lu,\"redirect_address\":%lu,\"exit_address\":%lu,\"return_address\":%lu,\"entry_hex\":\"55\",\"body_hex\":\"8b5508\",\"redirect_hex\":\"90\",\"exit_hex\":\"5d\",\"control_address\":%lu,\"fault_address\":%lu,\"rows_address\":%lu,\"row_size\":%lu,\"records\":%lu,\"camera_sdk_loaded\":false}\n",
  GetCurrentProcessId(),(DWORD)fake_cm,(DWORD)fake_pb,(DWORD)fake_controller,(DWORD)fake_port,(DWORD)fake_pipe,(DWORD)client,(DWORD)dll,(DWORD)gate,gate->magic,gate->frame,gate->generation,
  (DWORD)code,(DWORD)(code+18),(DWORD)(code+26),(DWORD)(code+34),(DWORD)fake_cm+0xde32,(DWORD)ctl,address(0),c->rows,c->row_size,c->records);
 fflush(f);fclose(f);
 start=GetTickCount();
 while(GetFileAttributesA(argv[2])==INVALID_FILE_ATTRIBUTES){
  if(GetTickCount()-start>120000){
   c->operation=5;if(ctl(c)){c->operation=4;ctl(c);}
   return fixture_result(argv[3],"go_timeout",75,c,received,0);
  }
  Sleep(10);
 }
 /* The external debugger alone writes phase/permit/completed and skips body1. */
 first=invoke_program((DWORD)fake_controller);
 second=invoke_program((DWORD)fake_controller);
 c->operation=3;
 if(!ctl(c)||first!=1||second!=1||c->programs!=2||c->batches!=5||single_calls!=39||multiple_calls!=4)
  return fixture_result(argv[3],"failed",76,c,received,0);
 if(!PeekNamedPipe(server,0,0,0,&available,0)||available!=375||!ReadFile(server,received,375,&got,0)||got!=375)
  return fixture_result(argv[3],"failed",77,c,received,got);
 for(i=0;i<5;i++)if(received[i*75]!=1||received[i*75+1]!=0x4a||received[i*75+2])
  return fixture_result(argv[3],"failed",78,c,received,got);
 for(i=0;i<120;i++){
  DWORD bit=i<48?((0x88>>(15-i/3))&1):0,at=(i/24)*75+3*(i%24)+3;
  if(received[at]!=2||received[at+1]!=((i%3==1?14:12)|bit)||received[at+2])
   return fixture_result(argv[3],"failed",79,c,received,got);
 }
 c->operation=4;
 if(!ctl(c)||*(DWORD*)(fake_controller+0x618)!=(DWORD)code||*(DWORD*)(fake_cm+0xf41e0)!=(DWORD)fake_pb+0x1199d||*(DWORD*)(fake_cm+0xf41e8)!=(DWORD)fake_pb+0x11778)
  return fixture_result(argv[3],"failed",80,c,received,got);
 CloseHandle(client);CloseHandle(server);
 return fixture_result(argv[3],"passed",0,c,received,got);
}
int main(int argc,char**argv){return replay_setup_main(argc,argv);}
