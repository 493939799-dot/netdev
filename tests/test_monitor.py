#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：排障监控改造（2026-10-04 第一批）。

覆盖四组「一坏就全坏」的性质：

  1. logbuffer 分类解析 —— 接口翻动/路由邻居/环路信号/告警；
     只认带时间戳的正文行（表头/提示符混进来就是假线索）。
  2. 增量口径 —— 错包只看「相对上次新增」；计数器回绕/设备重启
     （差值为负）必须弃用本轮增量，绝不能报成「在涨」吓人。
  3. 判色阈值 —— bad/warn 的分界是排障语义（≥90 立刻看 / ≥70 留意），
     写错方向 = 界面把红灯当绿灯。
  4. 基线落盘 —— JSONL 追加 + 读取 + 轮转；基线是「手动档跨间隔
     累积」的根，丢了它手动模式就退化回瞬时快照。
  5. mock_vrp 的 display logbuffer —— 模拟器必须喂得出带时间戳日志，
     否则本组 1~4 全是空中楼阁（无真机时联调断粮）。
  6. 前端断言守 —— 监控区必须是简易表格（用户 2026-10-04 指定：
     不做成卡片），且 CPU 历史要接后端基线 hist_cpu。

用法：
    python3 tests/test_monitor.py
全程离线：不接真机、不起服务、不发网络请求。
"""
from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="netdev-mon-")
os.environ["NETDEV_ROOT"] = TMP          # 基线文件全部落进临时目录
# ★ 退出即清：不清理的话，在 TEMP/TMP 为空或相对路径的环境里，这个模块级
#   临时目录会直接堆在 CWD（同类问题见 06-接管与完善记录 OPT-12）。
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'NG '} {name}" + (f"  -- {detail}" if detail and not cond else ""))


sys.path.insert(0, str(ROOT / "ui"))
import server as S          # noqa: E402   （NETDEV_ROOT 必须在 import 前设好）
import mock_vrp as MV       # noqa: E402


# ======================================================================
# 一、logbuffer 分类解析
# ======================================================================
HW_LOG = """
2026-10-04 09:12:33  MOCK %%01IFNET/4/IF_STATE(l)[0]:Interface GE0/0/2 has turned into DOWN state.
2026-10-04 09:12:41  MOCK %%01IFNET/4/IF_STATE(l)[1]:Interface GE0/0/2 has turned into UP state.
2026-10-04 09:13:02  MOCK %%01OSPF/3/NBR_CHANGE(l)[2]:Neighbor changes: neighbor 10.0.0.2 changed from Full to Down.
2026-10-04 09:15:27  MOCK %%01AAA/6/AAA_SUCCESS(l)[3]:User admin login successfully.
  ---- 更多日志略 ----
提示符行 <MOCK-HW> 不该被当日志数进去
"""
CISCO_LOG = """
Oct  4 09:12:33.101: %LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down
Oct  4 09:12:41.102: %LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to up
"""


def test_logbuffer_parse():
    print("\n[1] logbuffer 分类解析")
    r = S._parse_logbuffer(HW_LOG)
    check("华为式时间戳正文行被识别", r["supported"] and r["total"] == 4, repr(r))
    cats = {h["cat"]: h["n"] for h in r["hits"]}
    check("接口翻动 ×2 被归类", cats.get("接口翻动") == 2, repr(cats))
    check("OSPF 邻居变化归「路由邻居」", cats.get("路由邻居") == 1, repr(cats))
    check("无关行（AAA 登录）不误报成翻动", cats.get("接口翻动") == 2, repr(cats))
    check("每条线索带 sample 样本", all(h.get("sample") for h in r["hits"]), repr(r["hits"]))

    r2 = S._parse_logbuffer(CISCO_LOG)
    check("思科式时间戳（Oct  4）同样识别", r2["supported"] and r2["total"] == 2, repr(r2))
    c2 = {h["cat"]: h["n"] for h in r2["hits"]}
    check("思科 changed state to down 归接口翻动", c2.get("接口翻动") == 2, repr(c2))

    r3 = S._parse_logbuffer("\nError: Unrecognized command found at '^' position.\n")
    check("设备不认命令 → supported=False 不硬编", r3 == {"supported": False, "total": 0, "hits": []}, repr(r3))
    r4 = S._parse_logbuffer("")
    check("空回显安静返回", r4["supported"] is False and r4["total"] == 0, repr(r4))


# ======================================================================
# 二、增量口径 + 判色
# ======================================================================
def _metrics(**kw):
    m = {"cpu_1m": 20, "cpu_10s": 25, "mem_pct": 40, "crc": 0, "in_err": 0,
         "if_up": 3, "if_total": 4, "if_in_max": 10, "if_out_max": 5,
         "mem_used": 200 * 1048576, "mem_total": 512 * 1048576}
    m.update(kw)
    return m


NO_LOG = {"supported": False, "total": 0, "hits": []}
HIT_LOG = {"supported": True, "total": 4,
           "hits": [{"cat": "接口翻动", "n": 2, "sample": "Interface GE0/0/2 has turned into DOWN state."}]}


def test_delta_and_colors():
    print("\n[2] 增量口径 + 判色阈值")

    # 首次采集
    delta, rows = S._mon_delta_and_rows(_metrics(crc=100, in_err=0), None, NO_LOG)
    keys = [r["key"] for r in rows]
    check("固定 13 行事实清单（体验/资源/变化/日志）",
          keys == ["wan", "dns", "nat", "dhcp", "cpu", "ram", "err", "uti",
                   "int", "optical", "arp", "cfg", "log"], repr(keys))
    err = [r for r in rows if r["key"] == "err"][0]
    check("首次采集不报「在涨」", "首次采集" in err["note"] and err["status"] == "ok", repr(err))
    # 正常增量
    delta, rows = S._mon_delta_and_rows(_metrics(crc=105, in_err=1), {"crc": 100, "in_err": 0}, NO_LOG)
    err = [r for r in rows if r["key"] == "err"][0]
    check("crc +5 → ↑ 在涨 +5（bad）", err["status"] == "bad" and "在涨 +5" in err["note"], repr(err))
    # 零增量
    _, rows = S._mon_delta_and_rows(_metrics(crc=105, in_err=1), {"crc": 105, "in_err": 1}, NO_LOG)
    err = [r for r in rows if r["key"] == "err"][0]
    check("无新增 → ok「无新增」", err["status"] == "ok" and err["note"] == "无新增", repr(err))
    # 计数器回绕 / 设备重启：差值为负必须弃用
    _, rows = S._mon_delta_and_rows(_metrics(crc=5, in_err=0), {"crc": 999900, "in_err": 50}, NO_LOG)
    err = [r for r in rows if r["key"] == "err"][0]
    check("回绕（负差值）→ 弃用增量不报在涨", err["status"] == "ok" and "在涨" not in err["note"], repr(err))
    delta, _ = S._mon_delta_and_rows(_metrics(crc=5), {"crc": 999900}, NO_LOG)
    check("回绕增量在 delta 里标 None（弃用）", delta["crc"] is None, repr(delta))
    # CPU 判色
    _, rows = S._mon_delta_and_rows(_metrics(cpu_1m=95), None, NO_LOG)
    check("CPU 95% → bad", [r for r in rows if r["key"] == "cpu"][0]["status"] == "bad")
    _, rows = S._mon_delta_and_rows(_metrics(cpu_1m=75), None, NO_LOG)
    check("CPU 75% → warn", [r for r in rows if r["key"] == "cpu"][0]["status"] == "warn")
    _, rows = S._mon_delta_and_rows(_metrics(cpu_1m=30), None, NO_LOG)
    check("CPU 30% → ok", [r for r in rows if r["key"] == "cpu"][0]["status"] == "ok")
    # 内存判色
    _, rows = S._mon_delta_and_rows(_metrics(mem_pct=96), None, NO_LOG)
    check("内存 96% → bad", [r for r in rows if r["key"] == "ram"][0]["status"] == "bad")
    _, rows = S._mon_delta_and_rows(_metrics(mem_pct=91), None, NO_LOG)
    check("内存 91% → warn", [r for r in rows if r["key"] == "ram"][0]["status"] == "warn")
    # 占用率判色
    _, rows = S._mon_delta_and_rows(_metrics(if_in_max=92), None, NO_LOG)
    check("入占用 92% → bad", [r for r in rows if r["key"] == "uti"][0]["status"] == "bad")
    # 接口 up 对比
    _, rows = S._mon_delta_and_rows(_metrics(if_up=2, if_total=4), {"if_up": 3}, NO_LOG)
    introw = [r for r in rows if r["key"] == "int"][0]
    check("接口比上次少 1 → warn 提示", introw["status"] == "warn" and "少 1" in introw["note"], repr(introw))
    # 日志行
    _, rows = S._mon_delta_and_rows(_metrics(), None, HIT_LOG)
    logrow = [r for r in rows if r["key"] == "log"][0]
    check("日志有线索 → warn 行", logrow["status"] == "warn" and "接口翻动 ×2" in logrow["note"], repr(logrow))
    _, rows = S._mon_delta_and_rows(_metrics(), None, {"supported": True, "total": 9, "hits": []})
    logrow = [r for r in rows if r["key"] == "log"][0]
    check("日志无异常线索 → ok", logrow["status"] == "ok" and "无异常线索" in logrow["note"], repr(logrow))
    _, rows = S._mon_delta_and_rows(_metrics(), None, NO_LOG)
    logrow = [r for r in rows if r["key"] == "log"][0]
    check("日志不支持 → ok 且不影响其他行", logrow["status"] == "ok", repr(logrow))


# ======================================================================
# 七、三层指标观（体验/变化层解析 + 配置漂移 + 深体检闸门）
# ======================================================================
def test_three_layers():
    print("\n[7] 三层指标：探针解析 / 配置漂移 / 深体检闸门")
    # ping：华为逐包 time= 与思科 Success rate 两种口径
    r = S._parse_ping("time=38 ms\ntime=40 ms\n 5 packet(s) transmitted, 0.00% packet loss")
    check("ping 华为式：丢包 0 + 逐包平均", r == {"loss": 0.0, "avg_ms": 39.0}, repr(r))
    r = S._parse_ping("Success rate is 80 percent (4/5)\nround-trip min/avg/max = 1/2/4 ms")
    check("ping 思科式：丢包 20% + min/avg/max", r == {"loss": 20.0, "avg_ms": 2.0}, repr(r))
    check("ping 坏回显 → None", S._parse_ping("") is None
          and S._parse_ping("Error: Unrecognized command") is None)
    # ★ 全超时兜底（2026-10-04 真机教训）：统计行没出来/没有时不能装「未采集」，
    #   出口真不通是事实，得按 Request time out 估丢包
    r = S._parse_ping("Request time out\nRequest time out\nRequest time out\nRequest time out")
    check("ping 全超时（无统计行）→ 丢包 100%", r == {"loss": 100.0, "avg_ms": None}, repr(r))
    r = S._parse_ping("Request time out\ntime=38 ms")
    check("ping 半程截断：1 超时 1 成功 → 丢包 50%", r == {"loss": 50.0, "avg_ms": 38.0}, repr(r))
    # DNS
    r = S._parse_dns("Server: [114.114.114.114]\nAddress: 114.114.114.114\n\n"
                     "Name: www.baidu.com\nAddress: 14.215.177.38")
    check("DNS 解析成功取回答地址", r == {"ok": True, "addr": "14.215.177.38"}, repr(r))
    r = S._parse_dns("*** Unknown host www.nonexist.com")
    check("DNS 解析失败 → ok=False（是事实不是采不到）", r == {"ok": False}, repr(r))
    # NAT
    r = S._parse_nat("Current total sessions: 128\nSession upper limit: 4096")
    check("NAT 会话：used/cap/pct", r == {"used": 128, "cap": 4096, "pct": 3}, repr(r))
    r = S._parse_nat("Total active translations: 12")
    check("NAT 只有条数时 pct=None（不装懂）", r == {"used": 12, "cap": None, "pct": None}, repr(r))
    # DHCP（多池求和）
    r = S._parse_dhcp("Used             : 35        Idle: 165\nUsed             : 12        Idle: 88")
    check("DHCP 多池求和：used 47 / idle 253", r == {"used": 47, "idle": 253}, repr(r))
    # ★ 华为真机格式（2026-10-04 AR111-S 实采）：每池把同一组数字印两遍
    #   （Pool-name 明细块 + IP address Statistic 汇总块），直接求和会双倍记账
    r = S._parse_dhcp(
        "  Pool-name        : vlan448\n"
        "  Address Statistic: Total       :253       Used        :0\n"
        "                     Idle        :253       Expired     :0\n"
        "  IP address Statistic\n"
        "    Total       :253\n"
        "    Used        :0          Idle        :253\n")
    check("DHCP 华为池头双记账：按池取末值不翻倍", r == {"used": 0, "idle": 253}, repr(r))
    # ★ 同屏通道切段（2026-10-04 真机教训：不切段 dhcp/arp 采到了也解析成 null）
    fake_screen = ("<AR111-S>display ip pool\nPool-name: p1 Used:0 Idle:253\n"
                   "<AR111-S>display arp all\nTotal:6\n"
                   "<AR111-S>display transceiver diagnosis\nError:Too many parameters\n")
    segs = S._split_screen_by_cmds(fake_screen,
                                   ["display ip pool", "display arp all",
                                    "display transceiver diagnosis"])
    check("同屏切段：各命令拿到自己的回显段",
          "Pool-name: p1" in segs.get("display ip pool", "")
          and "Total:6" in segs.get("display arp all", "")
          and "Error" in segs.get("display transceiver diagnosis", ""), repr(segs))
    segs = S._split_screen_by_cmds(fake_screen, ["display ip pool", "display lldp neighbor"])
    check("同屏切段：被顶出屏的命令跳过不误切",
          "Total:6" in segs.get("display ip pool", "")
          and "display lldp neighbor" not in segs, repr(segs))
    # ★ 跨轮次残留（2026-10-04 真机教训）：滚动缓冲里上一轮回显还在，
    #   必须锚定最新一轮 —— 否则切出跨轮巨段，混进旧 Error 被 _bad_echo 否决
    two_rounds = ("<AR>display ip pool\nError: bad\n<AR>display arp all\nTotal:99\n<AR>"
                  "<AR>display ip pool\nUsed:0 Idle:253\n<AR>display arp all\nTotal:6\n<AR>")
    segs = S._split_screen_by_cmds(two_rounds, ["display ip pool", "display arp all"])
    check("同屏切段：滚动缓冲有上一轮残留时锚定最新一轮",
          segs.get("display arp all", "").strip().startswith("Total:6")
          and "Used:0" in segs.get("display ip pool", "")
          and "Total:99" not in segs.get("display arp all", ""), repr(segs))
    # ARP
    check("ARP Total 行优先", S._parse_arp_count("Total:3") == 3)
    # 光衰
    r = S._parse_optical("Rx Power(dBm): -12.50\nRx Power(dBm): -25.10")
    check("光衰：最弱 -25.1", r == {"min": -25.1, "max": -12.5, "n": 2}, repr(r))
    # 配置漂移（基线哈希对比）
    _, rows = S._mon_delta_and_rows({"cfg_sha": "aaa1"}, {"cfg_sha": "bbb2"}, NO_LOG)
    cfg = [r for r in rows if r["key"] == "cfg"][0]
    check("配置哈希变了 → warn「已变更」", cfg["status"] == "warn" and cfg["val"] == "已变更", repr(cfg))
    _, rows = S._mon_delta_and_rows({"cfg_sha": "aaa1"}, {"cfg_sha": "aaa1"}, NO_LOG)
    cfg = [r for r in rows if r["key"] == "cfg"][0]
    check("配置一致 → ok", cfg["status"] == "ok" and cfg["val"] == "无变更", repr(cfg))
    # ARP 突增
    _, rows = S._mon_delta_and_rows({"arp": 200}, {"arp": 100}, NO_LOG)
    arp = [r for r in rows if r["key"] == "arp"][0]
    check("ARP 翻倍 → warn（环路/扫描前兆）", arp["status"] == "warn", repr(arp))
    # STP/拓扑 分类
    r = S._parse_logbuffer("2026-10-03 16:44:02 M MSTP topology change detected, TC received on port GE0/0/4.\n")
    check("STP/拓扑归独立线索类", any(h["cat"] == "STP/拓扑" for h in r["hits"]), repr(r))

    # 深体检：只读闸门
    r = S.parse_deepcheck_plan('{"cmds":["display interface","show version",'
                               '"save config","undo stp all","display cpu-usage",'
                               '"display interface","reboot"]}')
    check("闸门放行 display/show、拦截写命令、去重",
          r.get("cmds") == ["display interface", "show version", "display cpu-usage"]
          and len(r.get("rejected", [])) >= 2, repr(r))
    check("全被拦下 → error 不执行", "error" in S.parse_deepcheck_plan('{"cmds":["save","reboot"]}'))
    check("非 JSON 计划 → error", "error" in S.parse_deepcheck_plan("我觉得应该看看接口"))
    check("闸门本身：ping 不算只读体检命令（防借道）", not S._deepcheck_gate("ping 1.2.3.4 -c 999"))

    # 前端断言守（深体检入口）
    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    check("深体检按钮 + 端点接线", 'id="btnDeep"' in html and "'/api/deepcheck'" in html)
    check("计划展示（可折叠，标注闸门拦截数）", 'dplan' in html and "dg.plan" in html)


# ======================================================================
# 三、基线落盘（JSONL）
# ======================================================================
def test_baseline():
    print("\n[3] 基线落盘")
    dev = "ut-baseline-dev"
    p = S._mon_baseline_path(dev)
    check("基线路径在 NETDEV_ROOT 下（测试隔离，注意 /var→/private/var 软链）",
          str(p).startswith(str(pathlib.Path(TMP).resolve()))
          and p.name == "ut-baseline-dev.jsonl", str(p))
    check("空基线 → load 返回 None", S._mon_baseline_load(dev) is None)
    S._mon_baseline_append(dev, _metrics(crc=10, cpu_1m=22))
    S._mon_baseline_append(dev, _metrics(crc=12, cpu_1m=25))
    last = S._mon_baseline_load(dev)
    check("load 取最后一行", last and last["crc"] == 12 and last["cpu"] == 25, repr(last))
    hist = S._mon_baseline_all(dev, "cpu")
    check("hist 取 cpu 序列", hist == [22.0, 25.0], repr(hist))
    # 轮转：>2000 行 → 留尾部 1000
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps({"crc": i, "cpu": i}) for i in range(2001)) + "\n",
                 encoding="utf-8")
    S._mon_baseline_append(dev, _metrics(crc=9999, cpu_1m=1))
    n = len(p.read_text(encoding="utf-8").splitlines())
    check("超 2000 行轮转到尾 1000", 1000 <= n <= 1001, f"实际 {n} 行")


# ======================================================================
# 四、log 命令分派 + mock_vrp 喂日志
# ======================================================================
def test_log_cmd_and_mock():
    print("\n[4] log 命令分派 + mock_vrp")
    check("锐捷/思科走 show logging", S._log_cmd_for("ruijie_os") == "show logging"
          and S._log_cmd_for("cisco_ios") == "show logging")
    check("华为/华三走 display logbuffer", S._log_cmd_for("huawei_vrp") == "display logbuffer"
          and S._log_cmd_for("h3c_comware") == "display logbuffer")
    check("未知平台回落 display 系", S._log_cmd_for(None) == "display logbuffer")

    st = MV.State()
    out = MV.handle(st, "display logbuffer")
    parsed = S._parse_logbuffer(out)
    check("mock_vrp 的 logbuffer 可被解析", parsed["supported"] and parsed["total"] >= 8, repr(parsed))
    cats = {h["cat"]: h["n"] for h in parsed["hits"]}
    check("mock 日志含接口翻动样本", cats.get("接口翻动", 0) >= 4, repr(cats))
    check("mock 日志含路由邻居样本", cats.get("路由邻居", 0) >= 2, repr(cats))
    out2 = MV.handle(st, "show logging")
    check("show logging 同样有回显（锐捷联调）", "turned into" in out2 or "down" in out2.lower(), out2[:80])


# ======================================================================
# 五、前端断言守：监控区必须是简易表格
# ======================================================================
def test_frontend():
    print("\n[5] 前端断言守（2026-10-04 定稿：纯 AI 诊断视图，指标详情已删）")
    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    check("状态区主视图 #diagBox", 'id="diagBox"' in html)
    check("指标详情表格已整体移除（details/table/canvas/hint）",
          all(s not in html for s in
              ('id="monDetail"', 'id="monTable"', 'id="monRows"', 'id="bwChart"',
               'id="monHint"', 'class="mon-tbl"', 'renderMonRows', 'MON_KEYMAP',
               'MON_HIST', '_fitSpark', 'drawCpuChart', '_stOn')))
    check("旧卡片元素 ID 也清干净", "$('mCpu')" not in html and "$('mMem')" not in html
          and "$('mCrc')" not in html and "$('mIf')" not in html)
    check("每条建议用横线分割（diag-item 上边框）",
          ".diag-item{border-top:1px dashed var(--g4)" in html)
    check("徽章三档用语义色", "DIAG_SEV" in html and "var(--st-on)" in html
          and "var(--bad)" in html)
    check("采集走 /api/monitor/diagnose", "/api/monitor/diagnose" in html)
    check("AI 失败降级：黄条说明原因", "AI 诊断不可用" in html)
    check("接口详情弹窗保留（?open=if 等入口还在用 showIfDetail）", "showIfDetail" in html)
    check("📚 已学规则入口保留（btnRules）", "$('btnRules').onclick=()=>showRules();" in html)


# ======================================================================
# 六、AI 状态诊断（2026-10-04 用户指定：监控改「状态」，AI 给结构化建议）
# ======================================================================
def test_ai_diagnose():
    print("\n[6] AI 状态诊断（prompt 组装 + JSON 解析 + 前端断言守）")
    mon = {"platform": "huawei_vrp", "via": "直连（静默）",
           "rows": [{"key": "cpu", "label": "CPU", "val": "7%", "status": "ok", "note": "1min 7.0%"},
                    {"key": "err", "label": "错包", "val": "12", "status": "bad", "note": "↑ 在涨 +12"}],
           "delta": {"crc": 12},
           "log": {"supported": True, "total": 10,
                   "hits": [{"cat": "接口翻动", "n": 6,
                             "sample": "Interface GE0/0/2 has turned into DOWN state."}]},
           "hist_cpu": [5.0, 6.0, 7.0]}
    p = S.ai_diagnose_prompt("mock-hw", mon)
    check("prompt 含设备名与平台", "mock-hw" in p and "huawei_vrp" in p)
    check("prompt 喂结构化指标（值+备注）", "7%" in p and "在涨 +12" in p)
    check("prompt 喂日志线索（分类+样本）", "接口翻动×6" in p and "turned into DOWN" in p)
    check("prompt 喂 CPU 历史统计", "最高 7%" in p and "近 3 次" in p)
    check("prompt 禁止编造 + 强制只出 JSON", "不要编造" in p and "只输出一个 JSON" in p)
    check("输出要求规整：限条数 + 短语标题 + 单条动作",
          "最多 4 条" in p and "不超过 12 个字" in p and "不要给一堆命令清单" in p)

    r = S.parse_diag_json('{"overall":"bad","summary":"错包在涨","items":'
                          '[{"sev":"bad","title":"t","detail":"d","action":"a"}]}')
    check("裸 JSON 解析", r.get("overall") == "bad" and r["items"][0]["action"] == "a", repr(r))
    r = S.parse_diag_json('```json\n{"overall":"ok","summary":"正常","items":[]}\n```')
    check("``` 围栏 JSON 解析", r.get("overall") == "ok" and r.get("items") == [], repr(r))
    r = S.parse_diag_json('分析如下：\n{"overall":"warn","summary":"留意","items":[]} 以上。')
    check("带前后废话也能抠出 JSON", r.get("overall") == "warn", repr(r))
    check("无 JSON → error", "error" in S.parse_diag_json("我觉得设备挺好的"))
    check("缺 overall → error", "error" in S.parse_diag_json('{"summary":"x"}'))
    r = S.parse_diag_json('{"overall":"离谱档","summary":"x","items":[{"sev":"?","title":"t"}]}')
    check("未知档位归 warn、坏 sev 归 warn（不炸）",
          r.get("overall") == "warn" and r["items"][0]["sev"] == "warn", repr(r))

    html = (ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    check("状态区主视图 #diagBox", 'id="diagBox"' in html)
    check("指标详情已整体移除（用户 2026-10-04 指定不要了）",
          all(s not in html for s in ('id="monDetail"', 'id="monTable"', 'id="monRows"')))
    check("徽章三档用语义色", "DIAG_SEV" in html and "var(--st-on)" in html
          and "var(--bad)" in html)
    check("采集走 /api/monitor/diagnose", "/api/monitor/diagnose" in html)
    check("AI 失败降级：黄条说明原因（不假装有结论）", "AI 诊断不可用" in html)
    check("每条建议横线分割", ".diag-item{border-top:1px dashed var(--g4)" in html)


if __name__ == "__main__":
    test_logbuffer_parse()
    test_delta_and_colors()
    test_baseline()
    test_log_cmd_and_mock()
    test_frontend()
    test_ai_diagnose()
    test_three_layers()
    print(f"\n共 {len(PASS) + len(FAIL)} 项：OK {len(PASS)} / NG {len(FAIL)}")
    sys.exit(1 if FAIL else 0)
