#Requires -Version 5.1
<#
.SYNOPSIS
    netdev 一键体检（Windows）—— 全面自检，并可一键修复「服务起不来」等常见故障。

.DESCRIPTION
    本脚本刻意**不依赖 netdev 命令行本身**：即使 netdev.cmd 起不来、依赖装残了，
    它也能把问题定位出来并尝试修复。体检范围：
      1) 安装定位与运行环境        2) Python / venv / 依赖
      3) 关键文件完整性            4) 配置文件（缺则从模板补齐）
      5) 网页服务与端口 8898       6) 串口、波特率与本机模拟器
      7) 残留 / 僵尸进程           8) 日志与报错
      9) 开机自启                 10) 目录写权限 / 磁盘空间

    默认只体检、只给修法，不动任何东西；
    加 -Fix 才动手修（幂等，可反复执行）。

.PARAMETER Fix
    执行修复：补齐配置、清掉占端口的僵尸进程、启动/重启网页服务、补装缺失依赖、重建开机自启。

.PARAMETER Root
    指定 netdev 安装根目录。默认顺序：
    -Root > $env:NETDEV_ROOT > %USERPROFILE%\netops > 脚本所在目录（含 04-源码-netdev 子目录）。

.PARAMETER Port
    网页服务端口。默认 8898（或环境变量 NETDEV_UI_PORT）。

.PARAMETER Tail
    服务起不来时，回显日志末尾行数。默认 25。

.PARAMETER NoPause
    结束后不等待按键（供脚本 / CI 调用）。

.EXAMPLE
    .\一键体检.ps1
    只体检，逐项给结论与修法。

.EXAMPLE
    .\一键体检.ps1 -Fix
    体检并自动修复能修的问题。

.EXAMPLE
    .\一键体检.ps1 -Fix -Root "$env:USERPROFILE\netops"
    指定安装根（多套安装 / 非默认目录时用）。
#>
[CmdletBinding()]
param(
    [switch]$Fix,
    [string]$Root = '',
    [int]$Port = 0,
    [int]$Tail = 25,
    [switch]$NoPause
)

$ErrorActionPreference = 'Continue'

# 中文在 PS 5.1 默认代码页（cp936）下会花，先把控制台切到 UTF-8。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$script:Pass  = 0
$script:Fail  = 0
$script:Warn  = 0
$script:Fixes = New-Object System.Collections.ArrayList

# ─────────────────────────────────────────────── 输出小工具
# 中文在等宽字体里占 2 格，用 .PadRight 会参差不齐 —— 按显示宽度对齐。
function Get-DisplayWidth([string]$s) {
    $w = 0
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        if (($c -ge 0x1100 -and $c -le 0x115F) -or ($c -ge 0x2E80 -and $c -le 0xA4CF) -or
            ($c -ge 0xAC00 -and $c -le 0xD7A3) -or ($c -ge 0xF900 -and $c -le 0xFAFF) -or
            ($c -ge 0xFE30 -and $c -le 0xFE6F) -or ($c -ge 0xFF00 -and $c -le 0xFF60) -or
            ($c -ge 0xFFE0 -and $c -le 0xFFE6)) { $w += 2 } else { $w += 1 }
    }
    return $w
}
function PadD([string]$s, [int]$width) {
    $pad = $width - (Get-DisplayWidth $s)
    if ($pad -lt 0) { $pad = 0 }
    return $s + (' ' * $pad)
}
function Section([string]$t) {
    Write-Host ''
    Write-Host ('── ' + $t + ' ' + ('─' * [Math]::Max(1, 50 - (Get-DisplayWidth $t)))) -ForegroundColor Cyan
}
function OK([string]$name, [string]$note = '') {
    $script:Pass++
    Write-Host ('  [OK] ' + (PadD $name 24) + $note) -ForegroundColor Green
}
function BAD([string]$name, [string]$note = '', [string]$fix = '') {
    $script:Fail++
    Write-Host ('  [!!] ' + (PadD $name 24) + $note) -ForegroundColor Yellow
    if ($fix) { Write-Host ('       -> ' + $fix) -ForegroundColor DarkGray }
}
function WARN([string]$name, [string]$note = '') {
    $script:Warn++
    Write-Host ('  [ ~] ' + (PadD $name 24) + $note) -ForegroundColor DarkYellow
}
function INFO([string]$name, [string]$note = '') {
    Write-Host ('  [  ] ' + (PadD $name 24) + $note) -ForegroundColor DarkGray
}
function FixNote([string]$msg) {
    [void]$script:Fixes.Add($msg)
    Write-Host ('  [修复] ' + $msg) -ForegroundColor Magenta
}

# ─────────────────────────────────────────────── 基础探测
function Test-NetdevRoot([string]$p) {
    if (-not $p) { return $false }
    if (-not (Test-Path -LiteralPath $p)) { return $false }
    return ((Test-Path -LiteralPath (Join-Path $p 'netdev_cli.py')) -or
            (Test-Path -LiteralPath (Join-Path $p 'ui\server.py')))
}

function Resolve-NetdevRoot([string]$explicit) {
    $cands = New-Object System.Collections.ArrayList
    if ($explicit)        { [void]$cands.Add($explicit) }
    if ($env:NETDEV_ROOT) { [void]$cands.Add($env:NETDEV_ROOT) }
    if ($env:USERPROFILE) { [void]$cands.Add((Join-Path $env:USERPROFILE 'netops')) }
    if ($PSScriptRoot) {
        [void]$cands.Add($PSScriptRoot)
        [void]$cands.Add((Join-Path $PSScriptRoot '04-源码-netdev'))
    }
    foreach ($c in $cands) {
        if (Test-NetdevRoot $c) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return $null
}

function Get-VenvPythonPath {
    foreach ($c in @((Join-Path $script:Root '.venv\Scripts\python.exe'),
                     (Join-Path $script:Root '.venv\bin\python'))) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Get-SystemPython {
    $w = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    $w = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    return $null
}

function Get-PortOwners([int]$p) {
    $res = New-Object System.Collections.ArrayList
    try {
        $lines = & netstat -ano -p TCP 2>$null
        foreach ($ln in $lines) {
            if ($ln -match (':' + $p + '\s') -and $ln -match 'LISTENING') {
                $parts = @($ln -split '\s+' | Where-Object { $_ -ne '' })
                $procId = $parts[-1]
                if ($procId -match '^\d+$') { [void]$res.Add([int]$procId) }
            }
        }
    } catch {}
    return @($res | Sort-Object -Unique)
}

function Get-ProcName([int]$procId) {
    try { return (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch { return '未知' }
}

# Windows venv 的 python.exe 是「跳板」：它自己起来后，会再拉一个基座解释器跑同一份脚本
# （实测：venv\Scripts\python.exe → Python312\python.exe，两条记录同一个服务）。
# 按「解释器之后的命令行」归并，避免把 1 个服务数成 2 个、把用户看晕。
function Get-ProcSig([string]$cmd) {
    if (-not $cmd) { return '' }
    $c = $cmd.Trim()
    if ($c.StartsWith('"')) {
        $c = $c.Substring(1)
        $i = $c.IndexOf('"')
        if ($i -ge 0) { $c = $c.Substring($i + 1) }
    } else {
        $sp = $c.IndexOf(' ')
        if ($sp -ge 0) { $c = $c.Substring($sp + 1) }
    }
    return $c.Trim().ToLower()
}

function Stop-Proc([int]$procId) {
    try { & taskkill /PID $procId /T /F 2>&1 | Out-Null } catch {}
}

function Test-UiHealth([int]$timeoutSec = 3) {
    $url = 'http://127.0.0.1:' + $Port + '/api/health'
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Timeout = $timeoutSec * 1000
        $req.Method = 'GET'
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $sr.ReadToEnd()
        $sr.Close(); $resp.Close()
        return @{ Up = ($body -match '"ok"\s*:\s*true'); Body = $body.Trim() }
    } catch {
        return @{ Up = $false; Body = '' }
    }
}

# 通用 TCP 探活（模拟器 / 任意监听端口）。超时短、失败不抛。
function Test-TcpPort([string]$h, [int]$p, [int]$timeoutMs = 800) {
    $c = $null
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        if ($c) { try { $c.Close() } catch {} }
    }
}

# 服务跑的是不是旧代码：拿服务启动时写的哈希清单与当前磁盘比对
function Test-CodeStale {
    $man = Join-Path $script:Root ('logs\ui-service-' + $Port + '.code.json')
    if (-not (Test-Path -LiteralPath $man)) { return @{ Stale = $false; Why = '' } }
    try {
        $j = Get-Content -LiteralPath $man -Raw -Encoding UTF8 | ConvertFrom-Json
        $changed = New-Object System.Collections.ArrayList
        foreach ($prop in $j.files.PSObject.Properties) {
            $fp = Join-Path $script:Root ($prop.Name -replace '/', '\')
            if (-not (Test-Path -LiteralPath $fp)) { [void]$changed.Add($prop.Name + '(已消失)'); continue }
            $h = (Get-FileHash -LiteralPath $fp -Algorithm SHA256).Hash.ToLower()
            if ($h -ne ([string]$prop.Value).ToLower()) { [void]$changed.Add($prop.Name) }
        }
        if ($changed.Count -gt 0) {
            $show = (@($changed | Select-Object -First 3) -join '、')
            return @{ Stale = $true; Why = $show }
        }
    } catch {}
    return @{ Stale = $false; Why = '' }
}

function Start-UiService {
    $py = Get-VenvPythonPath
    if (-not $py) { return $false }
    $daemon = Join-Path $script:Root 'ui\daemonize.py'
    if (-not (Test-Path -LiteralPath $daemon)) { return $false }
    New-Item -ItemType Directory -Force -Path (Join-Path $script:Root 'logs') | Out-Null
    $env:PYTHONUTF8 = '1'
    $null = & $py $daemon --port $Port --host 127.0.0.1 `
        --pidfile (Join-Path $script:Root 'logs\ui-service.pid') `
        --logfile (Join-Path $script:Root 'logs\ui-service.log') 2>&1
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep -Milliseconds 700
        if ((Test-UiHealth 2).Up) { return $true }
    }
    return $false
}

function Stop-UiService {
    $py = Get-VenvPythonPath
    $daemon = Join-Path $script:Root 'ui\daemonize.py'
    if ($py -and (Test-Path -LiteralPath $daemon)) {
        $env:PYTHONUTF8 = '1'
        $null = & $py $daemon --port $Port --stop 2>&1
        Start-Sleep -Milliseconds 500
    }
    foreach ($o in @(Get-PortOwners $Port)) { Stop-Proc $o }
}

function Test-Writable([string]$dir) {
    try {
        if (-not (Test-Path -LiteralPath $dir)) { return $false }
        $t = Join-Path $dir ('.netdev-wtest-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        [System.IO.File]::WriteAllText($t, 'ok')
        Remove-Item -LiteralPath $t -Force
        return $true
    } catch { return $false }
}

function Get-Devices {
    $f = Join-Path $script:Root 'config\devices.toml'
    if (-not (Test-Path -LiteralPath $f)) { $f = Join-Path $script:Root 'devices.toml' }
    if (-not (Test-Path -LiteralPath $f)) { return @() }
    $devs = New-Object System.Collections.ArrayList
    $cur = $null
    foreach ($ln in (Get-Content -LiteralPath $f -Encoding UTF8 -ErrorAction SilentlyContinue)) {
        if ($ln -match '^\s*\[\[device\]\]') { if ($cur) { [void]$devs.Add($cur) }; $cur = @{}; continue }
        if ($cur -ne $null -and $ln -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*(#.*)?$') {
            $k = $Matches[1]
            $v = $Matches[2].Trim().Trim('"')
            $cur[$k] = $v
        }
    }
    if ($cur) { [void]$devs.Add($cur) }
    return @($devs)
}

function Get-LogTail([string]$path, [int]$n) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    return @(Get-Content -LiteralPath $path -Encoding UTF8 -Tail $n -ErrorAction SilentlyContinue)
}

# ═══════════════════════════════════════════════ 开跑
Write-Host ''
Write-Host '╔══════════════════════════════════════════════════════════╗' -ForegroundColor Cyan
Write-Host '║        netdev 一键体检（Windows）  ·  只读自检 / -Fix 修复  ║' -ForegroundColor Cyan
Write-Host '╚══════════════════════════════════════════════════════════╝' -ForegroundColor Cyan

if ($Port -le 0) {
    if ($env:NETDEV_UI_PORT) { $Port = [int]$env:NETDEV_UI_PORT } else { $Port = 8898 }
}

# ── 1. 定位与运行环境
Section '1. 安装定位与运行环境'
$script:Root = Resolve-NetdevRoot $Root
if (-not $script:Root) {
    BAD '安装根' '没找到 netdev 安装（缺 netdev_cli.py / ui\server.py）' `
        '指定目录重跑：.\一键体检.ps1 -Root "<安装目录>"'
    Write-Host ''
    Write-Host '没定位到安装根，后面无法继续。常见位置：' -ForegroundColor Yellow
    Write-Host '  · 安装版：%USERPROFILE%\netops' -ForegroundColor DarkGray
    Write-Host '  · 源码版：解压后的 04-源码-netdev 目录' -ForegroundColor DarkGray
    if (-not $NoPause) { Write-Host ''; Read-Host '按回车退出' | Out-Null }
    exit 2
}
OK '安装根' $script:Root

$isInstalled = Test-Path -LiteralPath (Join-Path $script:Root 'netdev.cmd')
INFO '形态' ($(if ($isInstalled) { '安装版（netdev.cmd 就绪）' } else { '源码版（走 netdev_cli.py）' }))
INFO 'PowerShell' ($PSVersionTable.PSVersion.ToString() + '  ·  ' + [Environment]::OSVersion.VersionString)
$isAdmin = $false
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch {}
INFO '管理员权限' ($(if ($isAdmin) { '是' } else { '否（一般够用；个别情况需管理员）' }))

# ── 2. Python / venv / 依赖
Section '2. Python / venv / 依赖'
$script:Py = Get-VenvPythonPath
if ($script:Py) {
    $ver = (& $script:Py -V 2>&1 | Out-String).Trim()
    OK 'venv python' ($ver + '  ' + $script:Py)
    $env:PYTHONUTF8 = '1'
    # 传给 python 的代码里**不要出现双引号**：PowerShell 传参给原生 exe 时会吞掉双引号
    # （实测 `"|"` 变成 `|` → SyntaxError，被误报成「缺依赖」）。用空格分隔即可。
    $missing = @()
    foreach ($m in @('netmiko', 'serial', 'winpty', 'keyring')) {
        $null = & $script:Py -c ('import ' + $m) 2>$null
        if ($LASTEXITCODE -ne 0) { $missing += $m }
    }
    if ($missing.Count -eq 0) {
        $v = (& $script:Py -c 'import netmiko,serial;print(netmiko.__version__, serial.__version__)' 2>$null | Out-String).Trim()
        OK '关键依赖' ('netmiko / pyserial / pywinpty / keyring 均可导入  v' + $v)
    } else {
        BAD '关键依赖' ('缺：' + ($missing -join ', ')) '补装：.venv\Scripts\python.exe -m pip install -r requirements-win.txt（安装版用 requirements.txt）'
        if ($Fix) {
            $req = Join-Path $script:Root 'requirements-win.txt'
            if (-not (Test-Path -LiteralPath $req)) { $req = Join-Path $script:Root 'requirements.txt' }
            if (Test-Path -LiteralPath $req) {
                INFO 'pip install' ('正在补装依赖，可能需要几分钟：' + (Split-Path $req -Leaf))
                $null = & $script:Py -m pip install -r $req 2>&1 | Out-String
                $left = @()
                foreach ($m in $missing) {
                    $null = & $script:Py -c ('import ' + $m) 2>$null
                    if ($LASTEXITCODE -ne 0) { $left += $m }
                }
                if ($left.Count -eq 0) { FixNote '依赖已补齐' } else { FixNote ('仍缺：' + ($left -join ', ') + '（见上方 pip 输出）') }
            } else {
                FixNote '没找到 requirements 文件，无法自动补装'
            }
        }
    }
} else {
    BAD 'venv python' ('缺 ' + (Join-Path $script:Root '.venv\Scripts\python.exe')) '重建虚拟环境（见下）'
    $syspy = Get-SystemPython
    if ($syspy) {
        INFO '系统 Python' $syspy
        if ($Fix) {
            INFO 'venv' '正在创建 .venv 并安装依赖（耗时较长，请稍候）…'
            $null = & $syspy -m venv (Join-Path $script:Root '.venv') 2>&1 | Out-String
            $script:Py = Get-VenvPythonPath
            if ($script:Py) {
                $req = Join-Path $script:Root 'requirements-win.txt'
                if (-not (Test-Path -LiteralPath $req)) { $req = Join-Path $script:Root 'requirements.txt' }
                if (Test-Path -LiteralPath $req) { $null = & $script:Py -m pip install -r $req 2>&1 | Out-String }
                FixNote '已重建 .venv 并安装依赖'
            } else {
                FixNote 'venv 创建失败：请检查系统 Python 与网络'
            }
        } else {
            Write-Host '       -> 修复：.\一键体检.ps1 -Fix（会用系统 Python 重建 .venv 并装依赖）' -ForegroundColor DarkGray
        }
    } else {
        BAD '系统 Python' 'PATH 里没有 python，无法重建 venv' '装 Python 3.10+ 并勾选 Add to PATH，再重跑 -Fix'
    }
}

# ── 3. 关键文件
Section '3. 关键文件完整性'
$files = @(
    @('netdev_cli.py',        '命令行入口（必需）'),
    @('ui\server.py',         '网页服务（必需）'),
    @('ui\daemonize.py',      '服务守护启动器（必需）'),
    @('lib\paths.py',         '路径真源（必需）'),
    @('tools\pane_daemon.py', '同屏会话守护'),
    @('tools\serial_bridge.py','串口桥'),
    @('tools\quick_conn.py',  '接入脚本'),
    @('netdev-mcp.cmd',       'MCP 入口（外部工具对接用）')
)
foreach ($pair in $files) {
    $rel = $pair[0]; $desc = $pair[1]
    $full = Join-Path $script:Root $rel
    if (Test-Path -LiteralPath $full) { OK $rel $desc }
    else { BAD $rel ('缺失（' + $desc + '）') '安装不完整 → 重新安装（05-安装包 里的 installer）' }
}

# ── 4. 配置
Section '4. 配置文件'
$cfgDir = Join-Path $script:Root 'config'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $cfgDir 'state') | Out-Null

$derived = @('devices.toml', 'connections.json', 'pi-commands.json', '_netdev', 'netdev.bash', 'AGENTS.workspace.md')
$madeCfg = New-Object System.Collections.ArrayList
foreach ($n in $derived) {
    $dst = Join-Path $cfgDir $n
    if (Test-Path -LiteralPath $dst) { continue }
    $src = Join-Path $cfgDir ($n + '.example')
    if (-not (Test-Path -LiteralPath $src)) { continue }
    if ($Fix) {
        $t = Get-Content -LiteralPath $src -Raw -Encoding UTF8
        $t = $t.Replace('__PREFIX__', $script:Root).Replace('__HOME__', $env:USERPROFILE)
        [System.IO.File]::WriteAllText($dst, $t, (New-Object System.Text.UTF8Encoding($false)))
        [void]$madeCfg.Add($n)
    } else {
        [void]$madeCfg.Add($n)
    }
}
if ($madeCfg.Count -gt 0) {
    if ($Fix) { FixNote ('已从模板生成配置：' + ($madeCfg -join '、')) }
    else { BAD '配置文件' ('缺：' + ($madeCfg -join '、')) '从模板补齐：.\一键体检.ps1 -Fix（或手工 cp config\<同名>.example config\<同名>）' }
} else {
    OK '配置文件' 'devices.toml / connections.json 等齐全'
}

$devsToml = Join-Path $cfgDir 'devices.toml'
$devs = @(Get-Devices)
$serials = @($devs | Where-Object { ([string]$_['protocol']).ToLower() -eq 'serial' })
if (Test-Path -LiteralPath $devsToml) {
    OK '设备清单' ('devices.toml · ' + $devs.Count + ' 台（串口 ' + $serials.Count + ' 台）')
    foreach ($d in $devs) {
        $proto = ([string]$d['protocol']).ToLower()
        if ($proto -eq 'serial') {
            $desc = '串口 ' + [string]$d['port'] + ' @ ' + [string]$d['baud']
        } else {
            $desc = $proto + ' ' + [string]$d['host'] + ':' + [string]$d['port']
        }
        INFO ('  · ' + [string]$d['name']) $desc
    }
} else {
    BAD '设备清单' 'config\devices.toml 不存在' '首次使用属正常：.\一键体检.ps1 -Fix 会从模板生成（内含本机模拟器）'
}
$webToml = Join-Path $cfgDir 'web.toml'
if (Test-Path -LiteralPath $webToml) { OK 'web.toml' '存在' } else { WARN 'web.toml' '缺失（网页终端页对接可能需要）' }

# ── 5. 网页服务
Section ('5. 网页服务（netdev-ui :' + $Port + '）')
$pidFile = Join-Path $script:Root 'logs\ui-service.pid'

$health = Test-UiHealth 3
$st = @{ Stale = $false; Why = '' }
if ($health.Up) { $st = Test-CodeStale }
$wasOk = ($health.Up -and -not $st.Stale)

# 先修，再定论 —— 免得「已修好」却仍在汇总里计一笔失败。
if (-not $wasOk -and $Fix) {
    if ($health.Up -and $st.Stale) {
        Stop-UiService
        $null = Start-UiService
        FixNote '服务跑的是旧代码 → 已重启加载最新代码'
    } else {
        $owners0 = @(Get-PortOwners $Port)
        if ($owners0.Count -gt 0) {
            $d0 = (@($owners0 | ForEach-Object { ([string]$_ + '(' + (Get-ProcName $_) + ')') }) -join ', ')
            foreach ($o in $owners0) { Stop-Proc $o }
            Start-Sleep -Milliseconds 700
            FixNote ('已清掉占用 ' + $Port + ' 的进程：' + $d0)
        }
        if (Start-UiService) { FixNote '已启动网页服务' } else { FixNote '启动失败，见日志末尾' }
    }
    Start-Sleep -Milliseconds 400
    $health = Test-UiHealth 4
    $st = @{ Stale = $false; Why = '' }
    if ($health.Up) { $st = Test-CodeStale }
}

$pidInFile = 0
if (Test-Path -LiteralPath $pidFile) {
    try { $pidInFile = [int]((Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8).Trim()) } catch {}
}

if ($health.Up -and -not $st.Stale) {
    OK ('netdev-ui :' + $Port) ('健康 · HTTP 200' + $(if ($pidInFile) { '  PID ' + $pidInFile } else { '' }))
    INFO '当前地址' ('http://127.0.0.1:' + $Port)
} elseif ($health.Up -and $st.Stale) {
    BAD ('netdev-ui :' + $Port) ('仍在跑旧代码（' + $st.Why + ' 改过）') '重启：netdev ui restart（或 .\一键体检.ps1 -Fix）'
} else {
    $owners = @(Get-PortOwners $Port)
    if ($owners.Count -gt 0) {
        $desc = (@($owners | ForEach-Object { ([string]$_ + '(' + (Get-ProcName $_) + ')') }) -join ', ')
        BAD ('netdev-ui :' + $Port) ('端口被占但不健康：' + $desc) '清掉占用进程后重启：.\一键体检.ps1 -Fix'
    } else {
        BAD ('netdev-ui :' + $Port) '未运行（启动失败）' '看第 8 节日志末尾；或手动 netdev ui start'
    }
}

# ── 6. 串口
Section '6. 串口与波特率'
$comPorts = @()
try { $comPorts = @([System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object) } catch {}
if ($comPorts.Count -gt 0) { OK '系统串口' ($comPorts -join ', ') }
else { INFO '系统串口' '当前无 COM 口（USB-Console 线没插时属正常）' }
if ($serials.Count -gt 0) {
    foreach ($d in $serials) {
        $wantPort = [string]$d['port']
        $hit = $false
        foreach ($cp in $comPorts) { if ($wantPort -and ($cp -ieq $wantPort)) { $hit = $true } }
        if ($wantPort -and ($wantPort.ToLower() -ne 'auto') -and (-not $hit)) {
            WARN ('串口 ' + [string]$d['name']) ('清单写的是 ' + $wantPort + '，但当前系统里没有这个口（线没插 / 换了口）')
        } else {
            OK ('串口 ' + [string]$d['name']) ('port=' + $wantPort + ' baud=' + [string]$d['baud'])
        }
    }
}
$baudCache = Join-Path $script:Root 'state\serial_baud.json'
if (-not (Test-Path -LiteralPath $baudCache)) { $baudCache = Join-Path $cfgDir 'state\serial_baud.json' }
if (Test-Path -LiteralPath $baudCache) {
    try {
        $bj = Get-Content -LiteralPath $baudCache -Raw -Encoding UTF8 | ConvertFrom-Json
        $pairs = @()
        foreach ($pp in $bj.PSObject.Properties) { $pairs += ($pp.Name + '=' + $pp.Value) }
        INFO '波特率缓存' ($pairs -join '  ')
    } catch {}
} else {
    INFO '波特率缓存' '暂无（首次接入某串口时会自动探测并写入）'
}

# 本机模拟器（清单里 sim = true 的回环 TCP 设备）：没起时界面里报「设备连不上 /
# NetmikoTimeoutException」—— 与真机没插线的表现几乎一样，是高频误判源。
$sims = @($devs | Where-Object { ([string]$_['sim']).ToLower() -eq 'true' })
foreach ($s in $sims) {
    $nm = [string]$s['name']; $h = [string]$s['host']; $pp = [string]$s['port']
    if (-not $h -or ($pp -notmatch '^\d+$')) { continue }
    $isLoop = ($h -in @('127.0.0.1', 'localhost', '::1'))
    $up = Test-TcpPort $h ([int]$pp)

    # 先修，再定论（与第 5 节同约定）：本机模拟器没起就顺手拉起，再重新探活。
    if (-not $up -and $Fix -and $isLoop) {
        $started = $false
        $netdevCmd = Join-Path $script:Root 'netdev.cmd'
        if (Test-Path -LiteralPath $netdevCmd) {
            try { $null = & $netdevCmd mock start 2>&1 } catch {}
            for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Milliseconds 200; if (Test-TcpPort $h ([int]$pp)) { $started = $true; break } }
        }
        if (-not $started) {
            # 退化路径：netdev.cmd 不可用/起不来时，直接用 venv python 拉起模拟器脚本。
            $simScript = Join-Path $script:Root 'tests\mock_vrp.py'
            $py = Get-VenvPythonPath
            if ($py -and (Test-Path -LiteralPath $simScript)) {
                $stDir = Join-Path $script:Root 'state'
                New-Item -ItemType Directory -Force -Path $stDir | Out-Null
                $log = Join-Path $stDir ('mock-' + $pp + '.log')
                try {
                    $null = Start-Process -FilePath $py -ArgumentList ('"' + $simScript + '"'), $pp `
                        -WorkingDirectory $script:Root -RedirectStandardOutput $log `
                        -RedirectStandardError ($log + '.err') -PassThru
                } catch {}
                for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Milliseconds 200; if (Test-TcpPort $h ([int]$pp)) { $started = $true; break } }
            }
        }
        if ($started) { FixNote ('已启动本机模拟器 ' + $nm + '（' + $h + ':' + $pp + '）') }
        else { FixNote ('模拟器 ' + $nm + ' 启动失败 → 手动 netdev mock start，看 state\mock-' + $pp + '.log') }
        $up = Test-TcpPort $h ([int]$pp)
    }

    if ($up) {
        OK ('模拟器 ' + $nm) ($h + ':' + $pp + ' 在监听（清单标为非真机）')
    } elseif (-not $isLoop) {
        WARN ('模拟器 ' + $nm) ($h + ':' + $pp + ' 连不上（远端模拟器，需在对方机器上起）')
    } else {
        BAD ('模拟器 ' + $nm) ($h + ':' + $pp + ' 没在监听 → 界面里会报「连不上 / NetmikoTimeoutException」') `
            '启动：netdev mock start（或 .\一键体检.ps1 -Fix）'
    }
}

# ── 7. 残留 / 僵尸进程
Section '7. 进程与残留'
$procs = @()
try {
    $rawProcs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -and $_.CommandLine -like ('*' + $script:Root + '*') })
    # 归并 venv 跳板对（跳板 + 基座解释器跑同一份脚本），让计数反映真实逻辑进程数。
    $seenSig = @{}
    foreach ($p in $rawProcs) {
        $sig = Get-ProcSig $p.CommandLine
        if (-not $sig) { $sig = 'pid:' + $p.ProcessId }
        if ($seenSig.ContainsKey($sig)) { continue }
        $seenSig[$sig] = $true
        $procs += $p
    }
    if ($rawProcs.Count -gt $procs.Count) {
        INFO '进程归并' ($rawProcs.Count.ToString() + ' 条 python 记录 → ' + $procs.Count.ToString() + ' 个逻辑进程（venv 跳板去重）')
    }
} catch {}
$panes = @($procs | Where-Object { $_.CommandLine -match 'pane_daemon' })
$bridges = @($procs | Where-Object { $_.CommandLine -match 'serial_bridge' })
$servers = @($procs | Where-Object { $_.CommandLine -match 'ui[\\/]server\.py' })
$mocks = @($procs | Where-Object { $_.CommandLine -match 'mock_vrp|mock_telnet' })
INFO 'pane-daemon' ($panes.Count.ToString() + ' 个（同屏会话守护）')
INFO 'serial_bridge' ($bridges.Count.ToString() + ' 个（串口桥）')
INFO 'ui server' ($servers.Count.ToString() + ' 个（网页服务进程）')
if ($mocks.Count -gt 0) { WARN '模拟器' ($mocks.Count.ToString() + ' 个在跑（测试用；不用了可 netdev mock stop）') }
if ($health.Up -and $servers.Count -eq 0) {
    WARN 'ui server' '服务健康但没识别到本安装目录下的 server 进程（可能以其它方式启动）'
}
# 残留串口桥：桥由 pane-daemon 拉起。没有守护却还有桥 = 上次会话留下的僵尸，
# 它会一直占住 COM 口 → 重新接入时打不开串口。这是「接不上 / 服务起不来」的高频原因。
if ($panes.Count -eq 0 -and $bridges.Count -gt 0) {
    $portList = (@($bridges | ForEach-Object {
        if ($_.CommandLine -match 'serial_bridge\.py"?\s+(\S+)\s+(\S+)') { $Matches[1] + '@' + $Matches[2] }
    }) -join ', ')
    WARN '串口桥残留' ($bridges.Count.ToString() + ' 个桥进程但没有 pane-daemon 守护（' + $portList + '）→ 占住串口，新会话接不上')
    if ($Fix) {
        foreach ($b in $bridges) { Stop-Proc $b.ProcessId }
        FixNote ('已清理 ' + $bridges.Count + ' 个残留串口桥，释放串口')
    } else {
        Write-Host '       -> 清理：.\一键体检.ps1 -Fix' -ForegroundColor DarkGray
    }
}

# ── 8. 日志与报错
Section '8. 日志与报错'
$logFile = Join-Path $script:Root 'logs\ui-service.log'
if (Test-Path -LiteralPath $logFile) {
    $lsz = (Get-Item -LiteralPath $logFile).Length
    OK 'ui-service.log' ('存在 · ' + [Math]::Round($lsz / 1KB, 1).ToString() + ' KB')
    if ($lsz -gt 50MB) { WARN '日志偏大' '建议清理 / 轮转 logs\ui-service.log' }
    $tailLines = @(Get-LogTail $logFile 200)
    $errs = @($tailLines | Where-Object { $_ -match 'Traceback|Error|Exception|Address already in use|拒绝访问|PermissionError|Errno' })
    if ($errs.Count -gt 0) { WARN '近期报错' ($errs.Count.ToString() + ' 行命中（详见下）') }
    else { OK '近期报错' '末 200 行未见明显错误' }
    if ((-not $health.Up) -or ($errs.Count -gt 0)) {
        Write-Host ''
        Write-Host ('  —— ' + $logFile + ' 末尾 ' + $Tail + ' 行 ——') -ForegroundColor DarkGray
        foreach ($l in (Get-LogTail $logFile $Tail)) { Write-Host ('    ' + $l) -ForegroundColor DarkGray }
    }
} else {
    INFO 'ui-service.log' '还没有（服务没起来过；-Fix 启动后会生成）'
}
$approvalLog = Join-Path $script:Root 'logs\approvals.log'
if (Test-Path -LiteralPath $approvalLog) { INFO 'approvals.log' ('写操作审批流水 · ' + [Math]::Round((Get-Item -LiteralPath $approvalLog).Length / 1KB, 1).ToString() + ' KB') }

# ── 9. 开机自启
Section '9. 开机自启'
$startupLnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\netdev-ui.lnk'
$runKeyOk = $false
try {
    $v = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'netdev-ui' -ErrorAction Stop
    if ($v) { $runKeyOk = $true }
} catch {}
$lnkExists = Test-Path -LiteralPath $startupLnk
$lnkHidden = $false
if ($lnkExists) {
    try {
        $wsq = New-Object -ComObject WScript.Shell
        $lnkHidden = ($wsq.CreateShortcut($startupLnk).TargetPath -match 'pythonw\.exe$')
    } catch {}
}
if ($lnkExists -and $lnkHidden) {
    OK '开机自启' '已装（Startup 快捷方式 · pythonw 隐藏启动，不弹控制台）'
} elseif ($lnkExists) {
    WARN '开机自启' '已装，但指向旧的控制台入口 —— 每次登录会闪一个黑色窗口' '修复：.\一键体检.ps1 -Fix（重建为隐藏启动）'
} elseif ($runKeyOk) {
    OK '开机自启' '已装（注册表 Run 键）'
} else {
    WARN '开机自启' '未装 —— 重启后要手动 netdev ui start' '安装：.\一键体检.ps1 -Fix（重建 Startup 快捷方式）'
}
if ((-not $lnkExists) -or (-not $lnkHidden)) {
    if ($Fix) {
        $pyw = Join-Path $script:Root '.venv\Scripts\pythonw.exe'
        if (Test-Path -LiteralPath $pyw) {
            try {
                $ws = New-Object -ComObject WScript.Shell
                $sc = $ws.CreateShortcut($startupLnk)
                $sc.TargetPath = $pyw
                $sc.Arguments = '"' + (Join-Path $script:Root 'netdev_cli.py') + '" ui'
                $sc.WorkingDirectory = $script:Root
                $sc.WindowStyle = 7
                $sc.Description = 'netdev 网络设备工具台 · 登录后隐藏启动'
                $sc.Save()
                FixNote '已重建开机自启快捷方式（pythonw 隐藏启动 netdev ui）'
            } catch { FixNote ('开机自启创建失败：' + $_.Exception.Message) }
        } else {
            INFO '开机自启' '源码版没有 .venv\Scripts\pythonw.exe，跳过（源码版通常不需要开机自启）'
        }
    }
}

# ── 10. 写权限 / 磁盘
Section '10. 目录写权限与磁盘'
foreach ($d in @('', 'config', 'config\state', 'logs', 'state', 'live', 'backups')) {
    $full = $script:Root
    if ($d) { $full = Join-Path $script:Root $d }
    if (-not (Test-Path -LiteralPath $full)) {
        if ($Fix) { New-Item -ItemType Directory -Force -Path $full | Out-Null; FixNote ('已创建目录 ' + $d) }
        else { WARN ('目录 ' + $(if ($d) { $d } else { '.' })) '不存在' }
        continue
    }
    if (Test-Writable $full) { OK ('可写 ' + $(if ($d) { $d } else { '安装根' })) '' }
    else { BAD ('可写 ' + $(if ($d) { $d } else { '安装根' })) '写不进去（权限 / 杀软拦截）' '右键以管理员身份运行；或检查 Windows Defender「受控文件夹访问」' }
}
try {
    $driveLetter = (Split-Path -Qualifier $script:Root).TrimEnd(':')
    $pd = Get-PSDrive -Name $driveLetter -ErrorAction Stop
    $freeGB = [Math]::Round($pd.Free / 1GB, 1)
    if ($freeGB -lt 1) { BAD '磁盘空间' ($freeGB.ToString() + ' GB 可用') '空间不足会导致服务/日志写入失败，请清理' }
    elseif ($freeGB -lt 5) { WARN '磁盘空间' ($freeGB.ToString() + ' GB 可用') '偏少，建议清理' }
    else { OK '磁盘空间' ($freeGB.ToString() + ' GB 可用') }
} catch {}

# ── 汇总
Write-Host ''
Write-Host '══════════════════════════════════════════════════════════' -ForegroundColor Cyan
$total = $script:Pass + $script:Fail
if ($script:Fail -eq 0) {
    Write-Host ('  体检完成：' + $script:Pass + '/' + $total + ' 项通过') -ForegroundColor Green
    if ($script:Warn -gt 0) { Write-Host ('  另有 ' + $script:Warn + ' 项提醒（不影响使用）') -ForegroundColor DarkYellow }
    if ($health.Up) {
        Write-Host ('  网页服务在跑 → http://127.0.0.1:' + $Port) -ForegroundColor Green
    }
} else {
    Write-Host ('  体检完成：' + $script:Pass + '/' + $total + ' 项通过，' + $script:Fail + ' 项有问题') -ForegroundColor Yellow
    if (-not $Fix) {
        Write-Host '  → 加 -Fix 重跑，可自动修复上面能修的问题：' -ForegroundColor White
        Write-Host '       .\一键体检.ps1 -Fix' -ForegroundColor White
    }
}
if ($script:Fixes.Count -gt 0) {
    Write-Host ''
    Write-Host ('  本次已修复 ' + $script:Fixes.Count + ' 项：') -ForegroundColor Magenta
    foreach ($f in $script:Fixes) { Write-Host ('    · ' + $f) -ForegroundColor Magenta }
}
Write-Host '══════════════════════════════════════════════════════════' -ForegroundColor Cyan
Write-Host ''

if (-not $NoPause) { Read-Host '按回车退出' | Out-Null }
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
