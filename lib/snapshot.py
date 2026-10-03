"""snapshot.py —— 配置快照：解析 / 对比 / 生成恢复计划（纯逻辑，不做 IO 以外的事）。

给"接客户设备"场景用：
  1) 第一次接手 → 存一份快照（运行配置 + 已保存配置 + 设备元信息 + 校验和）
  2) 调试完想还原 → 先看差异预览，确认后**只补回缺失项**（安全、可逆、逐条可查）

设计纪律（与全局一致）：
  · 恢复默认只"预览"，必须显式 --apply 才动设备
  · 只自动补回"缺失"的行；"多出来的行"只给 undo 建议，需人工确认（--undo-extra）
  · 黑名单命令（reload/format/delete/reset saved-configuration）永不下发，只打印人工步骤
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import time

from . import paths as _paths   # 路径统一真源

SNAP_ROOT = _paths.ROOT / "backups" / "snapshots"
BLACKLIST = ("reload", "format", "delete", "reset saved-configuration", "undo saved-configuration")


ECHO_RE = re.compile(r"^\s*[<\[]\s*[^>\]]+\s*[>\]]")


def clean_cfg(text: str) -> str:
    """去掉命令回显/提示符行（从屏幕抓来的文本常带 `<Huawei>display xxx`、`[Huawei]` 这种）。"""
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if ECHO_RE.match(ln.rstrip()):
            continue
        out.append(ln.rstrip())
    return "\n".join(out)


# ── 解析 ─────────────────────────────────────────────────────────────────────
def parse_cfg(text: str):
    """把 VRP 配置文本解析成 [(view, line)]；view="" 表示系统视图（顶层命令）。

    判定规则（对 `display current-configuration` 输出很稳）：
      · 非空、非 '#'、非 return/end 的行才是内容
      · 先按 `#` 切块——VRP 用 `#` 分隔配置块，**绝不跨块认父**
      · 块内：首行非缩进、且后面跟缩进行 → 首行是视图入口（interface/vlan/aaa/…），
        后续缩进行是它的子命令；否则整块都是顶层命令
        （VRP 会把某些系统视图命令渲染成带 1 个缩进，如 ` ntp-service unicast-server …`、
         ` drop illegal-mac alarm`、` stelnet server enable`；这类不能挂到上一块的表头上）
    """
    raw = [ln.rstrip() for ln in text.splitlines()]
    blocks, cur = [], []
    for ln in raw:
        s = ln.strip()
        if not s or s in ("return", "end") or ECHO_RE.match(ln):
            continue
        if s == "#":
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append(cur)

    out = []
    for b in blocks:
        # ★ 2026-09-26 修正（Bug#2）：不能只认"块首"是视图入口。
        #   实测 VRP 对 vlan 段之间【不插 # 分隔】，例如：
        #       vlan 363
        #        description 222222
        #       vlan 595                    ← 没有 #
        #        description 909090
        #       vlan 888
        #        description TEST-BY-20260926
        #   → 整段被当成一块，块首 vlan 363 吞掉后面所有行，
        #     导致 revoke 计划里出现 "vlan 363 → vlan 888"、"vlan 363 → description …" 这种错归属，
        #     进而生成 undo 正常配置的危险计划。
        #   改为：逐行扫描，**块内遇到"非缩进行且下一行有缩进"就开新视图**。
        head = None
        for i, ln in enumerate(b):
            indented = ln.startswith((" ", "\t"))
            if not indented:
                nxt = b[i + 1] if i + 1 < len(b) else None
                if nxt is not None and nxt.startswith((" ", "\t")):
                    head = ln.strip()                        # 视图入口
                    out.append((head, head))
                else:
                    head = None                              # 顶层命令（可能是带 1 缩进的系统视图命令渲染）
                    out.append(("", ln.strip()))
            else:
                out.append((head or "", ln.strip()))
    return out


def normalize(rows):
    """去掉连续重复行（VRP 偶尔重复），保持顺序，返回 [(view, line)]。"""
    seen, out = set(), []
    for v, l in rows:
        k = (v, l)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def diff_cfg(old_text: str, new_text: str):
    """old=快照（期望），new=设备当前。返回 (missing, extra, same_count)。"""
    old = normalize(parse_cfg(old_text))
    new = normalize(parse_cfg(new_text))
    so, sn = set(old), set(new)
    missing = [x for x in old if x not in sn]      # 备份里有、现在没有 → 需要补回
    extra = [x for x in new if x not in so]        # 现在有、备份里没有 → 你调试时加的
    return missing, extra, len(so & sn)


def undo_of(line: str) -> str:
    """给一条配置行猜一个 undo 命令（仅供人工复核；不一定 100% 正确）。"""
    toks = line.split()
    if not toks:
        return ""
    if toks[0] == "undo":
        return line
    keep = []
    for t in toks:
        # 遇到"像取值"的 token 就停（数字/IP/带引号/含点或斜杠）
        if re.search(r"[0-9]|[\"'./]", t):
            break
        keep.append(t)
    if not keep:
        keep = [toks[0]]
    return "undo " + " ".join(keep)


# ── 恢复计划 ─────────────────────────────────────────────────────────────────
# ── 防误删保护（2026-09-26 加）────────────────────────────────────────────
#   教训：diff 一旦误判（把未变更配置算成"新增"），还原计划会变成
#   "undo 一大堆设备本来正常运行的配置"（实测出现过 undo wlan ac xxx 十余条）。
#   这条护栏：当计划里的 undo 数量远超"快照与现在的真实差异"时，直接拒绝，
#   强制人工确认，避免一条命令清空生产配置。
DANGER_UNDO_RATIO = 3.0     # undo 数 > 真实差异数 × 此系数 → 视为异常
DANGER_UNDO_MIN = 8         # 且 undo 数至少这么多才触发（小差异属正常）


def plan_is_dangerous(steps, missing_cnt: int, extra_cnt: int) -> tuple[bool, str]:
    """还原计划是否危险（可能误删未变更配置）。返回 (危险?, 说明)。"""
    undos = [s for s in steps if str(s.get("cmd", "")).strip().startswith("undo ")]
    n_undo, real = len(undos), max(1, missing_cnt + extra_cnt)
    if n_undo >= DANGER_UNDO_MIN and n_undo > real * DANGER_UNDO_RATIO:
        return True, (f"计划含 {n_undo} 条 undo，而快照与设备的真实差异仅 {real} 处"
                      f"（比例 {n_undo/real:.1f}×，阈值 {DANGER_UNDO_RATIO}×）——"
                      f"极可能是差异解析出了偏差，会删掉设备正常配置")
    return False, ""


def build_plan(missing, extra=None, undo_extra=False):
    """把差异变成"要在设备上执行的命令序列"（含视图切换）。

    去重规则：进入视图时已经下发了视图入口行，所以缺失列表里那条重复的视图入口要去掉。
    返回 (steps, warnings)；steps = [{'view', 'cmd', 'why'}]
    """
    steps, warnings = [], []
    views_needed = {v for v, _ in missing if v}
    todo = [(v, l) for (v, l) in missing if not (v == "" and l in views_needed)]

    order, groups = [], {}
    for v, l in todo:
        if v not in groups:
            groups[v] = []
            order.append(v)
        groups[v].append(l)

    for v in order:
        lines = groups[v]
        if v:
            steps.append({"view": "", "cmd": "system-view", "why": "进入系统视图"})
            steps.append({"view": "", "cmd": v, "why": f"进入视图：{v}"})
            for l in lines:
                if l != v:
                    steps.append({"view": v, "cmd": l, "why": "补回缺失行"})
        else:
            steps.append({"view": "", "cmd": "system-view", "why": "进入系统视图"})
            for l in lines:
                steps.append({"view": "", "cmd": l, "why": "补回缺失行"})

    # 多余的行：只给建议，除非明确 undo_extra
    for v, l in (extra or []):
        u = undo_of(l)
        if not u:
            continue
        if undo_extra:
            steps.append({"view": "", "cmd": "system-view", "why": "进入系统视图"})
            if v:
                steps.append({"view": "", "cmd": v, "why": f"进入视图：{v}"})
            steps.append({"view": v, "cmd": u, "why": f"撤销多余行：{l}"})
        else:
            warnings.append(f"多出（自己加的，需你决定）: {l}   → 建议：{u}")
    # 黑名单兜底
    for s in list(steps):
        if any(b in s["cmd"].lower() for b in BLACKLIST):
            warnings.append(f"跳过黑名单命令（请人工执行）: {s['cmd']}")
            steps.remove(s)
    return steps, warnings


# ── 快照盘上格式 ─────────────────────────────────────────────────────────────
def snap_id(device: str, tag: str = "") -> str:
    """快照 ID：设备名与标签都做净化（设备名可能是路径，绝不能含 '/'）。"""
    base = f"{short(device, 40)}_{time.strftime('%Y%m%d_%H%M%S')}"
    return f"{base}_{short(tag)}" if tag else base


def short(s: str, n: int = 18) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "-", s or "").strip("-")[:n]


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def next_idx() -> int:
    """下一个快照编号（全局自增，稳定不变，方便 `--from 3`）。"""
    mx = 0
    for s in list_snapshots():
        try:
            mx = max(mx, int(s.get("idx") or 0))
        except Exception:
            pass
    return mx + 1


def record_text(meta: dict, running: str, saved: str = "") -> str:
    """人读的「配置记录.txt」：设备信息 + 运行配置 + 已保存配置 + 恢复说明。"""
    L = []
    L.append("=" * 78)
    L.append(f"  配置快照记录   编号 #{meta.get('idx','-')}   {meta.get('device','')}")
    L.append("=" * 78)
    L.append(f"  标签      : {meta.get('tag','') or '(无)'}")
    L.append(f"  备注      : {meta.get('note','') or '(无)'}")
    L.append(f"  采集时间  : {meta.get('at','')}")
    L.append(f"  数据来源  : {meta.get('source','')}")
    if meta.get("version"):
        L.append(f"  设备型号  : {meta['version']}")
    if meta.get("device_clock"):
        L.append(f"  设备时钟  : {meta['device_clock']}")
    if meta.get("meta_error"):
        L.append(f"  采集告警  : {meta['meta_error']}")
    L.append("")
    L.append("  ── 恢复方法 ─────────────────────────────────────────────────────────")
    L.append(f"    netdev snap restore {meta.get('device','')} --from {meta.get('idx','')} --apply --yes")
    L.append("    （默认双向还原：撤销新增 + 补回缺失；执行前会自动再存一份「恢复前自动」快照）")
    L.append("    网页：终端页左栏「♻️ 恢复配置备份」→ 选本编号")
    L.append("")
    L.append("  ── 运行配置（display current-configuration）──────────────────────────")
    L.append(running.rstrip("\n"))
    if saved.strip():
        L.append("")
        L.append("  ── 已保存配置（display saved-configuration，即启动配置）──────────────")
        L.append(saved.rstrip("\n"))
    L.append("")
    L.append("=" * 78)
    return "\n".join(L) + "\n"


def write_snapshot(dirpath: pathlib.Path, running: str, saved: str = "", meta: dict | None = None):
    dirpath.mkdir(parents=True, exist_ok=True)
    meta = dict(meta or {})
    if not meta.get("idx"):
        meta["idx"] = next_idx()
    running = clean_cfg(running)
    saved = clean_cfg(saved)
    (dirpath / "running.cfg").write_text(running.rstrip("\n") + "\n", encoding="utf-8")
    if saved.strip():
        (dirpath / "saved.cfg").write_text(saved.rstrip("\n") + "\n", encoding="utf-8")
    (dirpath / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # 人读记录：方便直接打开/发给客户/归档（内容与 .cfg 一致，只是加了抬头与恢复说明）
    (dirpath / f"配置记录_#{meta.get('idx','-')}.txt").write_text(record_text(meta, running, saved), encoding="utf-8")
    sums = []
    for f in sorted(dirpath.iterdir()):
        if f.name == "SHA256SUMS" or f.is_dir():
            continue
        sums.append(f"{sha256_file(f)}  {f.name}")
    (dirpath / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


def list_snapshots(device: str = ""):
    if not SNAP_ROOT.exists():
        return []
    out = []
    for d in sorted(SNAP_ROOT.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if device and not d.name.startswith(device + "_"):
            continue
        m = {}
        try:
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        run = d / "running.cfg"
        out.append({
            "id": d.name, "dir": d, "idx": m.get("idx"), "at": m.get("at", ""), "tag": m.get("tag", ""),
            "note": m.get("note", ""), "device": m.get("device", d.name.split("_")[0]),
            "lines": (len(run.read_text(encoding="utf-8").splitlines()) if run.exists() else 0),
            "size": (run.stat().st_size if run.exists() else 0),
            "has_saved": (d / "saved.cfg").exists(),
        })
    return out


def find_snapshot(device: str, ref: str = ""):
    """支持：空/latest（最新）｜ #3 或 3（编号）｜ ID 片段。"""
    snaps = list_snapshots(device)
    if not snaps:
        return None
    r = str(ref or "").strip()
    if not r or r in ("latest", "last"):
        return snaps[0]
    num = r.lstrip("#").strip()
    if num.isdigit():
        for s in snaps:                       # 先在本设备的快照里找编号
            if str(s.get("idx")) == num:
                return s
        for s in list_snapshots():            # 找不到再全局找（打印该设备的快照时会给提示）
            if str(s.get("idx")) == num:
                return s
        return None
    for s in snaps:
        if r == s["id"] or r in s["id"]:
            return s
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  语义化对比与还原（v2）：回答"到底加了哪些配置"，并真的能双向还原
#    结构：globals（系统视图下的行） + views（视图入口行 → 其子配置行）
#    特判：vlan batch 只比 ID 集合；可删视图（vlan X / acl number N / …）整体 undo
# ─────────────────────────────────────────────────────────────────────────────
def structure(text: str):
    """→ (globals: list[str], views: dict[str, list[str]])（有序）"""
    g, views = [], {}
    for v, l in parse_cfg(text):
        if v:
            views.setdefault(v, [])
            if l != v:
                views[v].append(l)
        else:
            g.append(l)
    return g, views


def vlan_ids(globals_):
    """从 `vlan batch a b c` 行里取出 VLAN ID 集合。"""
    ids = set()
    for l in globals_:
        m = re.match(r"vlan batch (.+)$", l.strip())
        if m:
            ids |= {int(x) for x in m.group(1).split() if x.isdigit()}
    return ids


def deletable_view(view: str):
    """哪些视图可以整体 undo（返回 undo 命令），哪些不能删（物理接口等）返回 None。"""
    v = view.strip()
    if re.match(r"^vlan \d+$", v):
        return f"undo {v}"
    if re.match(r"^(acl|acl ipv6) number \d+$", v):
        return f"undo {v}"
    if re.match(r"^vlan batch ", v):
        return None
    if re.match(r"^(interface|user-interface) ", v):
        return None          # 物理接口/用户界面不整体删，逐条 undo 子命令
    return None


def diff_report(old_text: str, new_text: str):
    """old=快照（期望），new=设备当前。返回结构化差异（人话+可执行计划都用它）。"""
    og, ov = structure(old_text)
    ng, nv = structure(new_text)
    rep = {
        "added_views": [], "removed_views": [], "view_line_changes": [],
        "added_globals": [], "removed_globals": [],
        "added_vlans": [], "removed_vlans": [],
    }
    ovb, nvb = vlan_ids(og), vlan_ids(ng)
    rep["added_vlans"] = sorted(nvb - ovb)
    rep["removed_vlans"] = sorted(ovb - nvb)

    for v in nv:
        if v not in ov:
            rep["added_views"].append((v, nv[v]))
    for v in ov:
        if v not in nv:
            rep["removed_views"].append((v, ov[v]))
    for v in nv:
        if v in ov:
            add = [l for l in nv[v] if l not in ov[v]]
            rem = [l for l in ov[v] if l not in nv[v]]
            if add or rem:
                rep["view_line_changes"].append((v, add, rem))
    for l in ng:
        if l not in og and not l.startswith("vlan batch"):
            rep["added_globals"].append(l)
    for l in og:
        if l not in ng and not l.startswith("vlan batch"):
            rep["removed_globals"].append(l)
    return rep


def report_lines(rep):
    """把差异渲染成"人话"（回答：你加了什么 / 少了什么）。"""
    out = []
    if rep["added_vlans"]:
        out.append(("add", f"新增 VLAN：{', '.join(str(x) for x in rep['added_vlans'])}"))
    if rep["removed_vlans"]:
        out.append(("del", f"少了 VLAN：{', '.join(str(x) for x in rep['removed_vlans'])}（快照里有）"))
    for v, lines in rep["added_views"]:
        out.append(("add", f"新增视图：{v}" + (f"（含 {len(lines)} 条子配置：{'；'.join(lines[:4])}{'…' if len(lines)>4 else ''}）" if lines else "")))
    for v, lines in rep["removed_views"]:
        out.append(("del", f"少了视图：{v}" + (f"（快照里有 {len(lines)} 条子配置）" if lines else "")))
    for v, add, rem in rep["view_line_changes"]:
        for l in add:
            out.append(("add", f"新增配置：{v} → {l}"))
        for l in rem:
            out.append(("del", f"缺失配置：{v} → {l}（快照里有）"))
    for l in rep["added_globals"]:
        out.append(("add", f"新增全局配置：{l}"))
    for l in rep["removed_globals"]:
        out.append(("del", f"缺失全局配置：{l}（快照里有）"))
    return out


def plan_restore(old_text: str, new_text: str, mode: str = "full"):
    """生成"真正还原到快照状态"的命令计划。

    mode: full（双向：撤销多出 + 补回缺失，默认）｜ add（只补回缺失）
    每个 step = {'view': 目标视图('' = 系统视图), 'cmd': 命令, 'why': 原因}
    """
    rep = diff_report(old_text, new_text)
    steps, warnings = [], []
    ovb, nvb = vlan_ids(structure(old_text)[0]), vlan_ids(structure(new_text)[0])

    if mode == "full":
        # ① 撤销"多出的 VLAN"（vlan X 视图会连带其子配置一起删掉）
        for vid in rep["added_vlans"]:
            steps.append({"view": "", "cmd": f"undo vlan {vid}", "why": f"撤销新增 VLAN {vid}"})
        # ② 撤销"多出的视图"
        handled = set()
        for v, lines in rep["added_views"]:
            if re.match(r"^vlan \d+$", v):
                vid = int(v.split()[1])
                if vid in rep["added_vlans"]:
                    handled.add(v)          # 已被 undo vlan 覆盖
                    continue
            d = deletable_view(v)
            if d:
                steps.append({"view": "", "cmd": d, "why": f"撤销新增视图 {v}"})
                handled.add(v)
        # ③ 其余"多出的行"：进它所属视图后逐条 undo
        for v, add, _rem in rep["view_line_changes"]:
            for l in add:
                steps.append({"view": v, "cmd": f"undo {l}", "why": f"撤销新增配置：{v} → {l}"})
        for v, lines in rep["added_views"]:
            if v in handled:
                continue
            for l in lines:
                steps.append({"view": v, "cmd": f"undo {l}", "why": f"撤销 {v} 下的新增配置"})
    # ④ 补回缺失的 VLAN
    for vid in rep["removed_vlans"]:
        steps.append({"view": "", "cmd": f"vlan {vid}", "why": f"补回 VLAN {vid}"})
    # ⑤ 补回缺失的视图 + 子配置
    for v, lines in rep["removed_views"]:
        steps.append({"view": "", "cmd": v, "why": f"补回视图 {v}"})
        for l in lines:
            steps.append({"view": v, "cmd": l, "why": f"补回 {v} 下的配置"})
    # ⑥ 补回缺失的视图内行
    for v, _add, rem in rep["view_line_changes"]:
        for l in rem:
            steps.append({"view": v, "cmd": l, "why": f"补回缺失配置：{v} → {l}"})
    # ⑦ 补回缺失的全局行
    for l in rep["removed_globals"]:
        steps.append({"view": "", "cmd": l, "why": "补回全局配置"})
    for l in rep["added_globals"]:
        if mode == "full":
            warnings.append(f"新增的全局配置：{l}   （要撤销请手工确认，工具不自动 undo 全局行）")

    # 黑名单兜底
    for s in list(steps):
        if any(b in s["cmd"].lower() for b in BLACKLIST):
            warnings.append(f"跳过黑名单命令（请人工执行）: {s['cmd']}")
            steps.remove(s)
    return steps, warnings, rep


TRASH_ROOT = _paths.ROOT / "backups" / "snapshots_trash"


def move_to_trash(snap, note: str = "") -> pathlib.Path:
    """删除 = 移入回收区（不裸删；可在里面再 purge）。"""
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    dst = TRASH_ROOT / f"{snap['id']}_removed-{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.move(str(snap["dir"]), str(dst))
    if note:
        try:
            (dst / "REMOVED.txt").write_text(note + "\n", encoding="utf-8")
        except Exception:
            pass
    return dst


def list_trash():
    if not TRASH_ROOT.exists():
        return []
    return sorted([d for d in TRASH_ROOT.iterdir() if d.is_dir()], reverse=True)


# ── 索引清单 + 导出（防止"快照被删了都不知道"）────────────────────────────────
INDEX_FILE = _paths.ROOT / "backups" / "snapshots.index.tsv"
WORKBUDDY = pathlib.Path.home() / "Desktop" / "workbuddy"


def index_read():
    rows = []
    if INDEX_FILE.exists():
        for ln in INDEX_FILE.read_text(encoding="utf-8").splitlines()[1:]:
            f = ln.split("\t")
            if len(f) >= 6:
                rows.append(dict(zip(("idx", "id", "at", "tag", "sha", "status"), f)))
    return rows


def index_record(meta, sha, status="ok"):
    """把快照登记/更新到索引（纯文本 TSV，可 grep、可审计）。"""
    rows = [r for r in index_read() if r.get("id") != meta.get("id", "")]
    rows.append({"idx": str(meta.get("idx", "")), "id": meta.get("id", ""), "at": meta.get("at", ""),
                 "tag": meta.get("tag", ""), "sha": sha, "status": status})
    rows.sort(key=lambda r: (r["at"], r["id"]))
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write("idx\tid\tat\ttag\tsha256\tstatus\n")
        for r in rows:
            f.write("\t".join(str(r[k]) for k in ("idx", "id", "at", "tag", "sha", "status")) + "\n")


def export_record_to_workbuddy(meta, run_cfg_path: pathlib.Path):
    """把「配置记录.txt」复制一份到 ~/Desktop/workbuddy（只增不减的成品目录），
    这样即使 netops 下的快照被误删，交付级的副本还在。返回目标路径或 None。"""
    try:
        WORKBUDDY.mkdir(parents=True, exist_ok=True)
        src = run_cfg_path.parent / f"配置记录_#{meta.get('idx','-')}.txt"
        if not src.exists():
            return None
        stamp = time.strftime("%Y%m%d", time.localtime())
        tag = short(meta.get("tag") or "快照", 16)
        dst = WORKBUDDY / f"{stamp}_{meta.get('device','dev')}_配置快照_#{meta.get('idx','-')}_{tag}.txt"
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return None


def missing_from_index():
    """索引里有、但磁盘上已经没有的快照（= 被删掉了，要明确报出来）。"""
    on_disk = {s["id"] for s in list_snapshots()}
    return [r for r in index_read() if r.get("status") == "ok" and r["id"] not in on_disk]
