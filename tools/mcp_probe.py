#!/usr/bin/env python3
"""mcp_probe.py —— 判断"某个程序是不是 MCP 服务器"（发 initialize + tools/list 看回应）。
用法: python3 mcp_probe.py <命令> [参数…]
例:   python3 mcp_probe.py ~/netops/netdev-mcp
      python3 mcp_probe.py /bin/ls
"""
import json, subprocess, sys, os
cmd = sys.argv[1:]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def call(o, timeout=5):
    p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
    import select
    r,_,_ = select.select([p.stdout], [], [], timeout)
    if not r: return None
    line = p.stdout.readline()
    try: return json.loads(line)
    except Exception: return {"__raw__": line[:80]}
try:
    init = call({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}})
    if not init or "result" not in (init or {}):
        print(f"  ❌ 不是 MCP 服务器（initialize 无响应/无 result）。原始回应: {str(init)[:70]}")
    else:
        si = init["result"].get("serverInfo", {})
        print(f"  ✅ 是 MCP 服务器：name={si.get('name')} version={si.get('version')} 协议={init['result'].get('protocolVersion')}")
        print(f"     能力(capabilities): {list((init['result'].get('capabilities') or {}).keys())}")
        t = call({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        tools = (t or {}).get("result", {}).get("tools", [])
        print(f"     tools/list → {len(tools)} 个工具" + (f"（例: {tools[0]['name']}）" if tools else ""))
finally:
    p.terminate()
