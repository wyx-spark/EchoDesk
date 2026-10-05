# EchoDesk Emoji 显示检查（任务书修复六）
#
# 修复六的根因是"字体回退链里根本没有 emoji 字体"，靠肉眼很难发现（不同 Windows
# 版本表现还不一样），因此这里做成可自动执行的检查：
#
#   1. 扫描全部源码/语言包里实际用到的 emoji；
#   2. 读出 Resources\Styles.xaml 里声明的字体链（UiFontFamily / MonoFontFamily）；
#   3. 用 WPF 的 GlyphTypeface 逐字体查字形覆盖表（CharacterToGlyphMap），
#      计算"字体链的并集"能否覆盖全部在用的 emoji；
#   4. 校验前端的 ASCII 降级表与后端的 emoji.py 都能覆盖这些 emoji
#      （否则在不支持 emoji 的环境会退化成 "?"，等于信息丢失）。
#
# 用法：powershell -File tools\check_emoji.ps1
param(
    [switch]$Verbose
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore

$Root = Split-Path -Parent $PSScriptRoot
$failures = New-Object System.Collections.ArrayList
function Fail([string]$m) { [void]$failures.Add($m); Write-Output "[emoji] FAIL $m" }
function Ok([string]$m) { Write-Output "[emoji] OK   $m" }

# 列表太长会把终端刷爆，超过 12 项就只列前 12 个 + 总数
function Show-List([string[]]$items) {
    if ($items.Count -le 12) { return ($items -join ', ') }
    return (($items | Select-Object -First 12) -join ', ') + " …（共 $($items.Count) 个）"
}

# ---------- 1. 收集源码里用到的 emoji ----------
# 文件清单来自 `git ls-files`：只扫**纳入版本管理**的源文件。
# 之前用 Get-ChildItem -Recurse 会把 backend\.venv 里的第三方包一起扫进来
# （rich/click 的图标表、idna 的 Unicode 表里有上千个码位），既慢又把结论淹掉。
Write-Output "[emoji] 扫描源码与语言包 …"
$scanRoots = @('backend/', 'frontend/', 'examples/', 'tools/')
$exts = @('.py', '.cs', '.xaml', '.json', '.md', '.ps1', '.bat')
$usage = @{}      # codepoint -> 首次出现位置（便于定位）
# 用正则一次性取出候选（代理对 + 符号区），比逐字符 ConvertToUtf32 快两个数量级
$emojiRegex = [regex]'[\uD800-\uDBFF][\uDC00-\uDFFF]|[\u2600-\u27BF\u2B00-\u2BFF\uFE0F]'

Push-Location $Root
try { $tracked = & git ls-files } finally { Pop-Location }
if (-not $tracked) { throw "git ls-files 没有返回任何文件（当前目录不是 git 仓库？）" }

$scanned = 0
foreach ($rel in $tracked) {
    $norm = $rel -replace '\\', '/'
    $inScope = $false
    foreach ($pre in $scanRoots) { if ($norm.StartsWith($pre)) { $inScope = $true; break } }
    if (-not $inScope) { continue }
    if ($exts -notcontains [System.IO.Path]::GetExtension($norm)) { continue }
    if ($norm -match '(^|/)(obj|bin|\.venv|__pycache__|node_modules|publish|artifacts)/') { continue }
    $path = Join-Path $Root $rel
    if (-not (Test-Path $path)) { continue }
    $scanned++
    $text = Get-Content $path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $text) { continue }
    foreach ($m in $emojiRegex.Matches($text)) {
        $s = $m.Value
        $cp = if ($s.Length -eq 2) { [char]::ConvertToUtf32($s, 0) } else { [int][char]$s[0] }
        if ($cp -eq 0xFE0F) { continue }        # 变体选择符不是独立字符
        if (-not $usage.ContainsKey($cp)) { $usage[$cp] = $rel }
    }
}
$codes = @($usage.Keys | Sort-Object)
Write-Output "[emoji] 扫描 $scanned 个受版本管理的源文件，共发现 $($codes.Count) 个不同 emoji"

# ---------- 2. 读出字体链 ----------
$stylesPath = Join-Path $Root 'frontend\Resources\Styles.xaml'
$stylesText = Get-Content $stylesPath -Raw -Encoding UTF8
$chainRegex = [regex]'<FontFamily x:Key="(UiFontFamily|MonoFontFamily|IconFontFamily)">([^<]+)</FontFamily>'
$chains = @{}
foreach ($m in $chainRegex.Matches($stylesText)) {
    $chains[$m.Groups[1].Value] = $m.Groups[2].Value
}
if ($chains.Count -eq 0) { Fail '未在 Styles.xaml 里找到 FontFamily 资源' }
foreach ($k in $chains.Keys) { Write-Output "[emoji] $k = $($chains[$k])" }

# 字体文件目录（按家族名找文件：读注册表最可靠）
function Get-FontFiles([string]$family) {
    $key = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    $names = @()
    try {
        $props = Get-ItemProperty -Path $key
        foreach ($p in $props.PSObject.Properties) {
            if ($p.Name -like '*PSPath*' -or $p.Name -like '*PSParentPath*' -or
                $p.Name -like '*PSChildName*' -or $p.Name -like '*PSDrive*' -or
                $p.Name -like '*PSProvider*') { continue }
            if ($p.Name -like "$family*") { $names += $p.Value }
        }
    } catch { }
    $files = @()
    foreach ($n in $names) {
        $f = if ([System.IO.Path]::IsPathRooted($n)) { $n } else { Join-Path $env:WINDIR "Fonts\$n" }
        if (Test-Path $f) { $files += $f }
    }
    return $files
}

# ---------- 3. 逐字体算覆盖 ----------
function Get-Coverage([string[]]$files) {
    $set = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($f in $files) {
        try {
            $uri = New-Object System.Uri(("file:///" + ($f -replace '\\', '/')))
            $gt = New-Object System.Windows.Media.GlyphTypeface($uri)
            foreach ($k in $gt.CharacterToGlyphMap.Keys) { [void]$set.Add([int]$k) }
        } catch {
            Write-Output "[emoji]    （无法解析字体 $f：$($_.Exception.Message)）"
        }
    }
    return $set
}

foreach ($name in @('UiFontFamily', 'MonoFontFamily', 'IconFontFamily')) {
    $chain = $chains[$name]
    if (-not $chain) { continue }
    $families = $chain -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $allFiles = @()
    $perFamily = @{}
    foreach ($fam in $families) {
        $files = @(Get-FontFiles $fam)
        $perFamily[$fam] = $files.Count
        $allFiles += $files
    }
    if ($allFiles.Count -eq 0) { Fail "$name 字体链里没有任何一个字体文件存在：$chain"; continue }
    $coverage = Get-Coverage $allFiles
    $missing = @()
    foreach ($cp in $codes) {
        if (-not $coverage.Contains($cp)) {
            $missing += ('U+{0:X} ({1})' -f $cp, $usage[$cp])
        }
    }
    $present = ($families | Where-Object { $perFamily[$_] -gt 0 }) -join ', '
    if ($missing.Count -gt 0) {
        Fail "$name 字体链覆盖不足，$($missing.Count) 个 emoji 会显示成方块：$((Show-List $missing))"
    } else {
        Ok "$name 覆盖全部 $($codes.Count) 个在用 emoji（参与回退的字体：$present）"
    }
}

# ---------- 4. 降级表是否覆盖全部 emoji ----------
$asciiMapSrc = Get-Content (Join-Path $Root 'backend\emoji.py') -Raw -Encoding UTF8
$csMapSrc = Get-Content (Join-Path $Root 'frontend\Services\Emoji.cs') -Raw -Encoding UTF8
$unmapped = @()
foreach ($cp in $codes) {
    $ch = [char]::ConvertFromUtf32($cp)
    if (-not $asciiMapSrc.Contains($ch)) { $unmapped += ('U+{0:X} ({1})' -f $cp, $usage[$cp]) }
}
if ($unmapped.Count -gt 0) {
    Fail "后端 emoji.py 的 ASCII 降级表缺少 $($unmapped.Count) 个 emoji（降级会变成 '?'）：$((Show-List $unmapped))"
} else {
    Ok "后端 ASCII 降级表覆盖全部在用 emoji"
}
$unmappedCs = @()
foreach ($cp in $codes) {
    $ch = [char]::ConvertFromUtf32($cp)
    if (-not $csMapSrc.Contains($ch)) { $unmappedCs += ('U+{0:X} ({1})' -f $cp, $usage[$cp]) }
}
if ($unmappedCs.Count -gt 0) {
    Fail "前端 Emoji.cs 的 ASCII 降级表缺少 $($unmappedCs.Count) 个 emoji：$((Show-List $unmappedCs))"
} else {
    Ok "前端 ASCII 降级表覆盖全部在用 emoji"
}

# 跨平台前端（Avalonia）另有一份 Emoji.cs：同样的表必须同样完整，
# 否则非 Windows 上关掉 emoji 时会出现方块（WPF 版测不到这一份）。
$avaCsPath = Join-Path $Root 'frontend-avalonia\EchoDesk.Avalonia\Services\Emoji.cs'
if (Test-Path $avaCsPath) {
    $avaCsMapSrc = Get-Content $avaCsPath -Raw -Encoding UTF8
    $unmappedAva = @()
    foreach ($cp in $codes) {
        $ch = [char]::ConvertFromUtf32($cp)
        if (-not $avaCsMapSrc.Contains($ch)) { $unmappedAva += ('U+{0:X} ({1})' -f $cp, $usage[$cp]) }
    }
    if ($unmappedAva.Count -gt 0) {
        Fail "跨平台前端 Emoji.cs 的 ASCII 降级表缺少 $($unmappedAva.Count) 个 emoji：$((Show-List $unmappedAva))"
    } else {
        Ok "跨平台前端（Avalonia）ASCII 降级表覆盖全部在用 emoji"
    }
} else {
    Write-Output "[emoji] 跳过跨平台前端 Emoji.cs（文件不存在）"
}

if ($Verbose) {
    Write-Output "[emoji] 在用 emoji 明细："
    foreach ($cp in $codes) { Write-Output ("  U+{0:X4}  {1}  ({2})" -f $cp, [char]::ConvertFromUtf32($cp), $usage[$cp]) }
}

if ($failures.Count -gt 0) {
    Write-Output "[emoji] ✗ 发现 $($failures.Count) 处问题"
    exit 1
}
Write-Output "[emoji] ✓ Emoji 检查全部通过"
exit 0
