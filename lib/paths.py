# -*- coding: utf-8 -*-
"""路径解析：**唯一一处**知道"配置文件在哪"的地方。

为什么要有这个模块（2026-10-03 新增）
    此前同一件事在五个文件里各写一遍，且写法都不一样：
      · `lib/engine.py`      `ROOT = pathlib.Path.home() / "netops"`
      · `lib/conn_store.py` `FILE = pathlib.Path.home() / "netops" / "connections.json"`
      · `lib/approval.py`   `ROOT = home() / "netops"`（后改，仍是写死）
      · `netdev_cli.py`      `ROOT = NETDEV_ROOT 或 __file__ 推导`
    后果实测到两类：
      1. **装到 `~/netops` 以外的地方 → 读错文件**。策略与审计流水会静默写到
         另一个目录，读不到策略文件就回落成 `ask` —— 看着"安全"，实际失控。
      2. **源码安装（git clone）根本没有根目录软链**。安装包会建
         `devices.toml`/`connections.json`/`state` 这三条软链，但 clone 不会，
         于是 `~/netops` 之外的路径上 `engine` 找不到设备清单 —— 全新克隆跑不起来。

    所以规则只有两条，都在这里实现，别处不要再自己拼路径：
      · 装在哪：NETDEV_ROOT > 从本文件位置推导（支持任意安装路径）
      · 配置文件：先看安装根（安装包建的软链），没有就看 `config/`（源码安装）
"""
from __future__ import annotations

import pathlib

# lib/paths.py → lib/ → 项目根
ROOT = pathlib.Path(
    __import__("os").environ.get("NETDEV_ROOT")
    or pathlib.Path(__file__).resolve().parent.parent
).expanduser().resolve()

CONFIG = ROOT / "config"


def _first_existing(*cands: pathlib.Path) -> pathlib.Path:
    """返回第一个存在的候选；都不存在时返回第一个（调用方据此判断"没有"）。"""
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def cfg(name: str) -> pathlib.Path:
    """配置文件定位：安装根（软链）优先，回落 `config/`（源码安装）。"""
    return _first_existing(ROOT / name, CONFIG / name)


def state_dir() -> pathlib.Path:
    """状态目录：装在 config/state 下（与安装包的软链一致），并**确保存在**。

    以前各处直接 `ROOT / "state"`，全新克隆时那个目录压根不存在，
    第一次写状态就 FileNotFoundError。这里 mkdir 掉。
    """
    d = _first_existing(ROOT / "state", CONFIG / "state")
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_dir(name: str) -> pathlib.Path:
    """运行期目录（logs / live / backups）：并确保存在。

    这三个都被 .gitignore 排除，全新克隆里没有；代码到处直接写文件就会炸。
    """
    d = ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def rel_to_home(p: pathlib.Path) -> str:
    """给人看的相对路径。**不能在 home 下时不要抛异常**。

    `Path.relative_to()` 在路径不属于 home 时会抛 ValueError —— 装到
    `/opt/netops` 的用户跑 `netdev doctor` 直接崩（实测）。
    """
    import os
    try:
        return str(pathlib.Path(p).relative_to(pathlib.Path.home()))
    except ValueError:
        return str(p)


# 这些文件在仓库里是 gitignore 的"活文件"（含本机安装路径），
# 开源仓提供的是 `config/<同名>.example` 模板。
# 安装包由 install.sh 播种；**源码安装（git clone）没有这一步**，
# 结果全新克隆后这些文件压根不存在，`netdev doctor` 恒红（实测）。
# 这里补上：缺什么就从模板生成，**已有的绝不覆盖**。
_DERIVED = {
    "devices.toml": "devices.toml.example",
    "connections.json": "connections.json.example",
    "pi-commands.json": "pi-commands.json.example",
    "_netdev": "_netdev.example",
    "netdev.bash": "netdev.bash.example",
    "AGENTS.workspace.md": "AGENTS.workspace.md.example",
}


def bootstrap(verbose: bool = False) -> list[str]:
    """首次运行时从模板补齐派生配置。返回实际生成的文件名列表。

    - **只补缺的**，不碰已存在的（你的真机清单、密码一个字节都不动）
    - 模板也不在时安静跳过（不该因此拦住程序启动）
    """
    made: list[str] = []
    CONFIG.mkdir(parents=True, exist_ok=True)
    for name, tpl in _DERIVED.items():
        dst = CONFIG / name
        if dst.exists():
            continue
        src = CONFIG / tpl
        if not src.exists():
            continue
        try:
            # 从模板抄一份，并把 __PREFIX__ 换成本机安装目录
            dst.write_text(src.read_text(encoding="utf-8")
                           .replace("__PREFIX__", str(ROOT))
                           .replace("__HOME__", str(pathlib.Path.home())),
                           encoding="utf-8")
            made.append(name)
        except Exception:
            pass
    if made and verbose:
        print("[netdev] 首次运行，已从模板生成：" + "、".join(made))
    return made
