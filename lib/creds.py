"""凭据管理：本地文件 → 钥匙串 → 环境变量 → 原生弹窗（AI 永远看不到明文）。

2026-09-26 变更（用户明确要求）：**不再使用 macOS 钥匙串作为主存储**，
改用本地文件 ~/.netops/credentials.json（权限 600，仅本机本人可读）。
  * 读：文件 → 钥匙串（兼容迁移，旧数据还能读出来搬走）→ 环境变量 → 弹窗
  * 写：一律写文件；钥匙串不再新增
  * 删：两边都清（避免旧副本留着）
* 代价用户已知情并授权：密码落盘（本文件权限 600）。
  * 日志绝不过密码：对外打印必须过 redact()。
"""
import json
import os
import re
import subprocess
import sys
import time

from . import host

POPUP_TIMEOUT = 180

# 凭据文件位置（可用 NETDEV_CRED_FILE 覆盖，测试时方便）
CRED_FILE = os.path.expanduser(
    os.environ.get("NETDEV_CRED_FILE") or "~/.netops/credentials.json")


# ───────────────────────────────────────────────────────── 本地文件（主存储）
def _file_all() -> dict:
    try:
        with open(CRED_FILE, encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def file_item(service: str):
    """读本地凭据文件 → (username, password)；没有返回 (None, None)。"""
    if not service:
        return None, None
    it = _file_all().get(service)
    if not isinstance(it, dict):
        return None, None
    return it.get("username"), (it.get("password") or None)


def _file_save(service: str, username: str, password: str) -> bool:
    """原子写 + 强制 600 权限。"""
    d = _file_all()
    d[service] = {"username": username or "", "password": password or "",
                  "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        os.makedirs(os.path.dirname(CRED_FILE), mode=0o700, exist_ok=True)
        tmp = CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CRED_FILE)
        os.chmod(CRED_FILE, 0o600)
        return True
    except Exception:
        return False


def _file_delete(service: str) -> bool:
    d = _file_all()
    if service not in d:
        return False
    d.pop(service, None)
    try:
        with open(CRED_FILE, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False, indent=2)
        os.chmod(CRED_FILE, 0o600)
        return True
    except Exception:
        return False


# ───────────────────────────────────────────────────────── 系统凭据库（仅作迁移兼容）
def _run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


# Windows 侧 keyring 约定：service="netdev"，account=<业务 service 名>。
_KR_SERVICE = "netdev"


def _kr_item(service: str):
    """Windows Credential Manager（经 keyring）取旧数据 → (account, password)。"""
    try:
        import keyring
        pw = keyring.get_password(_KR_SERVICE, service)
        return (service, pw) if pw else (None, None)
    except Exception:
        return None, None


def _mac_item(service: str):
    """macOS 钥匙串取旧数据 → (account, password)。"""
    r = _run(["/usr/bin/security", "find-generic-password", "-s", service])
    if not r or r.returncode != 0:
        return None, None
    acct = None
    m = re.search(r'"acct"<blob>="([^"]*)"', r.stdout)
    if m:
        acct = m.group(1)
    r2 = _run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"])
    pw = r2.stdout.strip("\n") if (r2 and r2.returncode == 0) else None
    return acct, (pw or None)


def keychain_item(service: str):
    """取凭据 → (account, password)。

    顺序：**本地文件（主）→ 系统凭据库（旧数据兼容）**。
    名字保留 keychain_item 是因为三个桥和 CLI 都在调它；语义已升级为“取凭据”。
    """
    if not service:
        return None, None
    u, p = file_item(service)
    if p or u:
        return u, p
    # —— 以下为旧数据兼容：文件里没有才去问系统凭据库 ——
    if host.IS_WIN:
        return _kr_item(service)
    return _mac_item(service)


def store_credential(service: str, username: str, password: str) -> bool:
    """写入凭据 —— 只写本地文件（不再新增系统凭据库条目）。"""
    default_user = os.environ.get("USERNAME") if host.IS_WIN else os.environ.get("USER", "mac")
    return _file_save(service, username or default_user, password)


def delete_credential(service: str) -> bool:
    """删除凭据 —— 文件与系统凭据库都清，不留旧副本。"""
    ok = _file_delete(service)
    if host.IS_WIN:
        try:
            import keyring
            keyring.delete_password(_KR_SERVICE, service)
            return True
        except Exception:
            return bool(ok)
    r = _run(["/usr/bin/security", "delete-generic-password", "-s", service])
    return bool(ok or (r and r.returncode == 0))


def list_credentials(show_password: bool = False) -> list[dict]:
    """列出本地文件里的凭据。

    show_password=False（默认）：密码位打码（给日志/概览用）。
    show_password=True：**返回明文** —— 仅在用户明确要求“可查看”的网页接口上用。
    （AI 侧看不到：MCP 工具清单里没有凭据类接口）
    """
    out = []
    for svc, it in sorted(_file_all().items()):
        if isinstance(it, dict):
            pw = it.get("password") or ""
            out.append({"service": svc, "username": it.get("username", ""),
                        "saved_at": it.get("saved_at", ""),
                        "password": pw if show_password else ("***" if pw else "")})
    return out


def _popup_win(title: str, msg: str, hidden: bool, default: str = "",
               timeout: int = POPUP_TIMEOUT):
    """Windows 原生输入弹窗（tkinter，随 Python 自带）。

    语义对齐 macOS 版：超时/关窗/取消 → None；确定且空 → None。
    弹窗置顶，密码用掩码。
    """
    import tkinter as tk
    result: dict = {"value": None, "done": False}

    def _run_dialog():
        root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        try:
            root.option_add("*Font", "Microsoft YaHei UI 10")
        except Exception:
            pass

        def _finish(val):
            result["value"] = val
            result["done"] = True
            try:
                root.destroy()
            except Exception:
                pass

        def _on_timeout():
            if not result["done"]:
                _finish(None)

        tk.Label(root, text=msg, justify="left", padx=12, pady=10, wraplength=380).pack(anchor="w")
        var = tk.StringVar(value=default or "")
        entry = tk.Entry(root, textvariable=var, width=44, show="*" if hidden else "")
        entry.pack(padx=12, fill="x")
        entry.focus_set()
        bar = tk.Frame(root)
        bar.pack(pady=10)
        tk.Button(bar, text="确定", width=9, default="active",
                  command=lambda: _finish(var.get().strip() or None)).pack(side="left", padx=6)
        tk.Button(bar, text="取消", width=9,
                  command=lambda: _finish(None)).pack(side="left", padx=6)
        root.protocol("WM_DELETE_WINDOW", lambda: _finish(None))
        root.bind("<Return>", lambda e: _finish(var.get().strip() or None))
        root.bind("<Escape>", lambda e: _finish(None))
        root.after(int(timeout * 1000), _on_timeout)
        root.update_idletasks()
        root.mainloop()

    try:
        _run_dialog()
    except Exception:
        return None
    return result["value"] if result["done"] else None


def _popup(title: str, msg: str, hidden: bool, default: str = ""):
    if host.IS_WIN:
        return _popup_win(title, msg, hidden, default)
    script = (f'display dialog "{msg}" default answer "{default}" '
              + ("with hidden answer " if hidden else "")
              + f'with title "{title}" buttons {{"取消","确定"}} default button "确定" '
                f'giving up after {POPUP_TIMEOUT}')
    r = _run(["/usr/bin/osascript", "-e", script], timeout=POPUP_TIMEOUT + 20)
    if not r or r.returncode != 0:
        return None
    m = re.search(r"text returned:(.*)$", r.stdout.strip())
    return (m.group(1) if m else "").strip() or None


def ask_username(device_name: str, prompt_hint: str = "") -> str | None:
    return _popup("netdev 登录",
                  f"{device_name} 要求输入登录用户名" + (f"（{prompt_hint}）" if prompt_hint else ""),
                  hidden=False)


def ask_password(device_name: str, username: str = "") -> str | None:
    return _popup("netdev 登录",
                  f"请输入 {device_name} 的密码" + (f"（用户 {username}）" if username else ""),
                  hidden=True)


# ───────────────────────────────────────────────────────── 对外
def service_of(dev: dict) -> str:
    return dev.get("password_keychain") or f"netdev-{dev['name']}"


def get_username(dev: dict, allow_popup: bool = False, prompt_hint: str = ""):
    """返回 (username, source)：清单 → 凭据文件/钥匙串 → 弹窗。"""
    if dev.get("username"):
        return dev["username"], "config"
    acct, _ = keychain_item(service_of(dev))
    if acct:
        return acct, "credfile"
    if allow_popup:
        u = ask_username(dev["name"], prompt_hint)
        if u:
            return u, "popup"
    return None, "none"


def get_password(dev: dict, allow_popup: bool = True):
    """返回 (password, source)：凭据文件/钥匙串 → 环境变量 → 弹窗。"""
    _, pw = keychain_item(service_of(dev))
    if pw:
        return pw, "credfile"
    env_key = dev.get("password_env") or (
        "NETDEV_" + re.sub(r"\W+", "_", dev["name"]).upper() + "_PASSWORD")
    if os.environ.get(env_key):
        return os.environ[env_key], "env"
    if device_has_no_credential(dev):
        return "", "none"
    if allow_popup:
        p = ask_password(dev["name"])
        if p:
            return p, "popup"
    return None, "none"


def device_has_no_credential(dev: dict) -> bool:
    return bool(dev.get("allow_no_credential"))


def redact(text: str) -> str:
    """日志脱敏。"""
    if not text:
        return text
    text = re.sub(r"(?i)\b(password|passwd|token|secret|api_key|access_key|auth_token)\b\s*[=:]\s*\S+",
                  r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)(Authorization:\s*(Bearer|Basic|Token))\s+\S+", r"\1 [REDACTED]", text)
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                  "[PRIVATE KEY REDACTED]", text, flags=re.S)
    return text
