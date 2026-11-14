"""Start a Windows bridge suspended and assign it to a kill-on-close job."""

from __future__ import annotations

import msvcrt
import os
import subprocess
from contextlib import suppress

import pywintypes
import win32con
import win32event
import win32job
import win32process


class WindowsJobProcess:
    """The process operations the bridge supervisor needs."""

    def __init__(self, job, process, args: list[str], pid: int) -> None:
        self._job = job
        self._process = process
        self.args = args
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        try:
            return self.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            wait_ms = win32event.INFINITE if timeout is None else max(0, int(timeout * 1000))
            if win32event.WaitForSingleObject(self._process, wait_ms) == win32event.WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.returncode = win32process.GetExitCodeProcess(self._process)
            self.terminate()
            self._process.Close()
        return self.returncode

    def terminate(self) -> None:
        self._job.Close()

    kill = terminate


def start_job_process(args: list[str], *, cwd: str, **_popen_options) -> WindowsJobProcess:
    job = win32job.CreateJobObject(None, "")
    process = thread = None
    assigned = False
    try:
        limits_class = win32job.JobObjectExtendedLimitInformation
        limits = win32job.QueryInformationJobObject(job, limits_class)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, limits_class, limits)

        # Redirect all standard streams to NUL. The unnamed job is not inheritable.
        with open(os.devnull, "r+b", buffering=0) as null:
            handle = msvcrt.get_osfhandle(null.fileno())
            os.set_handle_inheritable(handle, True)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            startup.hStdInput = startup.hStdOutput = startup.hStdError = handle
            try:
                process, thread, pid, _ = win32process.CreateProcess(
                    args[0], subprocess.list2cmdline(args), None, None, True,
                    win32con.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW, None, cwd, startup,
                )
            finally:
                os.set_handle_inheritable(handle, False)
        win32job.AssignProcessToJobObject(job, process)
        assigned = True
        win32process.ResumeThread(thread)
        return WindowsJobProcess(job, process, args, pid)
    except BaseException:
        if process is not None and not assigned:
            # The suspended child cannot run when job assignment fails.
            with suppress(pywintypes.error):
                win32process.TerminateProcess(process, 1)
        job.Close()
        if process is not None:
            process.Close()
        raise
    finally:
        if thread is not None:
            thread.Close()
