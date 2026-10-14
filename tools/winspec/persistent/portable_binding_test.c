/* Pure process/environment checks; this executable has no USB entry points. */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "portable_binding.h"
#define CHECK(x) do { if(!(x)){fprintf(stderr,"FAIL %d: %s\n",__LINE__,#x);return 1;} } while(0)
int main(int argc,char **argv){
 const char *selected;char json[512];
 CHECK(argc==3);
 CHECK(SetEnvironmentVariableA(WS_INSTANCE_ENV,strcmp(argv[1],"missing")?argv[1]:NULL));
 selected=ws_bound_instance();
 if(!strcmp(argv[2],"reject")){CHECK(!selected[0]);puts("rejected, hardware_calls=0");return 0;}
 CHECK(!strcmp(selected,argv[2]));
 CHECK(ws_bound_json(json,sizeof(json)));
 CHECK(strstr(json,"USB\\\\VID_0BD7&PID_A010\\\\")!=NULL);
 CHECK(SetEnvironmentVariableA(WS_INSTANCE_ENV,"USB\\VID_0BD7&PID_A026\\CHANGED"));
 CHECK(!strcmp(ws_bound_instance(),argv[2]));
 puts("exact binding frozen, hardware_calls=0");return 0;
}
