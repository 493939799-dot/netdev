"""HTML 报告生成（与既有 workbuddy 报告同一套样式）。"""
import datetime as _dt
import html
import pathlib

from . import paths as _paths
OUT = pathlib.Path.home() / "Desktop" / "workbuddy"   # 交付目录按用户习惯固定，不随安装路径变

CSS = """
:root { --line:#d8dde3; --head:#1f3a5f; --bg:#f7f9fb; --warn:#b3261e; --ok:#1a7f37; }
* { box-sizing:border-box; }
body { font-family:-apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif; margin:0; padding:32px 40px 64px; color:#1c1f23; background:#fff; line-height:1.6; }
h1 { font-size:23px; margin:0 0 4px; color:var(--head); }
.sub { color:#5b6672; font-size:13px; margin-bottom:22px; }
h2 { font-size:17px; margin:30px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--head); color:var(--head); }
h3 { font-size:14px; margin:18px 0 6px; color:#33445c; }
table { border-collapse:collapse; width:100%; font-size:13px; margin:8px 0 4px; }
th,td { border:1px solid var(--line); padding:6px 9px; text-align:left; vertical-align:top; }
th { background:var(--bg); font-weight:600; white-space:nowrap; }
code,pre { font-family:"SF Mono",Menlo,Consolas,monospace; font-size:12px; }
pre { background:#0f1621; color:#d6e2f0; padding:14px 16px; border-radius:6px; overflow-x:auto; line-height:1.45; }
code { background:#eef2f7; padding:1px 4px; border-radius:3px; }
.ok { background:#e8f5e9; border-left:4px solid var(--ok); padding:10px 14px; font-size:13px; margin:12px 0; border-radius:0 4px 4px 0; }
.note { background:#fff8e1; border-left:4px solid #e0a800; padding:10px 14px; font-size:13px; margin:12px 0; border-radius:0 4px 4px 0; }
.danger { background:#fdecea; border-left:4px solid var(--warn); padding:10px 14px; font-size:13px; margin:12px 0; border-radius:0 4px 4px 0; }
.bad { color:var(--warn); font-weight:600; } .good { color:var(--ok); font-weight:600; }
footer { margin-top:36px; font-size:12px; color:#78838f; border-top:1px solid var(--line); padding-top:12px; }
"""


def _esc(t):
    return html.escape(str(t or ""))


def write_report(path: pathlib.Path, title: str, subtitle: str, sections) -> pathlib.Path:
    """sections: list of (heading, html_body)"""
    body = []
    for h, b in sections:
        if h:
            body.append(f"<h2>{_esc(h)}</h2>")
        body.append(b)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>{CSS}</style></head><body>
<h1>{_esc(title)}</h1>
<div class="sub">{subtitle}</div>
{''.join(body)}
<footer>生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 生成者：netdev / pi<br>
本报告由自动流程产出，命令与回显均为原始留档。</footer>
</body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


def kv_table(pairs):
    rows = "".join(f"<tr><td>{_esc(k)}</td><td>{v if isinstance(v, str) and v.startswith('<') else _esc(v)}</td></tr>"
                   for k, v in pairs)
    return f'<table class="kv">{rows}</table>'


def pre(text):
    return f"<pre>{_esc(text)}</pre>"


def ok_box(text):
    return f'<div class="ok">{text}</div>'


def note_box(text):
    return f'<div class="note">{text}</div>'


def danger_box(text):
    return f'<div class="danger">{text}</div>'
