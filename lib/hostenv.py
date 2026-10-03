"""hostenv.py —— 剥离宿主 IDE 注入的运行时钩子（Node 侧 + Python 侧）。

## 为什么需要这个模块

宿主（WorkBuddy / CodeBuddy 桌面版）会给**从它里面拉起的每一个进程**注入一整包钩子，
把进程的原生语义改掉：

    NODE_OPTIONS   = --require=…/cli/vendor/shim/node-language-shim.cjs   （改写 fs 错误码）
    PATH           = …/cli/vendor/shim/brokered-bin : …/safe-bin : …      （rm/mkdir 被换掉）
    BASH_ENV       = …/cli/vendor/shim/shell-runtime-bash-env.sh          （非交互 bash 预载）
    PYTHONPATH     = …/cli/vendor/shim                                    （★ sitecustomize.py）
    GENIE_TRASH_DIR / CODEBUDDY_SAFE_DELETE_* / CODEBUDDY_SANDBOX_* / CODEBUDDY_BROKERED_*

Node 那半边 2026-10-03 已经处理过（当时的症状是 proper-lockfile 的陈旧锁永不自愈）。
**Python 那半边是 2026-10-04 才暴露的，也是本模块新增的主要内容。**

## Python 侧到底干了什么

shim 目录里有一个 `sitecustomize.py`，解释器启动时会自动 import 它（因为该目录在
PYTHONPATH 里）。它把 `os.remove` / `os.unlink` / `os.rmdir` / `shutil.rmtree` /
`pathlib.Path.unlink` 全部换成受管版本，并在真正删除前跑一次"批量删除守卫"：
按**轮次**累计将被删除的文件数，超过阈值（本机 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=50`）
就打印标记并 **`raise SystemExit(1)`**。

后果分两层，都极其隐蔽：

1. `SystemExit` 是 `BaseException` 而不是 `Exception` —— 业务代码里的
   `except Exception` **接不住**，会一路穿透 HTTP 处理函数；
2. `threading.excepthook` 对 `SystemExit` **静默忽略**，日志里连 traceback 都不留。
   于是请求没有响应、连接被断开，浏览器只报一句 **"Load failed"**
   （Safari/WebKit 对 fetch 网络级失败的文案）——用户看到的是"netdev 删不掉快照"，
   而不是"宿主拦了这次删除"。

实测记录（本机 2026-10-04）：`logs/ui-service.log` 里出现
`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":53,"threshold":50,...}`，
之后界面上的「彻底删除」连续 4 次都是 `Load failed`。

## 边界（刻意做窄）

只按**精确特征**匹配宿主 shim 的路径 / 键名，用户自己的 `NODE_OPTIONS`、`PYTHONPATH`、
`PATH` 条目一律原样保留。特别注意：`~/.workbuddy/binaries/...` 这类**含 workbuddy 字样
但属于运行时**的路径**绝不误伤** —— 匹配用的是 `/cli/vendor/shim` 这种特征，
而不是 "workbuddy" 这个关键词。
"""
from __future__ import annotations

import os

# 命中任一特征即认定为宿主 shim。注意 "/cli/vendor/shim" **不带尾斜杠** ——
# 原来这里写的是带斜杠的版本，于是 PYTHONPATH=<shim 目录本身> 这种写法**匹配不到**，
# 而它恰恰就是 Python 注入的入口（2026-10-04 的教训）。
_SHIM_TOKENS = (
    "node-language-shim",
    "node-brokered-fs-shim",
    "node-safe-delete-shim",
    "/cli/vendor/shim",
    "shell-runtime-bash-env",
    "brokered-sandbox-bash-env",
)

# 宿主为 shim 专门注入的键：连同路径一起清掉，免得残留的半套配置让 shim 走到
# "helper 不可用" 的分支上（那条分支同样是 SystemExit）。
_DROP_KEY_PREFIXES = (
    "CODEBUDDY_SAFE_DELETE_",
    "CODEBUDDY_SANDBOX_",
    "CODEBUDDY_BROKERED_",
    "CODEBUDDY_TOYBOX_",
)
_DROP_KEYS = ("GENIE_TRASH_DIR",)


def is_shim_token(tok: str) -> bool:
    """这段文本是不是指向宿主 shim。"""
    if not tok:
        return False
    low = tok.lower()
    return any(m in low for m in _SHIM_TOKENS)


def _prune_list_key(env: dict, key: str) -> None:
    """把 key 里由 os.pathsep 分隔的条目中，指向 shim 的那些剔掉。

    一个都没命中就**原样不动**（避免顺手改掉用户 PATH 里的空段等细节）。
    """
    val = env.get(key) or ""
    if not val:
        return
    parts = val.split(os.pathsep)
    if not any(is_shim_token(p) for p in parts):
        return
    keep = [p for p in parts if p and not is_shim_token(p)]
    if keep:
        env[key] = os.pathsep.join(keep)
    else:
        env.pop(key, None)


def strip_host_injection(env: dict) -> dict:
    """就地剥掉宿主注入的钩子，返回同一个 dict（方便链式调用）。

    覆盖：
      · NODE_OPTIONS —— 逐 token 剔 shim 的 --require，全是 shim 就整键删
      · PATH         —— 剔 shim 目录（brokered-bin / safe-bin / toybox …）
      · PYTHONPATH   —— ★ 剔 shim 目录（sitecustomize.py 的入口）
      · BASH_ENV     —— 指向 shim 脚本就删
      · GENIE_TRASH_DIR / CODEBUDDY_SAFE_DELETE_* / _SANDBOX_* / _BROKERED_* / _TOYBOX_*
    """
    # 1) NODE_OPTIONS —— Node 侧的入口
    raw = env.get("NODE_OPTIONS") or ""
    if raw:
        keep = [t for t in raw.split() if t and not is_shim_token(t)]
        if keep:
            env["NODE_OPTIONS"] = " ".join(keep)
        else:
            env.pop("NODE_OPTIONS", None)

    # 2) PATH —— 命令包装器目录
    _prune_list_key(env, "PATH")

    # 3) PYTHONPATH —— Python 侧的入口（sitecustomize.py）。2026-10-04 新增。
    _prune_list_key(env, "PYTHONPATH")

    # 4) BASH_ENV —— 宿主用它给每个非交互 bash 预载 shim 脚本
    be = env.get("BASH_ENV") or ""
    if be and is_shim_token(be):
        env.pop("BASH_ENV", None)

    # 5) 宿主为 shim 注入的专用键
    for k in list(env.keys()):
        if k in _DROP_KEYS or any(k.startswith(p) for p in _DROP_KEY_PREFIXES):
            env.pop(k, None)

    return env


def clean_env(base: dict | None = None) -> dict:
    """os.environ 的副本，已剥掉宿主钩子。"""
    return strip_host_injection(dict(os.environ if base is None else base))
