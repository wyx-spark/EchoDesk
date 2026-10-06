# 适用平台：验收 Inno Setup 安装包（Windows 专属产物）
# 交付物验收（Inno Setup 版安装程序）
#
# 这不是"跑一遍完整测试套件"——按任务书「职责范围」，打包环节不做验证执行。
# 本脚本只核对**安装程序本身**是否合格，而且**真的装一遍、真的卸一遍**：
#   1. 安装程序的 SHA256 与 SHA256SUMS.txt 一致
#   2. 安装数据仍追加在 PE 映像之后（防"被 rcedit 削成空壳"）
#   3. 版本资源（Inno 能设置的 7 个字段）与图标
#   4. 安装程序里**没有一个 .gguf**（本版只有一份不含模型的安装程序）
#   5. **不给 /DIR 直接静默安装** → 默认装到安装程序所在目录，当前目录出现 app\ 等结构
#   6. 快捷方式指向 {app}\app\EchoDesk.exe，工作目录 = {app}（外层）
#   7. 安装后 %TEMP% 下无 EchoDesk 相关残留
#   8. install-complete.bat（给 zip 免安装用户的脚本）真跑一次能建出快捷方式
#   9. 卸载程序能把这些文件与快捷方式都清掉
#  10. **卸载必须清干净"运行期新建的文件"**（session.json / models\ / ui_state.json /
#      各种 *_registry.json / 缓存 / 日志…），但 **results\ 必须原样保留**。
#      做法：先把这批文件真的造出来（含 results\ 里的一个结果文件），再卸载，逐项断言。
#  11. installer.iss 的 [UninstallDelete] 清单必须**覆盖**第 10 项那批文件，
#      并且**不允许出现"整目录删 {app}"**的写法（那会把 results\ 一起删掉）。
#
# 为什么第 5 项一定要"不给 /DIR"：虚拟机实测的教训是"用户双击后全盘找不到
# EchoDesk 文件"。默认安装位置是本方案的核心行为，必须按用户真实路径验证，
# 而不是用 /DIR 指定一个我们觉得对的目录 —— 那等于把要验的东西假设成对的。
#
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_installer_deliverables.ps1

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
# 口径：v0.2.1 = 跨平台之前；v0.2.2 = 跨平台版本（见 backend\version.py）。
$Version = "0.2.2"
$Publish = Join-Path $Root "publish"
$Setup = Join-Path $Publish "EchoDesk-v$Version-Setup.exe"
$Sums = Join-Path $Publish "SHA256SUMS.txt"
$ReleaseDir = Join-Path $Publish "EchoDesk-v$Version"

<#
    运行期新建的文件清单（相对安装根目录 {app}）。
    来源：backend\config.py 的默认值（session_file / *_registry.json / cache_file /
    audit_log_file / processed_file / dev_log_file）、backend\main.py 的崩溃日志与
    summary_cache.json、model_hub 的缓存目录（{user_data_root}\cache\model_hub）、
    frontend 的 ui_state.json / snippets.json / Start-*.lnk / frontend_crash.log。

    **这份清单是第 11 项断言的基准**：installer.iss 的 [UninstallDelete] 必须覆盖它。
    以后程序新增了运行期文件，就先加到这儿 —— 断言会立刻告诉你 iss 漏了哪一项。
#>
$RuntimeArtifacts = @(
    "ui_state.json"
    "snippets.json"
    "Start-EchoDesk-v$Version.lnk"
    "cache\model_hub\search-cache.json"
    "app\frontend_crash.log"
    "app\backend\config.user.json"
    "app\backend\session.json"
    "app\backend\browser_state.json"
    "app\backend\models_registry.json"
    "app\backend\skills_registry.json"
    "app\backend\skills_local.json"
    "app\backend\market_cache.json"
    "app\backend\summary_cache.json"
    "app\backend\automation_log.jsonl"
    "app\backend\automation_processed.json"
    "app\backend\backend_crash.log"
    "app\backend\skills_dev.log"
    "app\backend\models\Qwen3-0.6B-Q6_K.gguf"
    "app\backend\models\.cache\huggingface\blob"
    "app\backend\skills\my-skill\skill.json"
    "app\backend\skills_dev\my-dev-skill\skill.json"
)

#: 卸载后**必须保留**的东西：批量处理结果是用户的业务产出
$KeepAfterUninstall = @("results\2025-01-01\result.txt")

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
    if (-not (Test-Path $Setup)) { throw "缺少 $(Split-Path -Leaf $Setup)" }
    $ap = Get-AppendedBytes $Setup
    if ($ap -le 0) {
        throw ("$(Split-Path -Leaf $Setup) 的追加数据为 0 —— PE 被重建过，安装包是空壳。" +
            "最常见原因是对最终 Setup.exe 跑了 rcedit（会丢弃 PE 映像之后的数据）。")
    }
    if ($ap -lt 1MB) { throw "$(Split-Path -Leaf $Setup) 的追加数据只有 $([math]::Round($ap / 1KB, 1)) KB，明显不完整" }
    "$(Split-Path -Leaf $Setup) 追加数据 $([math]::Round($ap / 1MB, 1)) MB"
}

# ---------- 2b. 安装程序里不许有模型 ----------
Check "安装程序不含模型（无 .gguf）" {
    $relModels = @(Get-ChildItem (Join-Path $ReleaseDir "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
    if ($relModels.Count -gt 0) {
        throw "发布目录里还有 $($relModels.Count) 个 .gguf —— 安装程序会把它打进包（请用不带 -IncludeModels 的构建产物）"
    }
    "发布目录无 .gguf，Inno 的 Excludes 也无条件排除模型"
}

# ---------- 3. 版本资源与图标 ----------
Check "安装程序版本资源与图标" {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    $vi = (Get-Item $Setup).VersionInfo
    $expect = @{
        CompanyName       = "Void Entropia Studio"
        LegalCopyright    = "Copyright (C) Void Entropia Studio"
        ProductName       = "EchoDesk"
        # 用 Raw（数值口径）：Inno 把 FileVersion 的字符串末尾 ".0" 省掉，Raw 才是真值
        FileVersionRaw    = "$Version.0"
        ProductVersionRaw = "$Version.0"
        FileDescription   = "EchoDesk v$Version Portable Setup"
        OriginalFilename  = (Split-Path -Leaf $Setup)
    }
    $fields = 0
    foreach ($k in $expect.Keys) {
        if ((ConvertTo-Comparable $vi.$k) -ne (ConvertTo-Comparable $expect[$k])) {
            throw "$(Split-Path -Leaf $Setup) 的 $k='$($vi.$k)' 期望 '$($expect[$k])'"
        }
        $fields++
    }
    $ico = [System.Drawing.Icon]::ExtractAssociatedIcon($Setup)
    if ($null -eq $ico) { throw "$(Split-Path -Leaf $Setup) 取不到图标" }
    $ico.Dispose()
    "$fields 个字段全部正确，图标可从 PE 取出"
}

# ---------- 4/5/6/7/9. 真装、真验、真卸 ----------
<#
    断言：安装后的后端里每个 `.dist-info` 的版本，都必须与**随包分发的**
    `docs\SBOM.json` 所声明的一致。

    为什么必须"真装一遍再比"，而不能只比仓库里的 `docs\SBOM.json`：
    这份 SBOM 是随安装包交给用户的**合规声明**，它描述的对象必须是"安装包里实际装了什么"。
    比工作区里的文件证明不了这一点 —— 中间还隔着 make_installer.ps1 的拷贝与 Inno 的压缩，
    所以这里读的是**安装目录里的那一份** SBOM，比对的是**安装目录里的** `_internal`。

    这条检查对应 v0.2.1 的真实缺陷：`build_release.ps1` 曾调用 PATH 上的 `pyinstaller`
    （解析到全局开发环境），于是打出 click 8.4.2 / cryptography 50.0.0 /
    huggingface_hub 1.27.0 / numpy 2.5.2 / pydantic 2.13.4 / tqdm 4.70.0 的后端，
    而 SBOM 按干净 venv 声明的是 8.5.0 / 50.0.1 / 2.0.0 / 2.5.3 / 2.13.5 / 4.70.1，
    **6 处不符**。当时所有既有检查都是绿的：模块名对得上、文件都在、脚本退出码 0。
#>
function Assert-PayloadMatchesSbom([string]$CaseDir) {
    $internal = Join-Path $CaseDir "app\backend\_internal"
    $sbomPath = Join-Path $CaseDir "docs\SBOM.json"
    if (-not (Test-Path -LiteralPath $sbomPath)) { throw "安装目录里没有 docs\SBOM.json，无法核对" }
    $sbom = Get-Content -LiteralPath $sbomPath -Raw -Encoding UTF8 | ConvertFrom-Json

    $declared = @{}
    foreach ($p in $sbom.packages) {
        if ($p.SPDXID -notlike "SPDXRef-Package-pypi-*") { continue }
        $declared[$p.name.ToLower().Replace("_", "-").Replace(".", "-")] = $p.versionInfo
    }
    if ($declared.Count -eq 0) { throw "随包 SBOM 里没有任何 Python 包条目，无法核对" }

    $compared = 0
    $mismatch = @()
    $undeclared = @()
    $distInfos = @(Get-ChildItem -LiteralPath $internal -Directory -Filter "*.dist-info" -ErrorAction SilentlyContinue)
    foreach ($d in $distInfos) {
        $stem = $d.Name.Substring(0, $d.Name.Length - ".dist-info".Length)
        # 取**最后一个**连字符：pywin32-ctypes 这类自带连字符的包名也要能正确切开
        $idx = $stem.LastIndexOf("-")
        if ($idx -lt 1) { continue }
        $name = $stem.Substring(0, $idx).ToLower().Replace("_", "-").Replace(".", "-")
        $version = $stem.Substring($idx + 1)
        if (-not $declared.ContainsKey($name)) {
            $undeclared += "$name $version"
            continue
        }
        if ($declared[$name] -ne $version) {
            $mismatch += "$name（发布物 $version / SBOM $($declared[$name])）"
        }
        $compared++
    }
    if ($undeclared.Count -gt 0) { throw "发布物里有随包 SBOM 未声明的组件：$($undeclared -join '；')" }
    if ($mismatch.Count -gt 0) {
        throw ("发布物里的库版本与随包 SBOM 声明不一致 —— 后端大概不是用干净 venv 打的" +
            "（见 tools\build_release.ps1 的 -CleanVenv）：`n      " + ($mismatch -join "`n      "))
    }
    if ($compared -eq 0) { throw "在 $internal 里没有找到任何 *.dist-info，版本比对无法成立" }
    if ($compared -lt 5) { throw "只比对到 $compared 个 .dist-info，明显偏少，_internal 可能不完整" }
    return "$compared 个 .dist-info 与随包 SBOM 逐项一致"
}

<#
    从 tools\installer.iss 的 [UninstallDelete] 段解析出卸载清单。
    只取 `Type:` 与 `Name:` 两个字段，把 `{app}\` 前缀去掉得到相对路径。

    为什么要解析源文件而不是"再抄一份清单"：清单抄两份必然漂移 ——
    改了一边忘了另一边，验收照样全绿，而用户装完卸载仍然留一地文件。
#>
function Get-IssUninstallEntries {
    $iss = Join-Path $PSScriptRoot "installer.iss"
    if (-not (Test-Path $iss)) { throw "找不到 installer.iss" }
    $lines = Get-Content -LiteralPath $iss -Encoding UTF8
    $inSection = $false
    $entries = @()
    foreach ($line in $lines) {
        $t = $line.Trim()
        if ($t -match '^\[UninstallDelete\]') { $inSection = $true; continue }
        if ($t -match '^\[') { $inSection = $false; continue }
        if (-not $inSection) { continue }
        if ($t -eq '' -or $t.StartsWith(';')) { continue }
        if ($t -notmatch '^Type:\s*(\w+)\s*;\s*Name:\s*"(.*)"\s*$') { continue }
        $rel = $Matches[2]
        # 只认指向安装根目录的条目；{app} 自身（相对路径为空）是"整目录删"，另行拦截
        if ($rel -notlike '{app}*') { continue }
        $rest = $rel.Substring(5).TrimStart('\')
        $entries += [pscustomobject]@{ Type = $Matches[1].ToLower(); Rel = $rest }
    }
    return $entries
}

<#
    在安装目录里造出"程序运行时会新建的那批文件"，供卸载清理实测使用。
    内容随意，只要文件/目录真的存在（卸载要删的是路径，不是内容）。
#>
function New-RuntimeArtifacts([string]$CaseDir) {
    $created = @()
    foreach ($rel in $RuntimeArtifacts) {
        $full = Join-Path $CaseDir $rel
        $dir = Split-Path -Parent $full
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Set-Content -LiteralPath $full -Value "runtime-artifact-test" -Encoding UTF8
        $created += $rel
    }
    foreach ($rel in $KeepAfterUninstall) {
        $full = Join-Path $CaseDir $rel
        $dir = Split-Path -Parent $full
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Set-Content -LiteralPath $full -Value "user-business-output" -Encoding UTF8
    }
    return $created.Count
}

<#
    顺序说明（此前是两个变体、共用同一个 AppId，所以必须"装一个 → 验一个 → 卸一个"；
    现在只有一份安装程序，这层约束没了，但流程保持不变：装 → 验 → 造运行期文件 → 卸 → 验残留）。
    每一轮都不传 /DIR，验证默认目录行为。
#>
function Test-InstallerRound([string]$Exe, [string]$Label) {
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
            "CHANGELOG.zh-Hans.md", "CONTRIBUTING.zh-Hans.md", "docs\en\DEVELOPER.md",
            "LICENSE", "LICENSE.zh-Hans.md", "SECURITY.zh-Hans.md",
            "config.example.json", "docs\PRIVACY.zh-Hans.md",
            "docs\THIRD_PARTY_LICENSES.zh-Hans.md")) {
        if (-not (Test-Path -LiteralPath (Join-Path $caseDir $rel))) { $missing += $rel }
    }
    if ($missing.Count -gt 0) {
        throw "默认安装目录（安装程序所在目录）里缺少：$($missing -join ', ')"
    }

    # 4/5-a-2：安装后的库版本必须与**随包分发的** docs\SBOM.json 逐项一致。
    # 放在这里是因为此时安装目录还在盘上（本轮末尾会卸载）。
    $sbomVerdict = Assert-PayloadMatchesSbom $caseDir

    # 5-b：安装程序一律不含模型
    $modelDst = @(Get-ChildItem (Join-Path $caseDir "app\backend\models") -Filter *.gguf -File -ErrorAction SilentlyContinue)
    if ($modelDst.Count -gt 0) { throw "安装程序里出现了 $($modelDst.Count) 个 .gguf（本版安装程序不含模型）" }

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

    $summary = "装到 $caseDir；快捷方式 → app\EchoDesk.exe（工作目录 = 安装目录）；%TEMP% 无残留；$sbomVerdict"

    # 10：造出运行期新建的文件（含 results\ 里的一个结果文件），再卸载验残留。
    # 这一步是"卸载要清干净"这条需求的**实测**部分：不造真文件就只是看着清单好看。
    $madeCount = New-RuntimeArtifacts $caseDir

    # 9/10：卸载。卸完要同时满足两件事：
    #   * 运行期文件一个都不剩；
    #   * results\（用户业务产出）原样保留。
    $unins = Join-Path $caseDir "unins000.exe"
    if (-not (Test-Path $unins)) { throw "安装目录里没有卸载程序（Uninstallable=no？）：$caseDir" }
    $ucode = Invoke-SilentUninstall $caseDir
    if ($ucode -ne 0) { throw "卸载失败（退出码 $ucode）" }
    foreach ($gone in @("app", "docs")) {
        if (Test-Path (Join-Path $caseDir $gone)) { throw "卸载后 $gone\ 仍在（$caseDir）" }
    }
    if (Test-Path (Join-Path $StartMenuDir "EchoDesk.lnk")) { throw "卸载后开始菜单快捷方式仍在" }

    $staleRuntime = @()
    foreach ($rel in $RuntimeArtifacts) {
        if (Test-Path -LiteralPath (Join-Path $caseDir $rel)) { $staleRuntime += $rel }
    }
    if ($staleRuntime.Count -gt 0) {
        throw ("卸载后仍留着 $($staleRuntime.Count) 个运行期文件（应补进 tools\installer.iss 的 [UninstallDelete]）：`n      " +
            ($staleRuntime -join "`n      "))
    }
    $keptMissing = @()
    foreach ($rel in $KeepAfterUninstall) {
        if (-not (Test-Path -LiteralPath (Join-Path $caseDir $rel))) { $keptMissing += $rel }
    }
    if ($keptMissing.Count -gt 0) {
        throw "卸载把 results\ 里的用户结果删掉了（必须保留）：$($keptMissing -join ', ')"
    }

    # 允许剩下的只有：安装程序本身（用户放的）+ results\（保留的用户结果）
    $leftover = @(Get-ChildItem $caseDir -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -notlike "*Setup.exe" -and
            $_.FullName.Substring($caseDir.Length + 1) -notlike "results\*"
        } | ForEach-Object { $_.FullName.Substring($caseDir.Length + 1) })
    if ($leftover.Count -gt 0) { throw "卸载后目录里仍有多余文件: $($leftover -join ', ')" }

    return [pscustomobject]@{
        Dir        = $caseDir
        Models     = $modelDst.Count
        Runtime    = $madeCount
        Target     = $sc.TargetPath
        WorkingDir = $sc.WorkingDirectory
        Summary    = $summary
    }
}

# ---------- 10b. installer.iss 的卸载清单必须覆盖上面那批运行期文件 ----------
Check "installer.iss 卸载清单覆盖全部运行期文件，且不整目录删（保 results）" {
    $entries = @(Get-IssUninstallEntries)
    if ($entries.Count -eq 0) { throw "installer.iss 的 [UninstallDelete] 没解析出任何条目" }
    # 护栏：绝不允许"删整个 {app}"——那会连 results\ 一起删掉
    if (@($entries | Where-Object { $_.Rel -eq '' }).Count -gt 0) {
        throw "installer.iss 里出现了删除整个安装根目录的写法（Name: `"{app}`"），会把 results\ 一起删掉"
    }
    $uncovered = @()
    foreach ($rel in $RuntimeArtifacts) {
        $hit = $false
        foreach ($e in $entries) {
            # 用 -like 而不是 -eq：iss 里允许通配（例如 "{app}\*.lnk"）。
            # 第一版这里写的是 -eq，于是"卸载清单没覆盖 Start-*.lnk"误报了一次 ——
            # 实际卸载是好的（通配在 Inno 里生效），是**检查器**不支持通配。
            if ($e.Type -eq 'files') {
                if ($rel -like $e.Rel) { $hit = $true; break }
            }
            elseif ($e.Type -eq 'filesandordirs') {
                # 目录条目：命中"该目录本身"或"它下面的任何东西"
                if ($rel -like $e.Rel -or $rel -like ($e.Rel + '\*')) { $hit = $true; break }
            }
        }
        if (-not $hit) { $uncovered += $rel }
    }
    if ($uncovered.Count -gt 0) {
        throw ("这些运行期文件没被 [UninstallDelete] 覆盖，卸载后会留在用户机器上：`n      " +
            ($uncovered -join "`n      "))
    }
    "$($entries.Count) 条卸载条目覆盖全部 $($RuntimeArtifacts.Count) 个运行期文件，且无整目录删除"
}

Check "安装程序：默认装到安装程序所在目录、结构齐全、无模型、快捷方式正确、可卸载且清干净运行期文件（保留 results）" {
    $r = Test-InstallerRound $Setup "single"
    "$($r.Summary)；造了 $($r.Runtime) 个运行期文件，卸载后全部清除、results\ 保留"
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
