# -*- coding: utf-8 -*-
"""跨平台宿主小工具（2026-10 Windows 移植引入，计划回哺主仓）。

只放**每个平台都有明确语义对应**的最小集合，别处不要再各写一遍 sys.platform：
  · IS_WIN / IS_MAC          —— 平台判定
  · venv_python(root)        —— venv 解释器路径（Win: Scripts\\python.exe；POSIX: bin/python）
  · pid_alive(pid)           —— 进程探活（替代 POSIX-only 的 os.kill(pid, 0)）
  · serial_ports()           —— 串口设备清单（替代 /dev/cu.* 写死枚举）
"""
from __future__ import annotations

import os
import pathlib
import sys

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_POSIX = os.name == "posix"


def venv_python(root: str | pathlib.Path) -> pathlib.Path:
    """项目自带 venv 的解释器路径。"""
    root = pathlib.Path(root)
    return root / (".venv/Scripts/python.exe" if IS_WIN else ".venv/bin/python")


def pid_alive(pid: int) -> bool:
    """进程是否还活着。pid<=0 一律 False。

    POSIX: os.kill(pid, 0)；Windows: OpenProcess 探活 + 快照枚举兜底。
    僵尸进程在 POSIX 下仍算"存在"——与原语义一致（锁文件只关心 pid 被谁占着）。
    """
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.GetLastError.restype = wintypes.DWORD

        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if h:
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                # GetExitCodeProcess 失败 → 保守认为还活着
                return True
            finally:
                kernel32.CloseHandle(h)
        # OpenProcess 失败：区分"权限不足（进程存在）"和"pid 不存在"
        err = kernel32.GetLastError()
        if err == ERROR_ACCESS_DENIED:
            # 没权限打开 → 进程肯定存在（否则会是 ERROR_INVALID_PARAMETER）
            return True
        if err == ERROR_INVALID_PARAMETER:
            return False
        # 其他错误码：用快照枚举兜底再确认一次
        return _win_pid_in_snapshot(int(pid))
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _win_pid_in_snapshot(pid: int) -> bool:
    """用 CreateToolhelp32Snapshot 枚举进程，确认 pid 是否在系统进程列表里。

    作为 OpenProcess 失败时的兜底：某些受限环境下 OpenProcess 即使 pid 存在
    也会返回意外的错误码，快照枚举权限要求更低、更可靠。
    """
    try:
        import ctypes
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = -1
        ERROR_NO_MORE_FILES = 18

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.CHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return False
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snap, ctypes.byref(pe)):
                return False
            while True:
                if pe.th32ProcessID == pid:
                    return True
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    err = kernel32.GetLastError()
                    if err == ERROR_NO_MORE_FILES:
                        break
                    return False
            return False
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return False


def serial_ports() -> list[tuple[str, str]]:
    """枚举串口，返回 [(设备名, 描述)]。跨平台统一走 pyserial。"""
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or "") for p in list_ports.comports()]
    except Exception:
        return []
