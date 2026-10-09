# ═══════════════════════════════════════════════════════════════════════════
#  netdev 网络设备工具台 · Windows x64 一键安装
#  用法：
#     powershell -ExecutionPolicy Bypass -File install.ps1
#     powershell -ExecutionPolicy Bypass -File install.ps1 -Prefix D:\netops
#     powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun
#     powershell -ExecutionPolicy Bypass -File install.ps1 -NoStartUi
#  特性：幂等（重复跑只更新程序，保留 config/ 与 backups/）
# ═══════════════════════════════════════════════════════════════════════════
[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:USERPROFILE 'netops'),
    [switch]$DryRun,
    [switch]$NoStartUi,
    [switch]$NoAutoStart,
    [string]$UiPort = $env:NETDEV_UI_PORT
)
$ErrorActionPreference = 'Stop'
$HERE = $PSScriptRoot

# ★ 统一 UTF-8 模式：netdev 的输出、日志、JSON、MCP 报文都含中文。
#   中文 Windows 的默认编码是 cp936，「子进程按 UTF-8 写 / 父进程按 cp936 读」
#   这类混用会直接解码失败或显示乱码。这里把本安装器进程与它拉起的全部
#   netdev 子进程统一到 UTF-8，从源头消掉这一类问题。
$env:PYTHONUTF8 = '1'

function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [X]  $m" -ForegroundColor Red }
function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Do-Step([scriptblock]$Block, [string]$Desc) {
    if ($DryRun) { Write-Host "    [dry-run] $Desc" -ForegroundColor DarkGray }
    else { & $Block }
}

Write-Host "netdev Windows 安装" -ForegroundColor White
Write-Host "  安装目录: $Prefix"
if ($DryRun) { Write-Host "  【dry-run：只预览，不动文件】" }

# ── 1. 环境检查 ─────────────────────────────────────────────────────────
Step "1/8 环境检查"
if (-not [Environment]::Is64BitOperatingSystem) { Bad '仅支持 64 位 Windows'; exit 1 }
Ok "Windows $([Environment]::OSVersion.Version)"

function Test-PythonExe([string]$Exe) {
    if (-not (Test-Path $Exe)) { return $false }
    # 微软商店 App Execution Alias 是个 0 字节 reparse 桩，不能用
    $real = (Resolve-Path $Exe).Path
    if ($real -match '\\WindowsApps\\.*python.*\.exe$') { return $false }
    # ★ 内嵌引号必须用单引号：PS 5.1 向原生命令转参时会吃掉双引号
    $probe = "import sys,platform;sys.exit(0 if sys.version_info>=(3,10) and platform.architecture()[0]=='64bit' else 1)"
    try {
        & $real -c $probe 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    $paths = New-Object System.Collections.Generic.List[string]
    # 1) py 启动器
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $probe = "import sys,platform;sys.exit(0 if sys.version_info>=(3,10) and platform.architecture()[0]=='64bit' else 1)"
        try {
            & $py.Path -3 -c $probe 2>$null
            if ($LASTEXITCODE -eq 0) {
                return (& $py.Path -3 -c 'import sys;print(sys.executable)' | Select-Object -Last 1).Trim()
            }
        } catch {}
    }
    # 2) PATH 上的 python（排除商店别名）
    foreach ($g in (Get-Command python -ErrorAction SilentlyContinue)) {
        if (Test-PythonExe $g.Path) { $paths.Add($g.Path) }
    }
    # 3) 常见安装目录（Python 官方安装器默认位置）
    $roots = @($env:LOCALAPPDATA, 'C:\') | ForEach-Object { Join-Path $_ 'Programs\Python' }
    $roots += 'C:\Python312','C:\Python311','C:\Python310'
    foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem $r -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | ForEach-Object {
                $paths.Add((Join-Path $_.FullName 'python.exe'))
            }
        }
    }
    foreach ($p in $paths) {
        if (Test-PythonExe $p) {
            return (& $p -c 'import sys;print(sys.executable)' | Select-Object -Last 1).Trim()
        }
    }
    return $null
}
$PY = Find-Python
if ($PY) { Ok "Python: $PY" } else { Bad '没找到 Python 3.10+ x64 —— 先装 Python（勾选 Add to PATH）: https://www.python.org/downloads/'; exit 1 }

# ── 1b. 安装包自校验 ────────────────────────────────────────────────────
Step "1b/8 安装包完整性校验"
$zipName = 'netdev-windows-x64-installer.zip'
$zipPath = Join-Path (Split-Path -Parent $HERE) $zipName
$shaPath = "$zipPath.sha256"
if ((Test-Path $zipPath) -and (Test-Path $shaPath)) {
    $expected = (Get-Content $shaPath -Raw).Trim().Split(' ')[0]
    $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($expected -eq $actual) {
        Ok "安装包校验通过（SHA-256 匹配）"
    } else {
        Warn "安装包校验失败：SHA-256 不匹配"
        Write-Host "    期望: $expected" -ForegroundColor DarkYellow
        Write-Host "    实际: $actual" -ForegroundColor DarkYellow
        Warn "文件可能损坏或被篡改，建议重新下载后再安装"
    }
} else {
    Warn "未找到安装包 zip 或 .sha256 文件，跳过自校验（直接从解压目录安装属正常）"
}

# ── 2. 复制程序 ─────────────────────────────────────────────────────────
Step "2/8 安装程序到 $Prefix"
$isUpgrade = $false
if ((Test-Path $Prefix) -and (-not (Test-Path (Join-Path $Prefix 'netdev_cli.py'))) -and (Get-ChildItem $Prefix -Force | Select-Object -First 1)) {
    Bad "$Prefix 已存在且不是 netdev 目录 —— 换 -Prefix 或先移走"; exit 1
}
if (Test-Path (Join-Path $Prefix 'backups')) {
    $isUpgrade = $true
    Ok '检测到已有安装：只更新程序，保留 config/ 与 backups/'
}
Do-Step -Desc "复制 payload\netops → $Prefix" -Block {
    # 建目标目录（带重试）。
    # ★ 实测坑：用户「解压完立刻运行安装器」时，Windows Defender / 第三方杀软
    #   正在实时扫描这批刚落地的新文件，会短暂拒绝其子进程的写入，
    #   表现为 UnauthorizedAccessException（错误里路径还可能显示成相对形式）。
    #   等扫描完成即恢复。这里退避重试 4 次，把这个瞬时窗口自动抹平。
    $madeDir = $false
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            New-Item -ItemType Directory -Force $Prefix -ErrorAction Stop | Out-Null
            $madeDir = $true; break
        } catch {
            if ($attempt -lt 4) {
                Start-Sleep -Milliseconds (600 * $attempt)
            } else {
                Bad "无法创建安装目录：$Prefix"
                Write-Host "    底层报错：$($_.Exception.Message)" -ForegroundColor DarkYellow
                Write-Host "    请依次排查：" -ForegroundColor DarkYellow
                Write-Host "      1) 杀毒软件正在实时扫描刚解压的文件（最常见）—— 等 10 秒后重跑安装器即可" -ForegroundColor DarkYellow
                Write-Host "      2) 该位置不可写 —— 换个目录：install.ps1 -Prefix D:\netops" -ForegroundColor DarkYellow
                Write-Host "      3) 请直接用 PowerShell 运行 install.ps1，不要经由其它程序间接启动" -ForegroundColor DarkYellow
                exit 1
            }
        }
    }
    Copy-Item -Path (Join-Path $HERE 'payload\netops\*') -Destination $Prefix -Recurse -Force
    Copy-Item (Join-Path $HERE 'VERSION') (Join-Path $Prefix 'VERSION') -Force
    Copy-Item (Join-Path $HERE 'uninstall.ps1') (Join-Path $Prefix 'uninstall.ps1') -Force
}
Ok '程序文件已就位'

# ── 2b. 图形程序与体检脚本（启动器 / 一键修复）─────────────────────────
Step "2b/8 安装图形启动器与一键修复"
Do-Step -Desc '复制 netdev-toolbox.exe / netdev.ico / 一键体检.ps1 / 一键体检.cmd' -Block {
    foreach ($f in @('netdev-toolbox.exe', 'netdev.ico', '一键体检.ps1', '一键体检.cmd')) {
        $src = Join-Path $HERE $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $Prefix $f) -Force }
    }
    if (Test-Path (Join-Path $Prefix 'netdev-toolbox.exe')) {
        Ok '工具台启动器已就位（开始菜单/桌面快捷方式会在第 7 步建）'
    } else {
        Warn '未随包提供 netdev-toolbox.exe —— 仍可用命令行 netdev ui 管理服务'
    }
    if (Test-Path (Join-Path $Prefix '一键体检.ps1')) { Ok '一键体检/一键修复已就位' }
}

# ── 3. venv + 依赖 ───────────────────────────────────────────────────────
Step "3/8 建 Python 虚拟环境并装依赖"
$venvPy = Join-Path $Prefix '.venv\Scripts\python.exe'
Do-Step -Desc "$PY -m venv $Prefix\.venv；pip install -r requirements.txt" -Block {
    $ErrorActionPreference = 'Continue'
    if (-not (Test-Path $venvPy)) {
        & $PY -m venv (Join-Path $Prefix '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'venv 创建失败' }
    }
    & $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $venvPy -m pip install -r (Join-Path $Prefix 'requirements.txt') --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Warn '依赖安装有失败项（可能需要联网）；可稍后手动: .venv\Scripts\pip install -r requirements.txt'
    }
    # 导入验证
    & $venvPy -c 'import netmiko,serial,winpty,keyring'
    if ($LASTEXITCODE -eq 0) { Ok '依赖导入验证通过' } else { Warn '核心依赖未全部就绪' }
}

# ── 4. 配置（从模板生成，只补缺的）────────────────────────────────────
Step "4/8 收编配置到 $Prefix\config"
Do-Step -Desc '生成 config\*（devices.toml / connections.json / AGENTS 等，已有的保留）' -Block {
    $cfg = Join-Path $Prefix 'config'
    New-Item -ItemType Directory -Force $cfg | Out-Null
    function Fill-Config($SrcName, $DstName, [hashtable]$Repl) {
        $dst = Join-Path $cfg $DstName
        if (Test-Path $dst) { return }
        # 升级收编：≤1.0.9 把 devices.toml / connections.json 写在安装根（当时根目录是活文件），
        # 真身 config/ 还没建 → 先把根目录那份收进来，别用模板把它盖掉。
        $fromRoot = Join-Path $Prefix $DstName
        if (Test-Path $fromRoot) { Copy-Item $fromRoot $dst -Force; return }
        $t = Get-Content (Join-Path (Join-Path $HERE 'config-template') $SrcName) -Raw -Encoding UTF8
        foreach ($k in $Repl.Keys) { $t = $t.Replace($k, $Repl[$k]) }
        # ★ 无 BOM UTF-8：PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM，tomllib 解析直接报错
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($dst, $t, $utf8NoBom)
    }
    $repl = @{ '__HOME__' = $env:USERPROFILE; '__PREFIX__' = $Prefix }
    Fill-Config 'devices.toml.example' 'devices.toml' $repl
    Fill-Config 'connections.json'      'connections.json' $repl
    Fill-Config 'pi-commands.json'     'pi-commands.json' $repl
    Fill-Config 'AGENTS.workspace.md'  'AGENTS.workspace.md' $repl
    Fill-Config '_netdev'              '_netdev' $repl
    Fill-Config 'netdev.bash'           'netdev.bash' $repl
    # 根目录配置入口（Win 直接放副本，不用软链）。
    # ★ config/ 是唯一真身（程序读写都走它）；根目录那份只是镜像。
    #   升级保护：若根目录那份比 config/ 新（≤1.0.9 时它是活文件、用户往里加过设备），
    #   先搬回 config/ 再镜像 —— 否则一次重装就把用户的设备清单抹了。
    foreach ($f in @('devices.toml','connections.json')) {
        $rootF = Join-Path $Prefix $f
        $cfgF  = Join-Path $cfg $f
        if ((Test-Path $rootF) -and (Test-Path $cfgF) -and
            ((Get-Item $rootF).LastWriteTime -gt (Get-Item $cfgF).LastWriteTime)) {
            Copy-Item $rootF $cfgF -Force
            Ok "收编较新的 $f（安装根 → config 真身）"
        }
        Copy-Item $cfgF $rootF -Force
    }
    $state = Join-Path $Prefix 'state'
    New-Item -ItemType Directory -Force $state | Out-Null
    # doctor「配置真身」要求 config/state 存在（Win 不用软链，两个实体目录并存）
    New-Item -ItemType Directory -Force (Join-Path $cfg 'state') | Out-Null
}
Ok '配置就绪'

# ── 5. 命令入口 netdev.cmd / netdev-mcp.cmd ───────────────────────────
Step "5/8 建命令入口"
Do-Step -Desc '写 netdev.cmd、netdev-mcp.cmd，并把安装目录加进用户 PATH' -Block {
    Set-Content -Path (Join-Path $Prefix 'netdev.cmd') -Encoding ASCII -Value @(
        '@echo off',
        'rem Force UTF-8 mode (output/logs/JSON contain non-ASCII text).',
        'set "PYTHONUTF8=1"',
        '"%~dp0.venv\Scripts\python.exe" "%~dp0netdev_cli.py" %*'
    )
    # MCP 入口（payload 里那份 netdev-mcp.cmd 已随代码复制，确保路径正确）
    Set-Content -Path (Join-Path $Prefix 'netdev-mcp.cmd') -Encoding ASCII -Value @(
        '@echo off',
        'rem Force UTF-8 mode (MCP JSON-RPC payloads contain non-ASCII text).',
        'set "PYTHONUTF8=1"',
        '"%~dp0.venv\Scripts\python.exe" "%~dp0netdev_mcp.py" %*'
    )
    $userPath = [Environment]::GetEnvironmentVariable('Path','User')
    if (-not ($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $Prefix })) {
        $newPath = (($userPath.TrimEnd(';') + ';' + $Prefix).Trim(';'))
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Ok "已加入用户 PATH（新终端生效）"
    }
}
Ok 'netdev / netdev-mcp 入口就绪'

# ── 6. 终端页命令（web repair，保证 doctor 不缺项）──────────────────
Step "6/8 终端页命令清单"
Do-Step -Desc 'netdev web repair' -Block {
    $ErrorActionPreference = 'Continue'
    & (Join-Path $Prefix 'netdev.cmd') web repair | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok '终端页命令已生成' } else { Warn "web repair 返回 $LASTEXITCODE（doctor 可能有缺项）" }
}

# ── 7. 卸载入口 + 开机自启 + 启动 UI ───────────────────────────────────
Step "7/8 网页服务 / 卸载入口 / 开机自启"
if (-not $NoStartUi) {
    # ★ 升级时用 restart 而不是 ensure：服务进程是「启动那一刻」把 ui/server.py
    #   读进内存的，光换磁盘上的文件它还在跑旧逻辑。ensure 见服务活着就直接返回，
    #   于是「装完还是旧行为」—— 实测踩过，白排查一轮。升级必须重启。
    $uiAction = if ($isUpgrade) { 'restart' } else { 'ensure' }
    Do-Step -Desc "启动 netdev-ui（后台，$uiAction）" -Block {
        $ErrorActionPreference = 'Continue'
        if ($UiPort) { $env:NETDEV_UI_PORT = $UiPort }
        & (Join-Path $Prefix 'netdev.cmd') ui $uiAction | Out-Null
        if ($isUpgrade) { Ok 'netdev-ui 已重启（加载新代码）' } else { Ok 'netdev-ui 已在后台运行' }
    }
}

# ★ 注册「设置 → 应用」/「控制面板 → 程序和功能」里的卸载入口。
#   为什么以前看不到 netdev 的卸载项：安装器只写了用户 PATH 和启动目录，
#   从没写过 Uninstall 注册表项 —— 那张列表是照注册表列出来的，没写就没有。
#   本机是免管理员安装（写不了 HKLM），所以写 HKCU；Win10/11 的「应用和功能」
#   会按用户列出 HKCU 项。uninstall.ps1 会自行清除，不留残影。
Do-Step -Desc '注册卸载入口（HKCU Uninstall\netdev）' -Block {
    $ver = (Get-Content (Join-Path $HERE 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $ver) { $ver = '0.0.0' }
    $unins = Join-Path $Prefix 'uninstall.ps1'
    $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall\netdev')
    $k.SetValue('DisplayName',     'netdev 网络设备工具台')
    $k.SetValue('DisplayVersion',  $ver)
    $k.SetValue('Publisher',       'netdev')
    $k.SetValue('InstallLocation', $Prefix)
    $iconSrc = Join-Path $Prefix 'netdev-toolbox.exe'
    if (-not (Test-Path $iconSrc)) { $iconSrc = Join-Path $Prefix '.venv\Scripts\python.exe' }
    $k.SetValue('DisplayIcon',     $iconSrc)
    $k.SetValue('InstallDate',     (Get-Date -Format 'yyyyMMdd'))
    $k.SetValue('NoModify', 1, 'DWord')
    $k.SetValue('NoRepair', 1, 'DWord')
    $k.SetValue('UninstallString',      "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$unins`"")
    $k.SetValue('QuietUninstallString', "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$unins`" -Prefix `"$Prefix`"")
    try {
        $sz = (Get-ChildItem $Prefix -Recurse -File -Force -ErrorAction SilentlyContinue |
               Measure-Object -Property Length -Sum).Sum
        if ($sz -gt 0) { $k.SetValue('EstimatedSize', [int][Math]::Round($sz / 1KB), 'DWord') }
    } catch {}
    $k.Close()
    Ok '卸载入口已注册（设置 → 应用 里可见）'
}

# ★ 开始菜单 / 桌面快捷方式：让普通用户「像用正常 exe 应用一样」找到 netdev，
#   不用记 netdev.cmd、不用开终端。快捷方式指向 netdev-toolbox.exe（无控制台 GUI）。
Do-Step -Desc '建开始菜单 / 桌面快捷方式（netdev 工具台）' -Block {
    $toolbox = Join-Path $Prefix 'netdev-toolbox.exe'
    if (Test-Path $toolbox) {
        $ws = New-Object -ComObject WScript.Shell
        $group = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\netdev 网络设备工具台'
        New-Item -ItemType Directory -Force $group | Out-Null

        function New-NetdevLnk([string]$Path, [string]$Target, [string]$Desc, [string]$Icon) {
            $sc = $ws.CreateShortcut($Path)
            $sc.TargetPath = $Target
            $sc.WorkingDirectory = $Prefix
            if ($Icon) { $sc.IconLocation = $Icon }
            $sc.Description = $Desc
            $sc.Save()
        }
        # 开始菜单：工具台 + 一键修复
        New-NetdevLnk (Join-Path $group 'netdev 工具台.lnk') $toolbox 'netdev 网络设备工具台' ($toolbox + ',0')
        # 「一键修复」= 一键体检.cmd -Fix。为什么用 .cmd 的 -Fix 而不是新建一个脚本：
        #   一键体检.cmd 的内容是纯 ASCII（用 %~n0.ps1 定位同名脚本），cmd.exe 用
        #   OEM 代码页解析 .cmd，中文文件名只能靠 %~n0 这种「运行时展开」拿到，
        #   写死中文路径会在部分代码页下乱码。所以复用它、只传 -Fix 最稳。
        $fixCmd = Join-Path $Prefix '一键体检.cmd'
        if (Test-Path $fixCmd) {
            $scf = $ws.CreateShortcut((Join-Path $group '一键修复.lnk'))
            $scf.TargetPath = $fixCmd
            $scf.Arguments = '-Fix'
            $scf.WorkingDirectory = $Prefix
            $scf.IconLocation = ($toolbox + ',0')
            $scf.Description = '体检并自动修复常见故障'
            $scf.Save()
        }
        # 桌面：工具台
        $desktop = [Environment]::GetFolderPath('Desktop')
        if ($desktop) {
            New-NetdevLnk (Join-Path $desktop 'netdev 工具台.lnk') $toolbox 'netdev 网络设备工具台' ($toolbox + ',0')
        }
        Ok '快捷方式已建（开始菜单分组 + 桌面）'
    } else {
        Warn '未找到 netdev-toolbox.exe，跳过快捷方式（可用命令行 netdev ui 管理服务）'
    }
}

if (-not $NoAutoStart) {
    Do-Step -Desc '建开机自启项（pythonw 隐藏启动，不弹控制台）' -Block {
        $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
        New-Item -ItemType Directory -Force $startup | Out-Null
        $lnk = Join-Path $startup 'netdev-ui.lnk'
        # 旧版指向 netdev.cmd —— 每次登录必闪一个黑色控制台窗口。先删掉重建。
        if (Test-Path $lnk) { Remove-Item $lnk -Force }
        # 用 pythonw.exe：无控制台窗口，拉起服务的能力与 python.exe 完全一致
        # （真正的服务进程仍由 daemonize 以 DETACHED_PROCESS + CREATE_NO_WINDOW 起）。
        $pyw = Join-Path $Prefix '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path $pyw)) { $pyw = Join-Path $Prefix '.venv\Scripts\python.exe' }
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $pyw
        $sc.Arguments = '"' + (Join-Path $Prefix 'netdev_cli.py') + '" ui'
        $sc.WorkingDirectory = $Prefix
        $sc.WindowStyle = 7
        $sc.Description = 'netdev 网络设备工具台 · 登录后隐藏启动'
        $sc.Save()
        Ok '开机自启项已建（pythonw 隐藏启动；卸载会移除）'
    }
}

# ── 8. 自检 ────────────────────────────────────────────────────────────
Step "8/8 doctor 自检"
if (-not $DryRun) {
    $ErrorActionPreference = 'Continue'
    if ($UiPort) { $env:NETDEV_UI_PORT = $UiPort }
    & (Join-Path $Prefix 'netdev.cmd') doctor
    Copy-Item (Join-Path $HERE 'README-Windows安装说明.txt') (Join-Path $Prefix 'README-从这里开始.txt') -Force
}

# ── 收尾：复查网页服务是否还在监听 ──────────────────────────────────────
# 实测反馈：装完立刻打开浏览器可能看到「127.0.0.1 拒绝连接」——服务起过但没留住。
# 这里做一次只读复查，把「服务没在跑」明确讲清楚，并给出拉起命令，避免用户误判成安装失败。
$uiPortFinal = if ($UiPort) { $UiPort } else { 8898 }
if (-not $DryRun) {
    $uiUp = $false
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect('127.0.0.1', $uiPortFinal)
        $uiUp = $tcp.Connected
        $tcp.Close()
    } catch { }
    if ($uiUp) {
        Ok "网页服务在跑（127.0.0.1:$uiPortFinal）"
    } else {
        Warn "网页服务当前没在监听 $uiPortFinal —— 不影响安装结果，随时可手动拉起"
        Write-Host "    启动：netdev ui        查看状态：netdev ui status        停止：netdev ui stop" -ForegroundColor DarkYellow
        Write-Host "    已建开机自启项，下次登录会自动启动" -ForegroundColor DarkYellow
    }
}

Write-Host "`n装好了。新开一个终端即可直接用 netdev" -ForegroundColor Green
Write-Host "  网页界面： http://127.0.0.1:$uiPortFinal" -ForegroundColor Green
Write-Host "  若打不开，先跑一次： netdev ui   （netdev ui status 看状态）" -ForegroundColor DarkGray
Write-Host "卸载： powershell -ExecutionPolicy Bypass -File `"$Prefix\uninstall.ps1`"" -ForegroundColor DarkGray
