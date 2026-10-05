"""编译 C++ 源码，并运行程序、测量耗时与峰值内存。"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

# Windows: 通过进程句柄读取峰值工作集内存
_PROCESS_QUERY = 0x0400       # PROCESS_QUERY_INFORMATION
_PROCESS_VM_READ = 0x0010     # PROCESS_VM_READ
_PROCESS_QUERY_LIMITED = 0x1000


def _disable_crash_dialog() -> None:
    """关闭 Windows 崩溃弹窗。

    否则子程序崩溃时 WerFault.exe 会继承管道句柄，导致 communicate 一直等不到 EOF，
    看起来像 TLE 而不是 RE。
    """
    if not _IS_WINDOWS:
        return
    try:
        import ctypes
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        ctypes.WinDLL("kernel32", use_last_error=True).SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        )
    except Exception:                                # noqa: BLE001, S110
        pass


_disable_crash_dialog()


@dataclass
class RunResult:
    rc: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    ms: float = 0.0
    peak_mb: float | None = None
    tle: bool = False
    mle: bool = False
    crash: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return not (self.tle or self.mle or self.crash)


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 编译
# --------------------------------------------------------------------------

def cpp_compiler() -> str:
    for name in ("g++", "clang++", "cl"):
        if _which(name):
            return name
    raise BuildError("找不到 C++ 编译器，请先安装 MinGW-w64 / MSYS2 的 g++ 并加入 PATH")


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def compile_cpp(source: Path, output: Path, extra_args: list[str] | None = None) -> Path:
    """把 source 编译成可执行文件，返回可执行文件路径。"""
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not source.is_file():
        raise BuildError(f"源文件不存在: {source}")
    if source.stat().st_size == 0:
        raise BuildError(f"源文件是空的: {source}，请先写好代码")

    compiler = cpp_compiler()
    target = output
    if _IS_WINDOWS and target.suffix.lower() not in (".exe", ".bat", ".cmd", ".dll"):
        target = target.with_suffix(".exe")

    if compiler == "cl":
        args = ["cl", "/nologo", "/O2", "/EHsc", f"/Fe:{target}", str(source)]
    else:
        args = [compiler, "-O2", "-std=c++17", "-pipe",
                "-o", str(target), str(source)]
    if extra_args:
        args[1:1] = list(extra_args)

    try:
        proc = subprocess.run(args, capture_output=True, timeout=180, check=False)
    except FileNotFoundError as exc:
        raise BuildError(f"调用编译器失败: {compiler}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError("编译超时") from exc

    if proc.returncode != 0 or not target.exists():
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise BuildError(f"编译失败: {source}\n{detail}")

    if _IS_WINDOWS and target.suffix.lower() != ".exe" and Path(str(target) + ".exe").exists():
        target = Path(str(target) + ".exe")
    return target


# --------------------------------------------------------------------------
# Windows 峰值内存
# --------------------------------------------------------------------------

def _open_process_handle(pid: int):
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        access = _PROCESS_QUERY | _PROCESS_VM_READ
        if hasattr(kernel32, "OpenProcess"):
            handle = kernel32.OpenProcess(access, False, pid)
            if handle:
                return handle
        access = _PROCESS_QUERY_LIMITED
        handle = kernel32.OpenProcess(access, False, pid)
        return handle or None
    except Exception:                                # noqa: BLE001
        return None


def _peak_memory_mb(handle) -> float | None:
    if not handle or not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return counters.PeakWorkingSetSize / (1024 * 1024)
    except Exception:                                # noqa: BLE001
        return None


def _close_handle(handle) -> None:
    if handle and _IS_WINDOWS:
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        except Exception:                            # noqa: BLE001, S110
            pass


# --------------------------------------------------------------------------
# 运行
# --------------------------------------------------------------------------

def run_program(binary: Path, stdin_data: bytes = b"",
                timeout: float = 5.0, ml_mb: float | None = None) -> RunResult:
    """运行 binary，返回耗时 / 峰值内存 / 退出状态。"""
    binary = Path(binary)
    if not binary.exists():
        return RunResult(rc=-1, crash=True, message=f"可执行文件不存在: {binary}")

    popen_kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(binary.parent),
    }
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen([str(binary)], **popen_kwargs)
    except OSError as exc:
        return RunResult(rc=-1, crash=True, message=f"启动失败: {exc}")

    handle = _open_process_handle(proc.pid)
    started = time.perf_counter()
    tle = False
    try:
        out, err = proc.communicate(stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        tle = True
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:                            # noqa: BLE001
            out, err = b"", b""
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    peak = _peak_memory_mb(handle)
    _close_handle(handle)

    rc = proc.returncode if proc.returncode is not None else -1
    result = RunResult(rc=rc, stdout=out or b"", stderr=err or b"",
                       ms=elapsed_ms, peak_mb=peak, tle=tle)

    if tle:
        result.message = f"超时 > {timeout * 1000:.0f} ms"
        result.crash = False
    elif rc != 0:
        result.crash = True
        result.message = f"非零退出码 {rc}"

    if ml_mb is not None and peak is not None and peak > ml_mb:
        result.mle = True
        result.message = (f"内存 {peak:.1f} MB > 限制 {ml_mb:g} MB"
                          if not result.message else result.message)

    if rc < 0 and not tle:
        result.crash = True

    return result


def run_generator(command: list[str], cwd: Path, timeout: float = 10.0) -> bytes:
    """运行外部数据生成器，返回它写到 stdout 的数据。"""
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(cwd),
    }
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = 0x08000000
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        raise BuildError(f"启动生成器失败: {' '.join(command)}\n{exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise BuildError(f"生成器超时 ({timeout:g}s): {' '.join(command)}")
    if proc.returncode != 0:
        raise BuildError(
            f"生成器返回 {proc.returncode}: {' '.join(command)}\n"
            + err.decode("utf-8", "replace")
        )
    return out or b""
