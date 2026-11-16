"""One request-owned process job. Closing it kills only this worker's tree."""
import os,signal,subprocess,ctypes
from ctypes import wintypes

class OwnedJob:
    def __init__(self,process):
        self.process=process;self.handle=None
        if os.name=='nt':
            class Basic(ctypes.Structure):
                _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',wintypes.DWORD),
                    ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active_limit',wintypes.DWORD),
                    ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
            class IO(ctypes.Structure):_fields_=[(n,ctypes.c_uint64) for n in ('read','write','other','readbytes','writebytes','otherbytes')]
            class Extended(ctypes.Structure):
                _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                    ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];k.CreateJobObjectW.restype=wintypes.HANDLE
            k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
            k.CloseHandle.argtypes=[wintypes.HANDLE]
            self.k=k;self.handle=k.CreateJobObjectW(None,None)
            info=Extended();info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.handle or not k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not k.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):
                if self.handle:k.CloseHandle(self.handle)
                self.handle=None;process.kill();process.wait()
                raise OSError(ctypes.get_last_error(),'Cannot establish owned worker job')
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None
        elif os.name!='nt' and self.process.poll() is None:os.killpg(self.process.pid,signal.SIGKILL)
        if self.process.poll() is None:self.process.kill()
        self.process.wait()
