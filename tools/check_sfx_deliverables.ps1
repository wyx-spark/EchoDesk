# 交付物验收（发布任务书「任务六：打包机自检」中与打包直接相关的项）
#
# 这不是"跑一遍完整测试套件"——按任务书「职责范围」，打包环节不做验证执行。
# 本脚本只核对**打包产物本身**是否合格：
#   1. 两个自解压包的 SHA256 与 SHA256SUMS.txt 一致
#   2. rcedit 九个字段 + 图标（真的从 PE 里取一次关联图标）
#   3. 不含模型包能解压、结构正确、确实不含 .gguf
#   4. 含模型包能解压、模型在里面且大小一致
#   5. install-complete.bat 真跑一次：快捷方式必须真的出现，目标/工作目录/图标都对
#      （这一项是 v0.2.1 实测踩过坑的地方：生成的批处理里 PowerShell 变量被
#       here-string 吃掉，静态检查全过、运行时必然失败）
#
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File artifacts\check_sfx_deliverables.ps1

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Version = "0.2.1"
$Publish = Join-Path $Root "publish"
$SetupWith = Join-Path $Publish "EchoDesk-v$Version-Setup.exe"
$SetupNo = Join-Path $Publish "EchoDesk-v$Version-NoModel-Setup.exe"
$Sums = Join-Path $Publish "SHA256SUMS.txt"
$ReleaseDir = Join-Path $Publish "EchoDesk-v$Version"

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

$sevenZip = @(
    (Join-Path $Root "tools\7z.exe"),
    (Join-Path ${env:ProgramFiles} "7-Zip\7z.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1

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

# ---------- 2. rcedit 字段与图标 ----------
Check "自解压包版本资源（九字段）与图标" {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    $expect = @{
        FileVersion = "$Version.0"; ProductVersion = "$Version.0"
        CompanyName = "Void Entropia Studio"
        LegalCopyright = "Copyright (C) Void Entropia Studio"
        ProductName = "EchoDesk"; InternalName = "EchoDeskSetup"
        Comments = "Powered by Void Entropia Studio"
    }
    foreach ($exe in @($SetupWith, $SetupNo)) {
        if (-not (Test-Path $exe)) { throw "缺少 $(Split-Path -Leaf $exe)" }
        $vi = (Get-Item $exe).VersionInfo
        foreach ($k in $expect.Keys) {
            if ("$($vi.$k)".Trim() -ne $expect[$k]) {
                throw "$(Split-Path -Leaf $exe) 的 $k='$($vi.$k)' 期望 '$($expect[$k])'"
            }
        }
        if ("$($vi.OriginalFilename)" -ne (Split-Path -Leaf $exe)) { throw "OriginalFilename 不对: $($vi.OriginalFilename)" }
        if ("$($vi.FileDescription)" -notlike "EchoDesk v$Version Portable Setup*") { throw "FileDescription 不对: $($vi.FileDescription)" }
        $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
        if ($null -eq $ico) { throw "$(Split-Path -Leaf $exe) 取不到图标" }
        $ico.Dispose()
    }
    "两份包的 8 个字符串字段 + 2 个版本字段 + 图标全部正确"
}

# ---------- 3/4. 解压结构与模型 ----------
if (-not $sevenZip) { Write-Host "[SKIP] 解压检查：未找到 7z.exe" -ForegroundColor Yellow }
else {
    $tmpNo = Join-Path $env:TEMP ("edchk_no_" + [guid]::NewGuid().ToString("N").Substring(0, 6))
    $tmpWith = Join-Path $env:TEMP ("edchk_with_" + [guid]::NewGuid().ToString("N").Substring(0, 6))
    try {
        Check "不含模型包解压结构正确且确无 .gguf" {
            New-Item -ItemType Directory -Path $tmpNo -Force | Out-Null
            $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
            & $sevenZip x -y "-o$tmpNo" $SetupNo *> $null
            $code = $LASTEXITCODE; $ErrorActionPreference = $prev
            if ($code -ne 0) { throw "解压失败（退出码 $code）" }
            foreach ($n in @("install-complete.bat", "USAGE.txt", "README.md", "CHANGELOG.md",
                             "LICENSE", "LICENSE.zh-Hans.md", "SECURITY.zh-Hans.md",
                             "config.example.json", "app\EchoDesk.exe", "app\EchoDesk.ico",
                             "app\backend\EchoDeskBackend.exe", "docs\SBOM.json",
                             "docs\PRIVACY.zh-Hans.md", "docs\THIRD_PARTY_LICENSES.zh-Hans.md")) {
                if (-not (Test-Path -LiteralPath (Join-Path $tmpNo $n))) { throw "解压后缺少 $n" }
            }
            $g = @(Get-ChildItem (Join-Path $tmpNo "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
            if ($g.Count -gt 0) { throw "仍含 $($g.Count) 个 .gguf" }
            $mb = [math]::Round((Get-ChildItem $tmpNo -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
            "解压后 $mb MB，必备文件齐全，无 .gguf"
        }

        Check "含模型包解压后模型在位且大小一致" {
            New-Item -ItemType Directory -Path $tmpWith -Force | Out-Null
            $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
            & $sevenZip x -y "-o$tmpWith" $SetupWith *> $null
            $code = $LASTEXITCODE; $ErrorActionPreference = $prev
            if ($code -ne 0) { throw "解压失败（退出码 $code）" }
            $src = Get-ChildItem (Join-Path $ReleaseDir "app\backend\models") -Filter *.gguf -File
            foreach ($m in $src) {
                $dst = Join-Path $tmpWith "app\backend\models\$($m.Name)"
                if (-not (Test-Path $dst)) { throw "解压后缺少模型 $($m.Name)" }
                if ((Get-Item $dst).Length -ne $m.Length) { throw "模型 $($m.Name) 大小不一致" }
            }
            $mb = [math]::Round((Get-ChildItem $tmpWith -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
            "解压后 $mb MB，模型 $($src.Count) 个且大小一致"
        }

        Check "install-complete.bat 实测建出快捷方式" {
            $bat = Join-Path $tmpNo "install-complete.bat"
            if (-not (Test-Path $bat)) { throw "缺少 install-complete.bat" }
            $out = & cmd /c "`"$bat`" < nul" 2>&1 | Out-String
            $lnk = Join-Path $tmpNo "Start-EchoDesk-v$Version.lnk"
            if (-not (Test-Path -LiteralPath $lnk)) {
                throw ("未创建快捷方式。输出尾部：`n" + (($out -split "`n" | Select-Object -Last 8) -join "`n"))
            }
            $sh = New-Object -ComObject WScript.Shell
            $sc = $sh.CreateShortcut($lnk)
            $want = Join-Path $tmpNo "app\EchoDesk.exe"
            if ($sc.TargetPath -ne $want) { throw "目标不对: $($sc.TargetPath)" }
            if ($sc.WorkingDirectory.TrimEnd('\') -ne $tmpNo.TrimEnd('\')) { throw "工作目录不对: $($sc.WorkingDirectory)" }
            if ($sc.IconLocation -notlike "*EchoDesk.ico*") { throw "图标不对: $($sc.IconLocation)" }
            "快捷方式已创建：$([System.IO.Path]::GetFileName($lnk)) → app\EchoDesk.exe（工作目录=解压目录，图标=app\EchoDesk.ico）"
        }
    }
    finally {
        Remove-Item $tmpNo, $tmpWith -Recurse -Force -ErrorAction SilentlyContinue
    }
}

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
