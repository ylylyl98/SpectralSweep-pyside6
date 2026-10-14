/* One writer lock protects the log handle, including idle-boundary rotation.
 * No camera/USB operations occur here. A sealed receipt is published only after
 * flushing and closing that segment; active segments are never renamed. */
#ifndef WS_PERSISTENT_LOG_H
#define WS_PERSISTENT_LOG_H
typedef struct {
 CRITICAL_SECTION lock;
 HANDLE handle;
 DWORD segment,first,bytes,limit,fault;
 BOOL initialized;
 char root[MAX_PATH],session[40];
} ws_persistent_log;
static ws_persistent_log persistent_log;
static BOOL pl_fail(DWORD error){SetLastError(error?error:ERROR_WRITE_FAULT);return FALSE;}
static DWORD pl_error(void){DWORD e=GetLastError();return e?e:ERROR_WRITE_FAULT;}
static void pl_name(DWORD segment,char*name){if(segment)sprintf(name,"session-%06lu.jsonl",segment);else strcpy(name,"session.jsonl");}
static BOOL pl_receipt(const char*name,const char*text,BOOL replace){
 char temporary[MAX_PATH],target[MAX_PATH];HANDLE file;DWORD written,error=0,n=(DWORD)strlen(text);
 sprintf(target,"%s\\%s",persistent_log.root,name);sprintf(temporary,"%s.partial",target);
 file=CreateFileA(temporary,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
 if(file==INVALID_HANDLE_VALUE)return FALSE;
 if(!WriteFile(file,text,n,&written,NULL)||written!=n||!FlushFileBuffers(file))error=pl_error();
 if(!CloseHandle(file)&&!error)error=pl_error();
 if(error)return pl_fail(error);
 return MoveFileExA(temporary,target,MOVEFILE_WRITE_THROUGH|(replace?MOVEFILE_REPLACE_EXISTING:0));
}
static BOOL pl_state(void){char text[400];
 sprintf(text,"{\"protocol\":1,\"session_id\":\"%s\",\"segment\":%lu,\"first_sequence\":%lu,\"rotation_bytes\":%lu}\n",
  persistent_log.session,persistent_log.segment,persistent_log.first,persistent_log.limit);
 return pl_receipt("log-state.json",text,TRUE);
}
static BOOL pl_seal(DWORD segment,DWORD first,DWORD last,DWORD bytes,DWORD error){char name[64],text[600];
 sprintf(name,"session-%06lu.sealed.json",segment);
 sprintf(text,"{\"protocol\":1,\"session_id\":\"%s\",\"segment\":%lu,\"first_sequence\":%lu,\"last_sequence\":%lu,\"bytes\":%lu,\"error\":%lu,\"closed\":true}\n",
  persistent_log.session,segment,first,last,bytes,error);
 return pl_receipt(name,text,FALSE);
}
static BOOL pl_open(const char*root,const char*session,DWORD limit){char path[MAX_PATH];
 if(limit<4096||limit>536870912||strlen(root)>MAX_PATH-100||strlen(session)!=36)return pl_fail(ERROR_INVALID_PARAMETER);
 memset(&persistent_log,0,sizeof(persistent_log));InitializeCriticalSection(&persistent_log.lock);persistent_log.initialized=TRUE;
 strcpy(persistent_log.root,root);strcpy(persistent_log.session,session);persistent_log.first=1;persistent_log.limit=limit;
 sprintf(path,"%s\\session.jsonl",root);
 persistent_log.handle=CreateFileA(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
 if(persistent_log.handle==INVALID_HANDLE_VALUE){persistent_log.handle=NULL;return FALSE;}
 return pl_state();
}
static BOOL pl_write(const char*bytes,DWORD count){DWORD written=0,error=0;
 EnterCriticalSection(&persistent_log.lock);
 if(!persistent_log.handle||persistent_log.fault)error=persistent_log.fault?persistent_log.fault:ERROR_INVALID_HANDLE;
 else if(count>0xffffffff-persistent_log.bytes)error=ERROR_ARITHMETIC_OVERFLOW;
 else if(!WriteFile(persistent_log.handle,bytes,count,&written,NULL)||written!=count)error=pl_error();
 else persistent_log.bytes+=written;
 if(error&&!persistent_log.fault)persistent_log.fault=error;
 LeaveCriticalSection(&persistent_log.lock);return error?pl_fail(error):TRUE;
}
/* Called only before admitting a new C request, after the previous frame has
 * STOP/drain/worker-join proof, or after initial configuration with no frame. */
static BOOL pl_rotate(DWORD completed){
 char path[MAX_PATH],name[64];HANDLE next=NULL;DWORD error=0,old_segment,old_first,old_bytes;
 EnterCriticalSection(&persistent_log.lock);
 if(persistent_log.fault){error=persistent_log.fault;goto done;}
 if(persistent_log.bytes<persistent_log.limit)goto done;
 if(persistent_log.segment>=999999){error=ERROR_ARITHMETIC_OVERFLOW;goto done;}
 pl_name(persistent_log.segment+1,name);sprintf(path,"%s\\%s",persistent_log.root,name);
 next=CreateFileA(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
 if(next==INVALID_HANDLE_VALUE){next=NULL;error=pl_error();goto done;}
 if(!FlushFileBuffers(persistent_log.handle)){error=pl_error();goto done;}
 if(!CloseHandle(persistent_log.handle)){error=pl_error();goto done;}
 old_segment=persistent_log.segment;old_first=persistent_log.first;old_bytes=persistent_log.bytes;
 persistent_log.handle=next;next=NULL;persistent_log.segment++;persistent_log.first=completed+1;persistent_log.bytes=0;
 if(!pl_seal(old_segment,old_first,completed,old_bytes,0)||!pl_state())error=pl_error();
done:
 if(next&&!CloseHandle(next)&&!error)error=pl_error();
 if(error&&!persistent_log.fault)persistent_log.fault=error;
 LeaveCriticalSection(&persistent_log.lock);return error?pl_fail(error):TRUE;
}
static BOOL pl_finish(DWORD completed,DWORD native_error){DWORD error=0;
 EnterCriticalSection(&persistent_log.lock);
 error=persistent_log.fault;
 if(persistent_log.handle){
  if(!FlushFileBuffers(persistent_log.handle)&&!error)error=pl_error();
  if(!CloseHandle(persistent_log.handle)&&!error)error=pl_error();
  persistent_log.handle=NULL;
 }
 if(!pl_seal(persistent_log.segment,persistent_log.first,completed,persistent_log.bytes,native_error?native_error:error)&&!error)error=pl_error();
 LeaveCriticalSection(&persistent_log.lock);DeleteCriticalSection(&persistent_log.lock);persistent_log.initialized=FALSE;
 return error?pl_fail(error):TRUE;
}
#endif
