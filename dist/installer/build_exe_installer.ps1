# ============================================================
#  build_exe_installer.ps1 —— 从 zip 安装包生成 .exe 自解压安装器
# ============================================================
#  ★★ 已废弃（2026-10-05，见 06-接管与完善记录.md 的 OPT-8 / OPT-13）★★
#  本脚本走 IExpress 自解压方案，**此路不通**：IExpress 不支持多层目录结构，
#  且实测产物内嵌 CAB 仅 18 KB、根本装不下 2.5 MB 的 payload —— 生成出来的
#  .exe 是坏的，装不了。netdev 最终采用「zip + exe 启动器」方案
#  （源码 dist/installer/InstallerStub.cs，产物 netdev-install.exe）。
#  本脚本仅作「试过并否决」的记录保留，**请勿把它的产物拿去分发**。
#  另：dist/installer 下的陈旧 zip 已按 OPT-13 清理，要跑本脚本需显式传 -ZipPath。
# ============================================================
# 用法：
#   .\build_exe_installer.ps1
#   .\build_exe_installer.ps1 -ZipPath .\netdev-windows-x64-installer.zip
#
# 依赖：iexpress.exe（Windows 自带，在 system32 下）
# 输出：netdev-windows-x64-installer.exe + .sha256
# ============================================================

[CmdletBinding()]
param(
    [string]$ZipPath = "netdev-windows-x64-installer.zip",
    [string]$OutExe  = "netdev-windows-x64-installer.exe",
    [string]$Version = "1.0.7"
)

$ErrorActionPreference = "Stop"

# 定位脚本所在目录
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

# 检查 zip
if (-not (Test-Path $ZipPath)) {
    throw "找不到安装包 zip：$ZipPath"
}
$ZipPath = Resolve-Path $ZipPath
Write-Host "输入: $ZipPath"

# 临时工作目录
$PayloadDir = Join-Path $Here "_payload_tmp"
if (Test-Path $PayloadDir) {
    Remove-Item $PayloadDir -Recurse -Force
}
New-Item -ItemType Directory -Path $PayloadDir | Out-Null

Write-Host "[1/4] 解压 zip 到临时目录..."
Expand-Archive -Path $ZipPath -DestinationPath $PayloadDir -Force

# 检查 install.ps1 是否存在
$installPs1 = Get-ChildItem $PayloadDir -Filter install.ps1 -Recurse | Select-Object -First 1
if (-not $installPs1) {
    throw "解压后的内容里找不到 install.ps1，zip 结构异常"
}
# 如果 install.ps1 在子目录里，把它提到根（IExpress 只支持单层目录）
$installRoot = $installPs1.DirectoryName
if ($installRoot -ne $PayloadDir) {
    Write-Host "    install.ps1 在子目录中，扁平化处理..."
    # 把子目录内容移到根
    Get-ChildItem $installRoot | Move-Item -Destination $PayloadDir -Force
    # 删除空子目录
    Get-ChildItem $PayloadDir -Directory | Remove-Item -Recurse -Force
}

Write-Host "[2/4] 生成 IExpress 配置文件..."

# 收集所有文件列表
$files = Get-ChildItem $PayloadDir -File -Recurse
$fileListSection = "[SourceFiles0]`r`n"
foreach ($f in $files) {
    # IExpress 格式：文件名=
    $fileListSection += "$($f.Name)=`r`n"
}

$sedContent = @"
[Version]
Class=IEXPRESS
SEDVersion=3

[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles

[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$OutExe
FriendlyName=netdev v$Version Windows Installer
AppLaunched=powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=

[SourceFiles]
SourceFiles0=$PayloadDir\

$fileListSection
"@

$sedPath = Join-Path $Here "_build.sed"
# IExpress 需要 ANSI 编码的 SED 文件
[System.IO.File]::WriteAllText($sedPath, $sedContent, [System.Text.Encoding]::Default)

Write-Host "[3/4] 调用 IExpress 生成 exe..."
$iexpress = Join-Path $env:SystemRoot "system32\iexpress.exe"
if (-not (Test-Path $iexpress)) {
    throw "找不到 iexpress.exe：$iexpress"
}

# /N = 静默模式，直接构建
& $iexpress /N $sedPath
if ($LASTEXITCODE -ne 0) {
    throw "IExpress 构建失败（退出码 $LASTEXITCODE）"
}

if (-not (Test-Path $OutExe)) {
    throw "构建完成但找不到输出文件：$OutExe"
}

$exeSize = (Get-Item $OutExe).Length / 1MB
Write-Host "    exe 大小：$([math]::Round($exeSize, 2)) MB"

Write-Host "[4/4] 生成 SHA-256 校验..."
$hash = (Get-FileHash $OutExe -Algorithm SHA256).Hash.ToLower()
"$hash  $OutExe" | Out-File -Encoding ascii "$OutExe.sha256"
Write-Host "    SHA-256: $hash"

# 清理临时文件
Remove-Item $PayloadDir -Recurse -Force
Remove-Item $sedPath -Force

Write-Host ""
Write-Host "? 构建完成：$OutExe" -ForegroundColor Green
Write-Host "  校验文件：$OutExe.sha256"
