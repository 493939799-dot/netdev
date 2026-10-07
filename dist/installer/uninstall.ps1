# ═══════════════════════════════════════════════════════════════════════════
#  netdev 网络设备工具台 · Windows 卸载
#  用法：
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Prefix D:\netops
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Purge   # 连安装目录一起删
#  默认只做"去注册"：停服务、删自启、摘 PATH、清「应用和功能」入口；config/backups 保留。
# ═══════════════════════════════════════════════════════════════════════════
[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:USERPROFILE 'netops'),
    [switch]$Purge
)
$ErrorActionPreference = 'Stop'

function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Step($m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }

Write-Host "netdev Windows 卸载（$Prefix）" -ForegroundColor White

# 1. 停网页服务与 pane 守护
Step "1/5 停止服务"
$netdevCmd = Join-Path $Prefix 'netdev.cmd'
if (Test-Path $netdevCmd) {
    $ErrorActionPreference = 'Continue'
    & $netdevCmd ui stop 2>$null
    Ok '网页服务已停止'
    $ErrorActionPreference = 'Stop'
}
# 兜底：taskkill 安装目录 venv 里的 python（pane daemon / server 及其子进程）
Start-Sleep -Milliseconds 800
if (Test-Path $Prefix) {
    $ErrorActionPreference = 'Continue'
    foreach ($round in 1..3) {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase) }
        if (-not $procs) { break }
        foreach ($p in $procs) { taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }
        Start-Sleep -Milliseconds 600
    }
    $ErrorActionPreference = 'Stop'
    Ok '相关 Python 进程已收'
}

# 2. 删开机自启
Step "2/6 删除开机自启"
$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\netdev-ui.lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force; Ok '已删 Startup\netdev-ui.lnk' }
try {
    $run = 'Software\Microsoft\Windows\CurrentVersion\Run'
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($run, $true)
    if ($k) {
        $val = $k.GetValue('netdev-ui')
        if ($null -ne $val) { $k.DeleteValue('netdev-ui'); Ok '已删注册表 Run 键 netdev-ui' }
        $k.Close()
    }
} catch { Warn '清理注册表 Run 键失败（可忽略）' }

# 2b. 删开始菜单 / 桌面快捷方式
Step "2b/6 删除快捷方式"
$group = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\netdev 网络设备工具台'
if (Test-Path $group) { Remove-Item $group -Recurse -Force; Ok '已删开始菜单分组' }
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $dl = Join-Path $desktop 'netdev 工具台.lnk'
    if (Test-Path $dl) { Remove-Item $dl -Force; Ok '已删桌面快捷方式' }
}

# 3. 清卸载入口（与 install.ps1 的注册对称，否则卸载后「应用和功能」里会留一个死条目）
Step "3/6 清除「应用和功能」卸载入口"
try {
    $uk = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\netdev'
    $cur = [Microsoft.Win32.Registry]::CurrentUser
    if ($cur.OpenSubKey($uk)) {
        $cur.DeleteSubKeyTree($uk)
        Ok '已清除 HKCU Uninstall\netdev（设置 → 应用 里不再显示）'
    } else {
        Write-Host "  （本来就没注册卸载入口，跳过）" -ForegroundColor DarkGray
    }
} catch { Warn '清除卸载入口失败（可忽略）' }

# 4. 摘 PATH
Step "4/6 从用户 PATH 移除"
$userPath = [Environment]::GetEnvironmentVariable('Path','User')
if ($userPath) {
    $kept = $userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $Prefix }
    [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    Ok 'PATH 已更新（新终端生效）'
}

# 5. 删除目录（可选）
Step "5/6 安装目录"
if ($Purge) {
    if (Test-Path $Prefix) {
        Remove-Item $Prefix -Recurse -Force
        Ok "已删除 $Prefix（含 config/backups）"
    } else { Warn "$Prefix 不存在" }
} else {
    Write-Host "  保留 $Prefix（config 配置与 backups 备份未动）。" -ForegroundColor DarkGray
    Write-Host "  确认要彻底删除：重新跑 uninstall.ps1 -Purge" -ForegroundColor DarkGray
}
Write-Host "`n卸载完成。" -ForegroundColor Green
