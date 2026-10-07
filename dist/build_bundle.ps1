# ═══════════════════════════════════════════════════════════════════════════
#  构建"netdev 网络设备工具台 · Windows x64 安装包"
#  用法：
#     powershell -ExecutionPolicy Bypass -File build_bundle.ps1 [-Out DIR]
#  产物：
#     <Out>\netdev-windows-x64-installer.zip
#     <Out>\netdev-windows-x64-installer.zip.sha256
#  说明：目标机需自备 Python 3.10+（x64）；install.ps1 会建 venv 并装依赖。
# ═══════════════════════════════════════════════════════════════════════════
[CmdletBinding()]
param(
    [string]$Out = (Join-Path $env:USERPROFILE 'Desktop')
)
$ErrorActionPreference = 'Stop'

$SRC  = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$INST = Join-Path $SRC 'dist\installer'
$VER   = (Get-Content (Join-Path $INST 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $VER) { $VER = '1.0.0' }

function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }

# 每次构建用唯一临时目录（不做 Remove-Item 复用，杜绝"新旧包混合"）
$STAGE = Join-Path $env:TEMP ("netdev-build-{0}" -f ([guid]::NewGuid().ToString('N').Substring(0,8)))
$BUNDLE_NAME = 'netdev-windows-x64-installer'
$root = Join-Path $STAGE $BUNDLE_NAME
$payload = Join-Path $root 'payload\netops'
New-Item -ItemType Directory -Force $payload | Out-Null
New-Item -ItemType Directory -Force $Out | Out-Null

Step "1/5 复制程序代码（不含配置/日志/备份/虚拟环境）"
$excludeDirs = @('.venv','dist','logs','live','backups','config','state','__pycache__',
                  '.git','.github','.workbuddy','docs','node_modules','_tmp')
# ★ 垃圾临时目录（名字带随机后缀，只能按模式挡）：
#   pip 与测试用例在「TEMP/TMP 为空或相对路径 → gettempdir() 落在 CWD」的环境里
#   留下的中间产物。历史事故：26 个 netdev-colorize-* + 3 个 pip-* 被扫进 payload，
#   随安装包装进了用户安装根（见 06-接管与完善记录 OPT-12）。
$excludeDirPatterns = @('netdev-colorize-*','netdev-mon-*','netdev-cache-*',
                        'netdev-rmtree-*','netdev-ui-test-*','pip-*')
$excludeFiles = @('*.pyc','*.pyo','*.bak-*','*.log','tags','.DS_Store','.chk',
                  # ★ 构建产物本身绝不能进包。历史事故：早期 Set-Content 路径 bug
                  #   在源码根留下 5 个「<sha256>  netdev-windows-x64-installer.zip」
                  #   垃圾文件，被 Copy-Tree 扫进 payload，又被 install.ps1 装到
                  #   安装根。这里按名挡掉，Step 1b 再兜底断言一次。
                  '*netdev-windows-x64-installer*.zip','*.zip')
# 根目录下用户配置/连接簿本体不打（安装器会从模板重建）
# requirements-win.txt 只服务源码树开发；安装包内的 requirements.txt 已自含
# Windows 依赖（见 Step 2）、无需再来一份，避免安装根出现两个依赖清单。
$excludeRootFiles = @('devices.toml','connections.json','requirements-win.txt')

# ── 版本号一致性自检：netdev_mcp.py 的 SERVER.version 必须等于 dist/installer/VERSION
#    （版本单一真源是 VERSION；两边不一致 → 打包出来 MCP 自我介绍的版本
#     与安装器声明的版本对不上，属于低级但容易漏的错误）
$mcpPath = Join-Path $SRC 'netdev_mcp.py'
if (Test-Path $mcpPath) {
    $mcpVer = $null
    $m = Select-String -Path $mcpPath -Pattern '"version"\s*:\s*"([^"]+)"' | Select-Object -First 1
    if ($m -and $m.Matches.Success) { $mcpVer = $m.Matches.Groups[1].Value }
    if ($mcpVer -and $mcpVer -ne $VER) {
        throw "版本号不一致：netdev_mcp.py = $mcpVer ，dist/installer/VERSION = $VER 。请以 VERSION 为准同步更新。"
    }
    Ok "版本号一致性：netdev_mcp.py = $VER （与 VERSION 一致）"
}

function Copy-Tree($From, $To) {
    New-Item -ItemType Directory -Force $To | Out-Null
    Get-ChildItem -LiteralPath $From -Force | ForEach-Object {
        $it = $_
        if ($it.PSIsContainer) {
            if ($excludeDirs -contains $it.Name) { return }
            foreach ($pat in $excludeDirPatterns) { if ($it.Name -like $pat) { return } }
            Copy-Tree $it.FullName (Join-Path $To $it.Name)
        } else {
            foreach ($pat in $excludeFiles) { if ($it.Name -like $pat) { return } }
            $relToSrc = $it.FullName.Substring($From.Length).TrimStart('\')
            if (-not $relToSrc.Contains('\')) {
                if ($excludeRootFiles -contains $it.Name) { return }
            }
            Copy-Item -LiteralPath $it.FullName -Destination (Join-Path $To $it.Name) -Force
        }
    }
}
Copy-Tree $SRC $payload
Ok "代码已复制"

Step "1b/5 校验 payload（不让构建产物/压缩包混进安装包）"
$payloadFiles = @(Get-ChildItem -LiteralPath $payload -Recurse -File -Force)
$stray = @($payloadFiles | Where-Object {
    $_.Name -like '*netdev-windows-x64-installer*' -or $_.Extension -ieq '.zip'
})
if ($stray.Count -gt 0) {
    $stray | ForEach-Object { Write-Host "      $($_.Name)" -ForegroundColor Red }
    Remove-Item -LiteralPath $STAGE -Recurse -Force -ErrorAction SilentlyContinue
    throw "payload 内出现构建产物/压缩包（上列 $($stray.Count) 个）—— 已中止构建，请先清理源码根"
}
Ok "payload 干净：$($payloadFiles.Count) 个文件，无压缩包/构建产物"

# 兜底：万一排除模式被绕过（改坏了 Copy-Tree），这里再拦一次垃圾临时目录。
# 让「装到用户机器上的东西」永远不可能是测试/pip 的中间产物。
$payloadDirs = @(Get-ChildItem -LiteralPath $payload -Recurse -Directory -Force)
$junkDirs = @($payloadDirs | Where-Object {
    $n = $_.Name
    @($excludeDirPatterns | Where-Object { $n -like $_ }).Count -gt 0
})
if ($junkDirs.Count -gt 0) {
    $junkDirs | ForEach-Object { Write-Host "      $($_.FullName.Substring($payload.Length).TrimStart('\'))" -ForegroundColor Red }
    Remove-Item -LiteralPath $STAGE -Recurse -Force -ErrorAction SilentlyContinue
    throw "payload 内出现垃圾临时目录（上列 $($junkDirs.Count) 个）—— 已中止构建，请清理源码树后重试"
}
Ok "payload 无垃圾临时目录"

Step "2/5 requirements.txt + 配置模板"
$reqDst = Join-Path $payload 'requirements.txt'
if (Test-Path (Join-Path $SRC 'requirements.txt')) {
    Copy-Item (Join-Path $SRC 'requirements.txt') $reqDst -Force
} else {
    'netmiko','pyserial','scrapli' | Set-Content $reqDst
}
# Windows 专属：源 requirements 是 macOS 版，追加 pywinpty/keyring
# （与源码根 requirements-win.txt 的 Windows 段保持一致）
$reqLines = Get-Content $reqDst
foreach ($must in @('pywinpty','keyring')) {
    # 允许 `pywinpty`、`pywinpty==x`、以及带环境标记的 `pywinpty; sys_platform=="win32"`
    if (-not ($reqLines | Where-Object { $_ -match "^\s*$must([=<>!~;]|$)" })) {
        Add-Content $reqDst ("# Windows-only`n{0}" -f $must)
    }
}
$tplDir = Join-Path $root 'config-template'
New-Item -ItemType Directory -Force $tplDir | Out-Null
$tplMap = @{
    'devices.toml.example'     = 'devices.toml.example'
    'connections.json.example' = 'connections.json'
    'pi-commands.json.example' = 'pi-commands.json'
    'AGENTS.workspace.md.example' = 'AGENTS.workspace.md'
    '_netdev.example'          = '_netdev'
    'netdev.bash.example'      = 'netdev.bash'
}
foreach ($k in $tplMap.Keys) {
    $sf = Join-Path $SRC ("config\{0}" -f $k)
    if (-not (Test-Path $sf)) { throw "缺模板 config\$k" }
    Copy-Item $sf (Join-Path $tplDir $tplMap[$k]) -Force
}
Ok "模板就绪（$($tplMap.Count) 个）"

Step "3/5 图形程序 + 安装器 + 说明书"
# 先编译图形程序（工具台启动器 / 图形安装向导）。csc.exe 是 Windows 自带的。
$buildExes = Join-Path $INST 'build_exes.ps1'
if (Test-Path $buildExes) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $buildExes
    if ($LASTEXITCODE -ne 0) { throw "编译图形程序失败（build_exes.ps1 返回 $LASTEXITCODE）" }
    Ok "图形程序已编译（netdev-toolbox.exe / netdev-install.exe）"
}

Copy-Item (Join-Path $INST 'install.ps1')   $root -Force
Copy-Item (Join-Path $INST 'uninstall.ps1') $root -Force
Copy-Item (Join-Path $INST 'README-Windows安装说明.txt') $root -Force
Set-Content (Join-Path $root 'VERSION') $VER

# exe 安装向导（双击即装，无控制台）
$installExe = Join-Path $INST 'netdev-install.exe'
if (Test-Path $installExe) {
    Copy-Item $installExe $root -Force
    Ok "安装向导就绪（netdev-install.exe）"
} else {
    Warn "未编译出 netdev-install.exe —— 回退用 install.ps1"
}

# 工具台启动器 + 一键修复 + 图标（install.ps1 第 2b 步会把它们装进安装目录）
# 注意：变量名别用 $src —— PowerShell 变量名不区分大小写，会覆盖上面的 $SRC。
foreach ($asset in @('netdev-toolbox.exe', 'netdev.ico', '一键体检.ps1', '一键体检.cmd')) {
    $assetPath = Join-Path $INST $asset
    if (Test-Path $assetPath) {
        Copy-Item $assetPath $root -Force
    } else {
        Warn "缺少 $asset（相关功能在目标机上会不可用）"
    }
}
Ok "工具台启动器 / 一键修复 / 图标已入包"

Step "4/5 打包 zip"
$zipPath = Join-Path $Out "$BUNDLE_NAME.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $STAGE $BUNDLE_NAME) -DestinationPath $zipPath -CompressionLevel Optimal
Ok "$zipPath"

Step "5/5 校验和"
$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash
$sumLine = "{0}  {1}" -f $hash.ToLower(), "$BUNDLE_NAME.zip"
# ★ 必须 $($zipPath)：直接写 "$zipPath.sha256" 会被 PS 当成属性访问求值成空串
$sumPath = "$($zipPath).sha256"
Set-Content -NoNewline -Path $sumPath -Value $sumLine
Ok "校验和文件：$sumPath"
Ok ("{0}..." -f $hash.Substring(0,24))
Set-Content (Join-Path $SRC 'dist\last_bundle.txt') "$BUNDLE_NAME.zip"

# 收尾：清掉本次的暂存目录（尽力而为；失败不影响产物）。
# 不清理的话，每构建一次就往 %TEMP% 留一份完整的中间树。
Remove-Item -LiteralPath $STAGE -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`n完成：把 zip 拷到目标 Windows，解压后执行：powershell -ExecutionPolicy Bypass -File install.ps1" -ForegroundColor White
