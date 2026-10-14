#ifndef WS_PORTABLE_BINDING_H
#define WS_PORTABLE_BINDING_H
#include <windows.h>
#include <string.h>
#define WS_INSTANCE_ENV "SPECTRALSWEEP_ST133_INSTANCE"
/* One immutable exact instance per process. No VID/PID fallback or enumeration
   is allowed when enrollment is missing/malformed. No handles or device I/O. */
static volatile LONG ws_binding_state;
static char ws_binding_value[256];
static const char *ws_bound_instance(void){
 static const char prefix[]="USB\\VID_0BD7&PID_A010\\";
 DWORD n,i;BOOL valid=TRUE;unsigned char c;
 if(InterlockedCompareExchange(&ws_binding_state,1,0)==0){
  n=GetEnvironmentVariableA(WS_INSTANCE_ENV,ws_binding_value,sizeof(ws_binding_value));
  if(!n||n>=sizeof(ws_binding_value))valid=FALSE;
  if(valid){
   for(i=0;i<n;i++)if(ws_binding_value[i]>='a'&&ws_binding_value[i]<='z')ws_binding_value[i]-=32;
   if(strncmp(ws_binding_value,prefix,sizeof(prefix)-1)||n<sizeof(prefix)||n-(sizeof(prefix)-1)>200)valid=FALSE;
   for(i=sizeof(prefix)-1;valid&&i<n;i++){
    c=(unsigned char)ws_binding_value[i];
    if(!((c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='&'||c=='_'||c=='.'||c=='-'))valid=FALSE;
   }
  }
  if(!valid)ws_binding_value[0]=0;
  InterlockedExchange(&ws_binding_state,2);
 }else{
  while(InterlockedCompareExchange(&ws_binding_state,2,2)!=2)Sleep(0);
 }
 return ws_binding_value;
}
static BOOL ws_bound_json(char *out,DWORD capacity){
 const char *value=ws_bound_instance();DWORD i,j=0;
 if(!value[0]||capacity<2*strlen(value)+3)return FALSE;
 out[j++]='"';
 for(i=0;value[i];i++){if(value[i]=='\\')out[j++]='\\';out[j++]=value[i];}
 out[j++]='"';out[j]=0;return TRUE;
}
#endif
