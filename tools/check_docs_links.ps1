# 适用平台：检查逻辑与平台无关（三平台都需要），但当前只有 PowerShell 实现 —— 应补 .py 等价物，见记录中的未完成项
<#
.SYNOPSIS
    EchoDesk 文档检查：相对链接有效性、字面量 TODO、UTF-8 合法性。

.DESCRIPTION
    对应任务书 1.4「文档检查：确认文档链接有效、截图未失效」。

    检查范围（存在才检查，缺失不报错）：
      * README.md、CONTRIBUTING.md、SECURITY.md、CODE_OF_CONDUCT.md（仓库根目录）；
      * README.<locale>.md（多语言 README 镜像，如 README.zh-Hans.md）；
      * docs\**\*.md，含 docs\zh-Hans\、docs\en\ 等语言目录。

    三项检查：
      1) 相对 Markdown 链接（含图片 ![]()）必须指向仓库内已存在的文件或目录。
         外链（http/https/mailto/tel/ftp/data/javascript）、纯锚点（#xxx）与
         绝对路径（D:\...）不检查：离线环境无法验证，且不属于「仓库内链接」。
         图片链接指向不存在的文件同样算错误（截/示意图缺失要能暴露出来），
         确属有意占位的可在下方 $AllowedMissingLinks 白名单里登记。
      2) 文件不得出现字面量 TODO（大小写敏感），避免待办标记随发布物发出。
      3) 文件必须是合法 UTF-8；带 BOM 只告警不判失败（.gitattributes 要求无 BOM）。

    退出码：0 = 全部通过；1 = 存在失败项（可直接用于 CI 与发布前自检）。

.PARAMETER RepoRoot
    仓库根目录。默认取本脚本所在目录的上一级（tools\..）。

.EXAMPLE
    pwsh -File tools\check_docs_links.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\check_docs_links.ps1 -RepoRoot C:\src\EchoDesk

.NOTES
    兼容 Windows PowerShell 5.1：不使用 ?. / ?? / 三元运算符，不使用 here-string，
    不把原生命令输出接进 cmdlet。只依赖 .NET 核心类型（[regex]）与内置 cmdlet。
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = ""
)

# ---------------------------------------------------------------------------
# 有意保留的占位链接白名单（按文档里书写的原样填写，例如 docs/zh-Hans/xxx.png）。
# 用途：文档先行、截图稍后补的场景。请尽量留空——图片链接指向不存在的文件
# 正是本脚本要报出来的错误，登记白名单等于放弃该项保护。
# ---------------------------------------------------------------------------
$AllowedMissingLinks = @(
    # "docs/zh-Hans/screenshots/automation-tab.png"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path -LiteralPath $RepoRoot)) {
    throw "仓库根目录不存在: $RepoRoot"
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

# ---------- 收集待检查文件 ----------
$targets = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

foreach ($name in @("README.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md")) {
    $path = Join-Path $RepoRoot $name
    if (Test-Path -LiteralPath $path) {
        [void]$targets.Add($path)
    }
    elseif ($name -eq "README.md") {
        # README 缺失不是本脚本的职责（另由仓库检查负责），这里只提示不判失败
        [void]$warnings.Add("未找到根目录 README.md，已跳过其链接检查")
    }
}

# 多语言 README（README.zh-Hans.md、README.ja.md …）同样是用户入口，一并检查；
# 它们常被当作主 README 的镜像，最容易在文档搬目录后留下失效链接。
$localizedReadmes = Get-ChildItem -LiteralPath $RepoRoot -File -Filter "README.*.md" -ErrorAction SilentlyContinue
foreach ($file in $localizedReadmes) {
    if (-not $targets.Contains($file.FullName)) {
        [void]$targets.Add($file.FullName)
    }
}

$docsDir = Join-Path $RepoRoot "docs"
if (Test-Path -LiteralPath $docsDir) {
    $docFiles = Get-ChildItem -LiteralPath $docsDir -Recurse -File -Filter *.md -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.git\\' }
    foreach ($file in $docFiles) {
        [void]$targets.Add($file.FullName)
    }
}
else {
    [void]$warnings.Add("未找到 docs 目录，已跳过文档目录检查")
}

if ($targets.Count -eq 0) {
    Write-Host "没有找到需要检查的 Markdown 文件（仓库根目录: $RepoRoot）" -ForegroundColor Yellow
    exit 0
}

# ---------- 工具函数 ----------

function Get-RepoRelativePath {
    param([string]$FullPath)
    if ($FullPath.StartsWith($RepoRoot)) {
        return ($FullPath.Substring($RepoRoot.Length).TrimStart('\') -replace '\\', '/')
    }
    return $FullPath
}

function Test-Utf8File {
    <# 用严格解码器判断字节序列是否合法 UTF-8；返回 $true / $false #>
    param([string]$Path)
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
    }
    catch {
        return $false
    }
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        [void]$strict.GetString($bytes)
        return $true
    }
    catch {
        return $false
    }
}

function Test-FileHasBom {
    param([string]$Path)
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
    }
    catch {
        return $false
    }
    if ($bytes.Length -ge 3) {
        return ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    }
    return $false
}

# Markdown 链接/图片：![alt](target "title") 与 [text](target)
$linkPattern = '(!?)\[([^\]]*)\]\(([^)]+)\)'
$fencePattern = '^\s*(```|~~~)'

# ---------- 逐个文件检查 ----------
$failures = New-Object System.Collections.ArrayList
$linkCount = 0
$checkedCount = 0

foreach ($file in $targets) {
    $relFile = Get-RepoRelativePath -FullPath $file
    $checkedCount++

    if (-not (Test-Utf8File -Path $file)) {
        [void]$failures.Add("${relFile}: 不是合法 UTF-8（含非法字节或编码错误）")
        continue
    }
    if (Test-FileHasBom -Path $file) {
        [void]$warnings.Add("${relFile}: 带 UTF-8 BOM（仓库约定 .md 不带 BOM）")
    }

    $lines = @(Get-Content -LiteralPath $file -Encoding UTF8)
    $inFence = $false
    $dir = Split-Path -Parent $file

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = [string]$lines[$i]
        $lineNo = $i + 1

        if ($line -match $fencePattern) {
            $inFence = -not $inFence
            continue
        }
        if ($line -cmatch 'TODO') {
            [void]$failures.Add("${relFile}:${lineNo}: 含字面量 TODO")
        }
        if ($inFence) {
            # 代码块里的示例路径不是真实链接，跳过
            continue
        }

        # 行内代码（`[文字](地址)` 这类「Markdown 语法示例」）同样不是真实链接：
        # 先把行内代码整段抹成空格再做链接匹配。
        $scan = [regex]::Replace($line, '`[^`]*`', ' ')

        $found = [regex]::Matches($scan, $linkPattern)
        foreach ($match in $found) {
            $raw = $match.Groups[3].Value.Trim()
            if ($raw -eq "") { continue }

            # 去掉 <...> 包裹与 "标题"
            if ($raw.StartsWith("<") -and $raw.EndsWith(">")) {
                $raw = $raw.Substring(1, $raw.Length - 2).Trim()
            }
            $raw = $raw -replace '\s+"[^"]*"$', ''
            $raw = $raw -replace "\s+'[^']*'$", ''
            if ($raw -eq "") { continue }

            # 外链 / 锚点 / 绝对路径：不检查
            if ($raw -match '^(?i)(https?|mailto|tel|ftp|ftps|data|javascript|file):') { continue }
            if ($raw.StartsWith("#")) { continue }
            if ($raw -match '^(?i)[a-z]:[\\/]') { continue }
            if ($raw -match '^\\\\') { continue }

            $pathPart = $raw -replace '#.*$', ''
            if ($pathPart -eq "") { continue }

            $linkCount++
            $lookup = $pathPart -replace '%20', ' '
            $lookup = $lookup -replace '/', '\'
            $full = Join-Path $dir $lookup

            if (Test-Path -LiteralPath $full) { continue }
            if ($AllowedMissingLinks -contains $pathPart) { continue }

            $text = $match.Groups[2].Value
            $kind = "链接"
            if ($match.Groups[1].Value -eq "!") { $kind = "图片" }
            [void]$failures.Add("${relFile}:${lineNo}: $kind 目标不存在 → [$text]($raw)")
        }
    }
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host "===== 文档检查（链接 / TODO / UTF-8） ====="
Write-Host ("仓库根目录: $RepoRoot")
Write-Host ("检查文件  : $checkedCount 个")
Write-Host ("相对链接  : $linkCount 条（外链与锚点已跳过）")

foreach ($warning in $warnings) {
    Write-Host "WARN $warning" -ForegroundColor Yellow
}

if ($failures.Count -eq 0) {
    Write-Host "全部通过" -ForegroundColor Green
    exit 0
}

foreach ($failure in $failures) {
    Write-Host "FAIL $failure" -ForegroundColor Red
}
Write-Host ("共 {0} 项失败" -f $failures.Count) -ForegroundColor Red
exit 1
