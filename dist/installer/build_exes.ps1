# ═══════════════════════════════════════════════════════════════════════════
#  build_exes.ps1 —— 编译 Windows 图形程序（无控制台）
# ───────────────────────────────────────────────────────────────────────────
#  产物：
#    netdev-toolbox.exe   工具台启动器（状态显示 / 打开界面 / 一键修复 / 重启服务）
#    netdev-install.exe   图形安装向导（进度 + 实时日志）
#
#  依赖：Windows 自带的 .NET Framework 编译器 csc.exe，无需装任何东西。
#  图标：同目录 netdev.ico（多尺寸），会嵌进 exe，作为快捷方式/任务栏图标。
#
#  用法：
#    powershell -ExecutionPolicy Bypass -File build_exes.ps1
# ═══════════════════════════════════════════════════════════════════════════
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path $csc)) { throw "找不到 csc.exe（.NET Framework 编译器），无法编译图形程序。" }

$ico = Join-Path $Here 'netdev.ico'
if (-not (Test-Path $ico)) { throw "找不到图标 $ico（快捷方式/exe 图标要用）" }

function Build-Exe([string]$Src, [string]$Out) {
    Write-Host "  编译 $Src → $Out" -ForegroundColor Cyan
    & $csc /nologo /target:winexe /codepage:65001 `
        /reference:System.dll `
        /reference:System.Windows.Forms.dll `
        /reference:System.Drawing.dll `
        /win32icon:$ico `
        /out:$Out $Src
    if ($LASTEXITCODE -ne 0) { throw "编译失败：$Src" }
}

Write-Host "编译 Windows 图形程序…" -ForegroundColor White
Build-Exe (Join-Path $Here 'LauncherStub.cs')  (Join-Path $Here 'netdev-toolbox.exe')
Build-Exe (Join-Path $Here 'InstallerStub.cs') (Join-Path $Here 'netdev-install.exe')

Get-ChildItem (Join-Path $Here 'netdev-toolbox.exe'), (Join-Path $Here 'netdev-install.exe') |
    Select-Object Name, Length | Format-Table -AutoSize
Write-Host "完成。" -ForegroundColor Green
