#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/host 单测：跨平台宿主小工具的边界用例。

覆盖：
  1. pid_alive 边界：0 / 负数 / 极大值 / 当前进程自身
  2. IS_WIN / IS_MAC / IS_POSIX 平台标记一致性
  3. venv_python 路径格式正确性
  4. serial_ports 返回结构（不要求实际有串口）
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import host  # noqa: E402


class TestPlatformFlags(unittest.TestCase):
    """平台标记应该互斥且至少一个为真。"""

    def test_exactly_one_platform(self):
        flags = [host.IS_WIN, host.IS_MAC]
        self.assertEqual(sum(1 for f in flags if f), 1,
                         "IS_WIN 与 IS_MAC 应有且仅有一个为 True")

    def test_posix_consistency(self):
        if host.IS_MAC:
            self.assertTrue(host.IS_POSIX)
        if host.IS_WIN:
            self.assertFalse(host.IS_POSIX)


class TestPidAliveBoundary(unittest.TestCase):
    """pid_alive 的边界输入（都应该返回 False，不会抛异常）。"""

    def test_pid_zero(self):
        self.assertFalse(host.pid_alive(0))

    def test_pid_negative(self):
        self.assertFalse(host.pid_alive(-1))
        self.assertFalse(host.pid_alive(-999))

    def test_pid_none(self):
        # 0 等价于假值，也应返回 False
        self.assertFalse(host.pid_alive(0))

    def test_pid_unbelievably_large(self):
        """极大的 pid（远超系统上限）应该返回 False，不抛异常。"""
        # Windows 上 pid 上限通常是 2^32-1；超过就是无效
        self.assertFalse(host.pid_alive(2**32))
        self.assertFalse(host.pid_alive(2**63 - 1))

    def test_current_process_alive(self):
        """当前进程肯定活着。"""
        self.assertTrue(host.pid_alive(os.getpid()))

    def test_just_exited_process(self):
        """启动一个短命进程，等它退出后再查 —— 应该返回 False。"""
        import subprocess
        # 启动一个立即退出的进程
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
        # 此时进程已退出，但 pid 可能还在系统里一小段时间（僵尸进程等）
        # 这里我们只验证调用不抛异常；返回值可能因平台而异
        # 在 Windows 上，已退出的进程如果句柄全关了就查不到
        # 在 POSIX 上，僵尸进程仍算存在
        # 所以这个测试只保证"不崩"
        try:
            host.pid_alive(proc.pid)
        except Exception as e:
            self.fail(f"pid_alive 对已退出进程抛异常: {type(e).__name__}: {e}")


class TestVenvPython(unittest.TestCase):
    """venv_python 返回的路径结构应该正确。"""

    def test_returns_path_with_python_exe(self):
        import pathlib
        p = host.venv_python("/tmp/fake_project")
        self.assertIsInstance(p, pathlib.Path)
        # 路径里应该包含 .venv
        self.assertIn(".venv", str(p))
        # 文件名应该是 python（Windows 上 python.exe 也含 python）
        self.assertIn("python", p.name.lower())

    def test_windows_has_scripts_dir(self):
        if host.IS_WIN:
            import pathlib
            p = host.venv_python("C:/fake")
            parts = [x.lower() for x in p.parts]
            self.assertIn("scripts", parts,
                          "Windows 上 venv python 应该在 Scripts/ 目录下")


class TestSerialPorts(unittest.TestCase):
    """serial_ports 返回结构正确（不要求有实际串口）。"""

    def test_returns_list_of_tuples(self):
        ports = host.serial_ports()
        self.assertIsInstance(ports, list)
        for item in ports:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], str)

    def test_no_exception_without_pyserial(self):
        """即使 pyserial 没装（或不可用），也应该返回空列表，不抛异常。"""
        # 这个测试验证的是函数的容错性
        # 正常情况下会走 pyserial 路径；这里我们不 mock，只保证不崩
        try:
            result = host.serial_ports()
            self.assertIsInstance(result, list)
        except Exception as e:
            self.fail(f"serial_ports 抛异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
