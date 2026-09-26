# 交付物验收（Inno Setup 版安装程序）
#
# 这不是"跑一遍完整测试套件"——按任务书「职责范围」，打包环节不做验证执行。
# 本脚本只核对**安装程序本身**是否合格，而且**真的装一遍、真的卸一遍**：
#   1. 两份安装程序的 SHA256 与 SHA256SUMS.txt 一致
#   2. 安装数据仍追加在 PE 映像之后（防"被 rcedit 削成空壳"）
#   3. 版本资源（Inno 能设置的 7 个字段）与图标
#   4. 不含模型版：**不给 /DIR 直接静默安装** → 默认装到安装程序所在目录，
#      当前目录出现 app\ 等结构，且确无 .gguf
#   5. 含模型版：同样默认装到所在目录，模型在位且大小一致
#   6. 快捷方式指向 {app}\app\EchoDesk.exe，工作目录 = {app}（外层）
#   7. 安装后 %TEMP% 下无 EchoDesk 相关残留
#   8. install-complete.bat（给 zip 免安装用户的脚本）真跑一次能建出快捷方式
#   9. 卸载程序能把这些文件与快捷方式都清掉
#
# 为什么第 4/5 项一定要"不给 /DIR"：虚拟机实测的教训是"用户双击后全盘找不到
# EchoDesk 文件"。默认安装位置是本方案的核心行为，必须按用户真实路径验证，
# 而不是用 /DIR 指定一个我们觉得对的目录 —— 那等于把要验的东西假设成对的。
#
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_installer_deliverables.ps1

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Version = "0.2.1"
$Publish = Join-Path $Root "publish"
$SetupWith = Join-Path $Publish "EchoDesk-v$Version-Setup.exe"
$SetupNo = Join-Path $Publish "EchoDesk-v$Version-NoModel-Setup.exe"
$Sums = Join-Path $Publish "SHA256SUMS.txt"
$ReleaseDir = Join-Path $Publish "EchoDesk-v$Version"

# 测试目录放在 artifacts\ 下（不入库），**不能**放 %TEMP%：
# 第 7 项要断言"%TEMP% 里没有 EchoDesk 残留"，把安装目标放进 %TEMP% 就自相矛盾了。
$TestRoot = Join-Path $Root "artifacts\installer-test"
Remove-Item $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null

$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\EchoDesk"
$DesktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "EchoDesk.lnk"

$results = New-Object System.Collections.ArrayList
function Check([string]$Name, [scriptblock]$Body) {
    try {
        $detail = & $Body
        [void]$results.Add([pscustomobject]@{ Name = $Name; Ok = $true; Detail = "$detail" })
        Write-Host "[OK]   $Name — $detail" -ForegroundColor Green
    }
    catch {
        [void]$results.Add([pscustomobject]@{ Name = $Name; Ok = $false; Detail = $_.Exception.Message })
        Write-Host "[FAIL] $Name — $($_.Exception.Message)" -ForegroundColor Red
    }
}

# PE 解析：追加数据在不在（Inno 把安装数据追加在 PE 映像之后）
function Get-AppendedBytes([string]$Path) {
    $len = (Get-Item -LiteralPath $Path).Length
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $br = New-Object System.IO.BinaryReader($fs)
        $fs.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
        $e = $br.ReadInt32()
        $fs.Seek($e + 6, [System.IO.SeekOrigin]::Begin) | Out-Null
        $numSec = $br.ReadUInt16()
        $fs.Seek($e + 20, [System.IO.SeekOrigin]::Begin) | Out-Null
        $optSize = $br.ReadUInt16()
        $fs.Seek($e + 24 + $optSize, [System.IO.SeekOrigin]::Begin) | Out-Null
        $maxEnd = [int64]0
        for ($i = 0; $i -lt $numSec; $i++) {
            $null = $br.ReadBytes(8); $null = $br.ReadUInt32(); $null = $br.ReadUInt32()
            $rawSize = $br.ReadUInt32(); $rawPtr = $br.ReadUInt32(); $null = $br.ReadBytes(16)
            $end = [int64]$rawPtr + [int64]$rawSize
            if ($end -gt $maxEnd) { $maxEnd = $end }
        }
        return ($len - $maxEnd)
    }
    finally { $fs.Dispose() }
}

<#
    版本号文本归一化：只用于**比较**，不改变实际值。
    Inno Setup 编译时会把版权串里的 `(C)` 写成 `©`（实测：写
    `Copyright (C) Void Entropia Studio`，读回来是 `Copyright © Void Entropia Studio`），
    所以比对前要归一化，否则断言会因这个已知的等价改写而误报失败。
#>
function ConvertTo-Comparable([string]$Text) {
    return (("$Text").Trim() -replace '\(C\)', '©')
}

function Get-TempEchoDeskResidue {
    <#
        返回 %TEMP% 下与 EchoDesk 有关的残留项。
        Inno 安装期间会在 %TEMP% 建一个受保护的临时目录（is-XXXX.tmp）并在结束后
        自行清理，所以正常情况下这里应该是空的。
    #>
    $hits = @()
    foreach ($d in @(Get-ChildItem $env:TEMP -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "is-*" -or $_.Name -like "7z*" })) {
        $inner = @(Get-ChildItem $d.FullName -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'EchoDesk|EchoBackend' })
        if ($inner.Count -gt 0) { $hits += $d.FullName }
    }
    return $hits
}

function Invoke-SilentInstall([string]$Exe, [string]$LogPath) {
    # 刻意**不传 /DIR**：要验证的正是"默认装到安装程序自己所在的目录"。
    # /MERGETASKS="!desktopicon" 关掉桌面图标：自动化验收不该在用户桌面上留东西，
    # 开始菜单快捷方式足以验证 [Icons] 段。
    $args = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MERGETASKS=`"!desktopicon`"")
    if ($LogPath) { $args += "/LOG=`"$LogPath`"" }
    $p = Start-Process -FilePath $Exe -ArgumentList $args -PassThru -Wait
    return $p.ExitCode
}

function Invoke-SilentUninstall([string]$Dir) {
    $unins = Join-Path $Dir "unins000.exe"
    if (-not (Test-Path $unins)) { return -1 }
    $p = Start-Process -FilePath $unins -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -PassThru -Wait
    Start-Sleep -Seconds 3
    return $p.ExitCode
}

# 验收开始前先清掉可能存在的旧安装（同一个 AppId，避免上一次的残留干扰）
foreach ($d in @(Get-ChildItem $TestRoot -Directory -ErrorAction SilentlyContinue)) {
    [void](Invoke-SilentUninstall $d.FullName)
}
Remove-Item $StartMenuDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $DesktopLnk -Force -ErrorAction SilentlyContinue

# ---------- 1. SHA256 与清单一致 ----------
Check "SHA256 与 SHA256SUMS.txt 一致" {
    if (-not (Test-Path $Sums)) { throw "缺少 SHA256SUMS.txt" }
    $n = 0; $bad = @()
    foreach ($line in Get-Content -LiteralPath $Sums -Encoding UTF8) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '\s+', 2
        $want = $parts[0].Trim().ToLower(); $name = $parts[1].Trim()
        $cand = @((Join-Path $Publish $name), (Join-Path $Root $name),
                  (Join-Path $Root "docs\$name")) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $cand) { throw "清单里的资产找不到: $name" }
        $got = (Get-FileHash -LiteralPath $cand -Algorithm SHA256).Hash.ToLower()
        $n++
        if ($got -ne $want) { $bad += $name }
    }
    if ($bad.Count) { throw "哈希不一致: $($bad -join ', ')" }
    "$n 个资产全部一致"
}

# ---------- 2. 安装数据仍在（防 rcedit 削壳） ----------
Check "安装程序含追加的安装数据（PE 未被重建）" {
    $out = @()
    foreach ($exe in @($SetupWith, $SetupNo)) {
        if (-not (Test-Path $exe)) { throw "缺少 $(Split-Path -Leaf $exe)" }
        $ap = Get-AppendedBytes $exe
        if ($ap -le 0) {
            throw ("$(Split-Path -Leaf $exe) 的追加数据为 0 —— PE 被重建过，安装包是空壳。" +
                "最常见原因是对最终 Setup.exe 跑了 rcedit（会丢弃 PE 映像之后的数据）。")
        }
        if ($ap -lt 1MB) { throw "$(Split-Path -Leaf $exe) 的追加数据只有 $([math]::Round($ap / 1KB, 1)) KB，明显不完整" }
        $out += "$(Split-Path -Leaf $exe) $([math]::Round($ap / 1MB, 1)) MB"
    }
    $out -join ' · '
}

# ---------- 3. 版本资源与图标 ----------
Check "安装程序版本资源与图标" {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    $variants = @(
        @{ Exe = $SetupWith; Desc = "EchoDesk v$Version Portable Setup" },
        @{ Exe = $SetupNo; Desc = "EchoDesk v$Version Portable Setup (No Model)" }
    )
    $fields = 0
    foreach ($v in $variants) {
        $vi = (Get-Item $v.Exe).VersionInfo
        $expect = @{
            CompanyName       = "Void Entropia Studio"
            LegalCopyright    = "Copyright (C) Void Entropia Studio"
            ProductName       = "EchoDesk"
            # 用 Raw（数值口径）：Inno 把 FileVersion 的字符串末尾 ".0" 省掉，Raw 才是真值
            FileVersionRaw    = "$Version.0"
            ProductVersionRaw = "$Version.0"
            FileDescription   = $v.Desc
            OriginalFilename  = (Split-Path -Leaf $v.Exe)
        }
        foreach ($k in $expect.Keys) {
            if ((ConvertTo-Comparable $vi.$k) -ne (ConvertTo-Comparable $expect[$k])) {
                throw "$(Split-Path -Leaf $v.Exe) 的 $k='$($vi.$k)' 期望 '$($expect[$k])'"
            }
            $fields++
        }
        $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($v.Exe)
        if ($null -eq $ico) { throw "$(Split-Path -Leaf $v.Exe) 取不到图标" }
        $ico.Dispose()
    }
    "两份包共 $fields 个字段全部正确，图标均可从 PE 取出"
}

# ---------- 4/5/6/7/9. 真装、真验、真卸 ----------
<#
    顺序很重要：两个变体**共用同一个 AppId**（同一个产品的两种载荷），
    所以必须"装一个 → 验一个 → 卸一个"，不能同时装着两个。
    每一轮都不传 /DIR，验证默认目录行为。
#>
function Test-InstallerRound([string]$Exe, [string]$Label, [bool]$ExpectModels) {
    $caseDir = Join-Path $TestRoot $Label
    New-Item -ItemType Directory -Path $caseDir -Force | Out-Null
    # 把安装程序放到一个"干净的空目录"里，模拟用户把 Setup.exe 丢进一个文件夹后双击
    $localExe = Join-Path $caseDir (Split-Path -Leaf $Exe)
    Copy-Item -LiteralPath $Exe -Destination $localExe -Force

    # 安装日志写到测试根目录下的 logs\，**不要**写进安装目录：
    # 安装目录必须保持"除了安装程序本身什么都没有"，否则"卸载后目录里不该有多余文件"
    # 这条断言会被我们自己的日志绊倒（实测踩到：断言报 "仍有多余文件: setup.log"）。
    $logDir = Join-Path $TestRoot "logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $log = Join-Path $logDir "$Label-setup.log"
    $tempBefore = @(Get-TempEchoDeskResidue)
    $code = Invoke-SilentInstall $localExe $log
    if ($code -ne 0) {
        throw ("静默安装失败（退出码 $code）。日志尾部：`n" +
            (($log | ForEach-Object { Get-Content -LiteralPath $_ -ErrorAction SilentlyContinue } |
                Select-Object -Last 10) -join "`n"))
    }

    # 4/5-a：默认装到"安装程序所在目录"
    $missing = @()
    foreach ($rel in @("app\EchoDesk.exe", "app\EchoDesk.ico", "app\backend\EchoDeskBackend.exe",
            "app\backend\_internal", "docs\SBOM.json", "docs\ENCRYPTED_DOCUMENTS.md",
            "docs\ENCRYPTED_DOCUMENTS.zh-Hans.md", "lang", "examples\skills", "results",
            "install-complete.bat", "USAGE.txt", "README.md", "CHANGELOG.md",
            "LICENSE", "LICENSE.zh-Hans.md", "SECURITY.zh-Hans.md",
            "config.example.json", "docs\PRIVACY.zh-Hans.md",
            "docs\THIRD_PARTY_LICENSES.zh-Hans.md")) {
        if (-not (Test-Path -LiteralPath (Join-Path $caseDir $rel))) { $missing += $rel }
    }
    if ($missing.Count -gt 0) {
        throw "默认安装目录（安装程序所在目录）里缺少：$($missing -join ', ')"
    }

    # 4/5-b：模型是否按预期在位
    $modelSrc = @(Get-ChildItem (Join-Path $ReleaseDir "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
    $modelDst = @(Get-ChildItem (Join-Path $caseDir "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
    if ($ExpectModels) {
        if ($modelDst.Count -eq 0) { throw "含模型版安装后没有 .gguf" }
        foreach ($m in $modelSrc) {
            $d = Join-Path $caseDir "app\backend\models\$($m.Name)"
            if (-not (Test-Path $d)) { throw "缺少模型 $($m.Name)" }
            if ((Get-Item $d).Length -ne $m.Length) { throw "模型 $($m.Name) 大小不一致" }
        }
    }
    else {
        if ($modelDst.Count -gt 0) { throw "不含模型版里出现了 $($modelDst.Count) 个 .gguf" }
    }

    # 6：快捷方式（开始菜单）目标与工作目录
    $lnk = Join-Path $StartMenuDir "EchoDesk.lnk"
    if (-not (Test-Path $lnk)) { throw "开始菜单里没有创建 EchoDesk 快捷方式（$StartMenuDir）" }
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($lnk)
    $wantTarget = Join-Path $caseDir "app\EchoDesk.exe"
    if ($sc.TargetPath -ne $wantTarget) { throw "快捷方式目标不对: $($sc.TargetPath) 期望 $wantTarget" }
    if ($sc.WorkingDirectory.TrimEnd('\') -ne $caseDir.TrimEnd('\')) {
        throw "快捷方式工作目录不对: $($sc.WorkingDirectory) 期望 $caseDir（外层，保证 results\ 与用户数据生成在外层）"
    }
    if ($sc.IconLocation -notlike "*EchoDesk.ico*") { throw "快捷方式图标不对: $($sc.IconLocation)" }

    # 7：%TEMP% 无残留
    $tempAfter = @(Get-TempEchoDeskResidue)
    if ($tempAfter.Count -gt 0) { throw "%TEMP% 下有 EchoDesk 残留: $($tempAfter -join ', ')" }

    $summary = "装到 $caseDir；快捷方式 → app\EchoDesk.exe（工作目录 = 安装目录）；%TEMP% 无残留"

    # 9：卸载。**必须在本轮就卸掉**：两个变体共用同一个 AppId（同一产品的两种载荷），
    # 留着不卸会让下一轮变成"覆盖已有安装"，卸载注册项也会指向错的目录，
    # 最终在用户机器上留下孤儿记录。
    $unins = Join-Path $caseDir "unins000.exe"
    if (-not (Test-Path $unins)) { throw "安装目录里没有卸载程序（Uninstallable=no？）：$caseDir" }
    $ucode = Invoke-SilentUninstall $caseDir
    if ($ucode -ne 0) { throw "卸载失败（退出码 $ucode）" }
    foreach ($gone in @("app", "results", "docs")) {
        if (Test-Path (Join-Path $caseDir $gone)) { throw "卸载后 $gone\ 仍在（$caseDir）" }
    }
    if (Test-Path (Join-Path $StartMenuDir "EchoDesk.lnk")) { throw "卸载后开始菜单快捷方式仍在" }
    $leftover = @(Get-ChildItem $caseDir -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "*Setup.exe" } | ForEach-Object { $_.FullName.Substring($caseDir.Length + 1) })
    if ($leftover.Count -gt 0) { throw "卸载后目录里仍有多余文件: $($leftover -join ', ')" }

    return [pscustomobject]@{
        Dir        = $caseDir
        Models     = $modelDst.Count
        Target     = $sc.TargetPath
        WorkingDir = $sc.WorkingDirectory
        Summary    = $summary
    }
}

$roundNo = $null
$roundWith = $null

Check "不含模型版：默认装到安装程序所在目录、结构齐全、无 .gguf、快捷方式正确、可卸载" {
    $r = Test-InstallerRound $SetupNo "nomodel" $false
    $r.Summary
}

Check "含模型版：默认装到安装程序所在目录、模型在位且大小一致、可卸载" {
    $r = Test-InstallerRound $SetupWith "withmodel" $true
    "$($r.Summary)；模型 $($r.Models) 个且大小一致"
}

# ---------- 8. install-complete.bat（zip 免安装用户）真跑一次 ----------
Check "install-complete.bat 实测建出便携快捷方式" {
    $batDir = Join-Path $TestRoot "bat-only"
    New-Item -ItemType Directory -Path (Join-Path $batDir "app") -Force | Out-Null
    Copy-Item (Join-Path $ReleaseDir "install-complete.bat") $batDir -Force
    foreach ($f in @("app\EchoDesk.exe", "app\EchoDesk.ico")) {
        $src = Join-Path $ReleaseDir $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $batDir $f) -Force }
    }
    $bat = Join-Path $batDir "install-complete.bat"
    if (-not (Test-Path $bat)) { throw "缺少 install-complete.bat" }
    if (-not (Test-Path (Join-Path $batDir "app\EchoDesk.exe"))) { throw "缺少 app\EchoDesk.exe，无法验证脚本" }
    $out = & cmd /c "`"$bat`" < nul" 2>&1 | Out-String
    $lnk = Join-Path $batDir "Start-EchoDesk-v$Version.lnk"
    if (-not (Test-Path -LiteralPath $lnk)) {
        throw ("未创建快捷方式。输出尾部：`n" + (($out -split "`n" | Select-Object -Last 8) -join "`n"))
    }
    $sh = New-Object -ComObject WScript.Shell
    $sc = $sh.CreateShortcut($lnk)
    $want = Join-Path $batDir "app\EchoDesk.exe"
    if ($sc.TargetPath -ne $want) { throw "目标不对: $($sc.TargetPath)" }
    if ($sc.WorkingDirectory.TrimEnd('\') -ne $batDir.TrimEnd('\')) { throw "工作目录不对: $($sc.WorkingDirectory)" }
    "已创建 Start-EchoDesk-v$Version.lnk → app\EchoDesk.exe（工作目录=解压目录）"
}

# ---------- 9. 卸载后不留孤儿记录 ----------
Check "卸载后无遗留安装记录与 %TEMP% 残留" {
    # 只认"指向本次测试目录"的记录：如果这台机器上本来就装了正式版 EchoDesk，
    # 不该被这项验收误判。
    $keys = @("HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")
    $left = @()
    foreach ($k in $keys) {
        $left += @(Get-ItemProperty $k -ErrorAction SilentlyContinue |
            Where-Object {
                $_.DisplayName -like "EchoDesk*" -and $_.InstallLocation -and
                $_.InstallLocation -like "*artifacts\installer-test*"
            } | ForEach-Object { "$($_.DisplayName) @ $($_.InstallLocation)" })
    }
    if ($left.Count -gt 0) { throw "卸载后仍留有本次测试的安装记录: $($left -join ', ')" }
    $tempLeft = @(Get-TempEchoDeskResidue)
    if ($tempLeft.Count -gt 0) { throw "%TEMP% 仍有残留: $($tempLeft -join ', ')" }
    "卸载注册项已清除，%TEMP% 无残留"
}

# 收尾：清掉测试目录与可能残留的桌面图标
Remove-Item $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $StartMenuDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $DesktopLnk -Force -ErrorAction SilentlyContinue

# ---------- 汇总 ----------
$failed = @($results | Where-Object { -not $_.Ok })
Write-Host ""
Write-Host "------------------------------------------"
Write-Host "交付物验收：通过 $($results.Count - $failed.Count) 项，失败 $($failed.Count) 项"
if ($failed.Count -gt 0) {
    $failed | ForEach-Object { Write-Host "  - $($_.Name)：$($_.Detail)" -ForegroundColor Red }
    exit 1
}
Write-Host "全部通过。" -ForegroundColor Green
exit 0
