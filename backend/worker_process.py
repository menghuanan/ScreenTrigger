"""Start sidecar workers without attaching a Windows console to the desktop."""
import multiprocessing
import sys


if sys.platform == "win32":
    import _winapi
    import msvcrt
    import os
    import subprocess
    from multiprocessing import reduction, spawn, util
    from multiprocessing.context import SpawnProcess, set_spawning_popen
    from multiprocessing.popen_spawn_win32 import Popen as WindowsSpawn

    class HeadlessSpawn(WindowsSpawn):
        """Keep CPython's spawn bootstrap and handle transfer; change only process creation."""

        def __init__(self, process_obj):
            data = spawn.get_preparation_data(process_obj.name)
            read_handle, write_handle = _winapi.CreatePipe(None, 0)
            self.finalizer = util.Finalize(self, _winapi.CloseHandle, (read_handle,))
            with os.fdopen(msvcrt.open_osfhandle(write_handle, 0), "wb") as stream:
                args = spawn.get_command_line(parent_pid=os.getpid(), pipe_handle=read_handle)
                executable = spawn.get_executable()
                env = None
                if os.path.normcase(executable) == os.path.normcase(sys.executable) and sys.executable != sys._base_executable:
                    executable = sys._base_executable
                    env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
                # stdout belongs to the sidecar protocol. Native diagnostics stay on stderr.
                self.child = subprocess.Popen(args, executable=executable, env=env,
                    creationflags=process_obj.creation_flags, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=sys.stderr or subprocess.DEVNULL)
                self.pid = self.child.pid
                self.sentinel = self._handle = int(self.child._handle)
                self.returncode = None
                set_spawning_popen(self)
                try:
                    reduction.dump(data, stream)
                    reduction.dump(process_obj, stream)
                except BaseException:
                    self.child.kill()
                    self.child.wait()
                    raise
                finally:
                    set_spawning_popen(None)

        def close(self):
            self.finalizer()
            self.child.poll()
            self.child._handle.Close()

    class WorkerProcess(SpawnProcess):
        _Popen = staticmethod(HeadlessSpawn)
        creation_flags = subprocess.DETACHED_PROCESS

    class OcrWorkerProcess(WorkerProcess):
        # Paddle's console utilities inherit this windowless console during lazy initialization.
        creation_flags = subprocess.CREATE_NO_WINDOW
else:
    WorkerProcess = multiprocessing.get_context("spawn").Process
    OcrWorkerProcess = WorkerProcess
