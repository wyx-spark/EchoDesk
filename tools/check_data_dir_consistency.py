#!/usr/bin/env python3
"""前后端「用户数据目录」一致性门禁（自测记录 §5.2 / §10 第 8 条）。

## 这个工具在防什么

同一个模拟环境里，**后端**（`backend/config.py` 的 `user_data_root()`）与**前端**
（WPF `ShortcutService.UserDataDirectory`、Avalonia `UserPaths.DataRoot`）必须算出
**同一个目录**。算不到一起的后果不是报错，而是"界面把 `ui_state.json` 写到这里、
后端把 `session.json` 写到哪里" —— 用户导出/备份数据时两半都在，但谁也不知道哪半是真的。

记录 §5.2 的结论是：两侧**算法并不一样**，只是"当前发布布局恰好同值"，而且**没有门禁盯着**。
本工具就是那条门禁。

## 做法：跑真代码，不复刻算法

差异清单要求的对照方式有两种（"复刻算法"或"跨语言对照"）。这里选**跨语言对照**，
因为复刻出来的第二份实现在两侧任何一边改动后都会静默漂移，而门禁自身漂移最危险。

* **后端**：把 `backend/config.py` **原样**（同一个文件）放到模拟布局的后端目录里，
  用子进程 import 后调用 `user_data_root()`。需要"打包形态"的布局就把
  `sys.frozen` / `sys.executable` 一并模拟成该布局里的路径。
* **前端**：把两侧**真实源文件**（`frontend/Services/ShortcutService.cs`、
  `frontend-avalonia/.../UserPaths.cs`、`PlatformInfo.cs`）链接进一个临时控制台工程编译，
  再把产物**拷贝进模拟布局的程序目录**后运行 —— 于是代码里读的
  `AppContext.BaseDirectory` / `Environment.ProcessPath` 恰好就是该布局的路径。
  **两侧源文件一行都不改**（本工具只读它们，并把 sha256 打出来备查）。

## 判定口径

* `contract`（发布布局）：**两侧必须一致**。不一致 → 退出码 1。
  这些是真正在发布的形态（便携 zip / Inno 安装 / 改名部署 / 三平台 tar）。
* `edge`（边界输入与源码检出）：**只报告，不参与退出码**（要参与就加 `--fail-on-edge`）。
  `ECHODESK_DATA` 的写法差异（相对路径、首尾空白、`~`、盘符不存在）与"开发机上前端在自己
  的构建输出目录里跑"都属于这一类：它们的成因有一句话的解释，**但要不要改属于维护者裁定**
  —— 本工具的约束是"只报告，不擅自改任一侧"。`--fail-on-edge` 供裁定之后启用。
* `skip`：本机跑不了的形态（Linux/macOS 系统级安装等），**如实列为未覆盖**，不算通过。
  跨平台分支只能在 macOS/Linux 上执行；在 Windows 上跑本工具**不能**证明那些形态一致。

## 用法

    python tools/check_data_dir_consistency.py                # 门禁模式
    python tools/check_data_dir_consistency.py --fail-on-edge  # 连边界项一起判定
    python tools/check_data_dir_consistency.py --json out.json # 额外落一份机器可读结果
    python tools/check_data_dir_consistency.py --keep-temp     # 保留临时目录供排查

退出码：0 = contract 全一致；1 = 有不一致；2 = 工具自身跑不起来（缺 dotnet / 缺源文件 / 编译失败）。

**尚未纳入 `tools/verify_release.ps1`**（任务书要求"稳定后再考虑"）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "@@RESULT@@"
PROBE_ASSEMBLY = "echodesk-datadir-probe"

# 两侧算法所在的**唯一**文件（工具只读它们；改动它们就等于改动被测算法）
SOURCE_FILES = {
    "backend": "backend/config.py",
    "wpf": "frontend/Services/ShortcutService.cs",
    "avalonia_paths": "frontend-avalonia/EchoDesk.Avalonia/Services/UserPaths.cs",
    "avalonia_platform": "frontend-avalonia/EchoDesk.Avalonia/Services/PlatformInfo.cs",
}


# --------------------------------------------------------------------------- #
# 模拟环境（场景）定义
# --------------------------------------------------------------------------- #

@dataclass
class Scenario:
    """一个模拟环境：给定布局 + 给定环境变量，两侧应当算出同一个目录。"""

    key: str
    kind: str                     # contract / edge / skip
    title: str
    note: str = ""                # 差异成因或判定理由（打印在结论旁边）
    root: str = ""                # 相对 <临时目录>/layouts 的场景根
    dirs: tuple[str, ...] = ()    # 场景根下要创建的目录
    program_dir: str = "app"      # 前端程序目录（探针拷到这里再运行）
    backend_dir: str = "app/backend"   # 后端 config.py 所在目录
    backend_from_repo: bool = False    # 直接用源码检出的 backend/（开发形态）
    frozen: bool = True           # 后端按打包 exe 判定（sys.frozen/sys.executable）
    env_template: str | None = None    # ECHODESK_DATA 的值；None = 删除该变量
    frontends: tuple[str, ...] = ("wpf", "avalonia")
    windows_only: bool = False        # 只在 Windows 上有意义的场景（盘符一类）
    skip_reason: str = ""

    def env_value(self, scenario_root: Path, spare_drive: str) -> str | None:
        if self.env_template is None:
            return None
        return (self.env_template
                .replace("{root}", str(scenario_root))
                .replace("{spare_drive}", spare_drive)
                .replace("{sep}", os.sep))


PORTABLE_DIRS = ("app", "app/backend")

SCENARIOS: list[Scenario] = [
    # ---------- contract：正在发布的布局，两侧必须一致 ----------
    Scenario(
        key="portable-zip",
        kind="contract",
        title="便携/解压版（Windows zip）：<发布>\\app\\EchoDesk.exe + <发布>\\app\\backend\\",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
    ),
    Scenario(
        key="installer",
        kind="contract",
        title="安装版（Inno：装到 Setup.exe 所在目录，PrivilegesRequired=lowest）",
        root="installed/EchoDesk",
        dirs=PORTABLE_DIRS,
        note="与便携版同构，只有外层目录名不同",
    ),
    Scenario(
        key="renamed-program-dir",
        kind="contract",
        title="程序目录不叫 app（改名部署 / 旧版平铺布局）：<目录>\\EchoDesk.exe + <目录>\\backend\\",
        root="flat/EchoDesk-Portable",
        dirs=("backend",),
        program_dir=".",
        backend_dir="backend",
        note="记录 §5.2 点名担心的形态：前端的 app 判定不命中，后端靠跳过 backend 目录",
    ),
    Scenario(
        key="crossplatform-tar",
        kind="contract",
        title="三平台 tar/zip（Avalonia + standalone 后端）：<发布>/app/EchoDesk + <发布>/app/backend/app/main.py",
        root="EchoDesk-v0.2.2-linux-x64",
        dirs=("app", "app/backend/app"),
        program_dir="app",
        backend_dir="app/backend/app",
        frozen=False,                       # run-backend.sh 用自带解释器跑 main.py
        frontends=("avalonia",),            # WPF 不出现在三平台包里
        note="后端不是打包 exe：config.py 落在 <发布>/app/backend/app/（沿途要跳过 app 与 backend 两层）",
    ),
    Scenario(
        key="portable-env-absolute",
        kind="contract",
        title="便携版 + ECHODESK_DATA 指向绝对路径（企业受管盘的标准用法）",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="{root}{sep}custom-data",
    ),

    # ---------- edge：边界输入与源码检出，只报告 ----------
    Scenario(
        key="dev-source-checkout",
        kind="edge",
        title="开发机（源码检出）：后端 backend/，前端 frontend*/bin/Debug/…",
        root="dev-src",
        dirs=("frontend/bin/Debug/net10.0-windows", "frontend-avalonia/bin/Debug/net10.0"),
        program_dir="frontend/bin/Debug/net10.0-windows",
        backend_from_repo=True,
        frozen=False,
        note="后端按源码检出算到仓库根；前端算到自己的构建输出目录 —— 两者天生不同，"
             "属「开发机现象」还是「缺陷」需维护者裁定（本工具只报告）",
    ),
    Scenario(
        key="env-relative",
        kind="edge",
        title="便携版 + ECHODESK_DATA=echodesk-data（相对路径）",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="echodesk-data",
        note="后端/WPF 规范化成绝对路径，Avalonia 原样返回相对路径（结果随 cwd 漂移）",
    ),
    Scenario(
        key="env-surrounding-spaces",
        kind="edge",
        title="便携版 + ECHODESK_DATA 带首尾空白",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="  {root}{sep}spaced-data  ",
        note="后端/WPF 去空白并规范化，Avalonia 原样返回带空白的值",
    ),
    Scenario(
        key="env-tilde",
        kind="edge",
        title="便携版 + ECHODESK_DATA=~/echodesk-data（家目录写法）",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="~/echodesk-data",
        note="后端 expanduser 展开成家目录，前端两侧都把 ~ 当普通字符",
    ),
    Scenario(
        key="env-blank",
        kind="edge",
        title="便携版 + ECHODESK_DATA 只含空白（等于没设）",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="   ",
        note="对照项：三侧都按「未设置」处理，应当与 portable-zip 同值",
    ),
    Scenario(
        key="env-missing-drive",
        kind="edge",
        title="便携版 + ECHODESK_DATA 指向本机不存在的盘符（受管盘未挂载）",
        root="EchoDesk-v0.2.2",
        dirs=PORTABLE_DIRS,
        env_template="{spare_drive}:{sep}EchoDeskData",
        windows_only=True,
        note="WPF 会先 CreateDirectory，失败则**静默忽略**该变量退回自动判定；后端/Avalonia 照用",
    ),

    # ---------- skip：本机跑不了的形态，如实列为未覆盖 ----------
    Scenario(
        key="linux-system-install",
        kind="skip",
        title="系统级安装（Linux：/usr/bin/EchoDesk + /usr/lib/echodesk/backend/）",
        frontends=("avalonia",),
        skip_reason="Avalonia 在这里走 XDG 分支、后端走跳过容器目录分支 —— "
                    "需要 Linux 主机才能真正执行；Windows 上跑本工具证明不了这一形态",
    ),
    Scenario(
        key="macos-app-bundle",
        kind="skip",
        title="macOS .app bundle（EchoDesk.app/Contents/MacOS/…）",
        frontends=("avalonia",),
        skip_reason="同上：ProgramDirectory 不是 app 时 Avalonia 走 ~/Library/Application Support，"
                    "需要 macOS 主机才能真正执行",
    ),
    Scenario(
        key="readonly-install-dir",
        kind="skip",
        title="装到只读位置（如机器级 C:\\Program Files\\EchoDesk）：父目录不可写",
        skip_reason="要模拟只读需改目录 ACL；本轮不引入带副作用的 ACL 操作，"
                    "因此这一形态**如实标为未覆盖**（WPF 有 IsWritable 判定，后端没有）",
    ),
]


# --------------------------------------------------------------------------- #
# 探针源码（运行时生成到临时目录，不落进仓库）
# --------------------------------------------------------------------------- #

PROBE_PROGRAM_CS = """// 由 tools/check_data_dir_consistency.py 生成：跑真实前端源文件里的数据目录算法。
// 这里**没有**任何路径逻辑的副本 —— 两个类都是被链接进来的真实实现。
using System;
using System.Text.Json;

internal static class Program
{
    private static int Main()
    {
        string? wpf = null;
        string? wpfError = null;
#if HAS_WPF
        try
        {
            wpf = EchoDesk.Services.ShortcutService.UserDataDirectory;
        }
        catch (Exception ex)
        {
            wpfError = ex.GetType().Name + ": " + ex.Message;
        }
#endif

        string? avalonia = null;
        string? avaloniaError = null;
        try
        {
            avalonia = EchoDesk.Avalonia.Services.UserPaths.DataRoot;
        }
        catch (Exception ex)
        {
            avaloniaError = ex.GetType().Name + ": " + ex.Message;
        }

        var payload = new
        {
            platform = EchoDesk.Avalonia.Services.PlatformInfo.Current.KindName,
            baseDirectory = AppContext.BaseDirectory,
            processPath = Environment.ProcessPath,
            currentDirectory = Environment.CurrentDirectory,
#if HAS_WPF
            wpfProgramDirectory = EchoDesk.Services.ShortcutService.ProgramDirectory,
#else
            wpfProgramDirectory = (string?)null,
#endif
            wpf,
            wpfError,
            avalonia,
            avaloniaError,
        };
        Console.Out.Write("__SENTINEL__" + JsonSerializer.Serialize(payload));
        return 0;
    }
}
"""

PROBE_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">

  <!-- 由 tools/check_data_dir_consistency.py 生成：链接**真实**前端源文件，不改动它们。 -->
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>{tfm}</TargetFramework>
    <Nullable>enable</Nullable>
    <LangVersion>latest</LangVersion>
    <ImplicitUsings>enable</ImplicitUsings>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <AssemblyName>{assembly}</AssemblyName>
    <RootNamespace>EchoDesk.DataDirProbe</RootNamespace>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
{define_block}
  <ItemGroup>
    <Compile Include="Program.cs" />
{compile_items}
  </ItemGroup>

</Project>
"""

BACKEND_DRIVER_PY = '''"""由 tools/check_data_dir_consistency.py 生成：跑**真实** backend/config.py 的数据目录算法。

只 import 并调用 user_data_root()，不复制它的任何逻辑；
需要"打包形态"时，在 import 之前把 sys.frozen / sys.executable 模拟成该布局里的路径。
"""
import importlib.util
import json
import os
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8-sig"))
if payload.get("frozen"):
    sys.frozen = True
    sys.executable = payload["exe"]

config_file = payload["config_file"]
spec = importlib.util.spec_from_file_location("echodesk_config_probe", config_file)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

print("__SENTINEL__" + json.dumps({
    "raw": module.user_data_root(),
    "configFile": config_file,
    "frozen": bool(payload.get("frozen")),
    "currentDirectory": os.getcwd(),
}))
'''


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = 300
        ) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def extract_payload(stdout: str) -> dict | None:
    """探针把结果放在哨兵行里，避免与其它输出混在一起。"""
    for line in reversed(stdout.splitlines()):
        if SENTINEL in line:
            return json.loads(line.split(SENTINEL, 1)[1])
    return None


def child_env(env_value: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ECHODESK_DATA", None)
    if env_value is not None:
        env["ECHODESK_DATA"] = env_value
    if extra:
        env.update(extra)
    return env


def spare_drive_letter() -> str:
    """挑一个本机不存在的盘符（用于"受管盘未挂载"这一类）。"""
    if os.name != "nt":
        return "Z"
    for code in range(ord("Z"), ord("D"), -1):
        letter = chr(code)
        if not os.path.exists(f"{letter}:\\"):
            return letter
    return "Z"


def normalize(raw: str, cwd: Path) -> str:
    """比对用的归一化：相对值按子进程的 cwd 展开，再大小写/分隔符归一（Windows）。"""
    absolute = os.path.abspath(os.path.join(str(cwd), raw))
    return os.path.normcase(os.path.normpath(absolute))


# --------------------------------------------------------------------------- #
# 两侧探针的构建与执行
# --------------------------------------------------------------------------- #

class FrontendProbe:
    """把真实 C# 源文件编译成一个探针，再把产物拷进模拟布局运行。"""

    def __init__(self, workdir: Path, dotnet: str, is_windows: bool) -> None:
        self.workdir = workdir
        self.dotnet = dotnet
        self.is_windows = is_windows
        self.has_wpf = is_windows          # WPF 前端只在 Windows 上发布
        self.project_dir = workdir / "probe"
        self.output_dir: Path | None = None
        self.assembly = PROBE_ASSEMBLY + (".dll" if not is_windows else ".exe")
        self.dotnet_version = ""

    def build(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "Program.cs").write_text(
            PROBE_PROGRAM_CS.replace("__SENTINEL__", SENTINEL), encoding="utf-8", newline="\n")

        compile_items = []
        for name, rel in SOURCE_FILES.items():
            if name == "backend":
                continue                   # Python 侧不参与 C# 编译
            if name == "wpf" and not self.has_wpf:
                continue
            path = ROOT / rel
            if not path.is_file():
                raise SystemExit(f"[错误] 找不到被测源文件：{rel}")
            compile_items.append(f'    <Compile Include="{path}" />')

        define_block = ""
        if self.has_wpf:
            define_block = ("  <PropertyGroup>\n"
                            "    <DefineConstants>$(DefineConstants);HAS_WPF</DefineConstants>\n"
                            "  </PropertyGroup>\n")

        tfm = "net10.0-windows" if self.is_windows else "net10.0"
        (self.project_dir / "Probe.csproj").write_text(
            PROBE_CSPROJ.format(tfm=tfm, assembly=PROBE_ASSEMBLY,
                                compile_items="\n".join(compile_items),
                                define_block=define_block),
            encoding="utf-8", newline="\n")

        result = run([self.dotnet, "build", str(self.project_dir / "Probe.csproj"),
                      "-c", "Release", "--nologo", "-v", "m"], cwd=self.project_dir,
                     env=child_env(None), timeout=600)
        if result.returncode != 0:
            tail = "\n".join((result.stdout or "").strip().splitlines()[-25:])
            print(tail)
            print(result.stderr.strip(), file=sys.stderr)
            raise SystemExit("[错误] 前端探针编译失败（两侧源文件未改动，问题在编译环境）")

        version = run([self.dotnet, "--version"], cwd=self.project_dir, env=child_env(None))
        self.dotnet_version = version.stdout.strip()

        candidates = list((self.project_dir / "bin" / "Release").glob(
            f"*/{PROBE_ASSEMBLY}.dll"))
        if not candidates:
            raise SystemExit("[错误] 找不到前端探针产物")
        self.output_dir = candidates[0].parent

    def sources_to_copy(self) -> list[Path]:
        assert self.output_dir is not None
        return [p for p in sorted(self.output_dir.iterdir()) if p.is_file()]

    def run_in(self, program_dir: Path, cwd: Path, env_value: str | None) -> dict:
        assert self.output_dir is not None
        target = program_dir
        for item in self.sources_to_copy():
            shutil.copy2(item, target / item.name)

        if self.is_windows:
            cmd = [str(target / (PROBE_ASSEMBLY + ".exe"))]
        else:
            cmd = [self.dotnet, str(target / (PROBE_ASSEMBLY + ".dll"))]
        result = run(cmd, cwd=cwd, env=child_env(env_value), timeout=180)
        payload = extract_payload(result.stdout)
        if payload is None:
            return {"error": f"探针没有输出结果（退出码 {result.returncode}）："
                             f"{(result.stderr or result.stdout).strip()[:400]}"}
        return payload


class BackendProbe:
    """用子进程 import 真实 backend/config.py 并调用 user_data_root()。"""

    def __init__(self, workdir: Path) -> None:
        self.driver = workdir / "backend_probe.py"
        self.driver.write_text(BACKEND_DRIVER_PY.replace("__SENTINEL__", SENTINEL),
                               encoding="utf-8", newline="\n")
        self.python = sys.executable

    def run_in(self, config_file: Path, cwd: Path, frozen: bool, exe: Path | None,
               env_value: str | None) -> dict:
        payload_file = self.driver.parent / "backend_payload.json"
        payload_file.write_text(json.dumps({
            "config_file": str(config_file),
            "frozen": frozen,
            "exe": str(exe) if exe else None,
        }, ensure_ascii=False), encoding="utf-8", newline="\n")

        result = run([self.python, str(self.driver), str(payload_file)], cwd=cwd,
                     env=child_env(env_value), timeout=180)
        payload = extract_payload(result.stdout)
        if payload is None:
            return {"error": f"探针没有输出结果（退出码 {result.returncode}）："
                             f"{(result.stderr or result.stdout).strip()[:400]}"}
        return payload


# --------------------------------------------------------------------------- #
# 场景执行
# --------------------------------------------------------------------------- #

def build_layout(workdir: Path, scenario: Scenario) -> Path:
    root = workdir / "layouts" / scenario.root
    root.mkdir(parents=True, exist_ok=True)
    for rel in scenario.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def api_label(name: str) -> str:
    return {
        "backend": "后端  config.user_data_root()",
        "wpf": "前端  WPF ShortcutService.UserDataDirectory",
        "avalonia": "前端  Avalonia UserPaths.DataRoot",
    }[name]


def short_label(name: str) -> str:
    return {"backend": "后端", "wpf": "WPF", "avalonia": "Avalonia"}[name]


def run_scenario(workdir: Path, scenario: Scenario, backend: BackendProbe,
                 frontend: FrontendProbe, spare: str) -> dict:
    root = build_layout(workdir, scenario)
    env_value = scenario.env_value(root, spare)
    program_dir = (root / scenario.program_dir).resolve()

    if scenario.backend_from_repo:
        config_file = ROOT / SOURCE_FILES["backend"]
        backend_dir = config_file.parent
    else:
        backend_dir = root / scenario.backend_dir
        config_file = backend_dir / "config.py"
        config_file.write_bytes((ROOT / SOURCE_FILES["backend"]).read_bytes())

    frozen_exe = backend_dir / ("EchoDeskBackend.exe" if os.name == "nt" else "EchoDeskBackend")
    rows: dict[str, dict] = {}

    payload = backend.run_in(config_file, cwd=root, frozen=scenario.frozen,
                            exe=frozen_exe, env_value=env_value)
    rows["backend"] = payload

    if "wpf" in scenario.frontends and frontend.has_wpf:
        front = frontend.run_in(program_dir, cwd=root, env_value=env_value)
        rows["wpf"] = ({"raw": front["wpf"]} if front.get("wpf") is not None
                       else {"error": front.get("wpfError") or front.get("error")})
        rows["avalonia"] = ({"raw": front["avalonia"]} if front.get("avalonia") is not None
                            else {"error": front.get("avaloniaError") or front.get("error")})
    else:
        front = frontend.run_in(program_dir, cwd=root, env_value=env_value)
        rows["avalonia"] = ({"raw": front["avalonia"]} if front.get("avalonia") is not None
                            else {"error": front.get("avaloniaError") or front.get("error")})

    compared = ["backend"] + [name for name in ("wpf", "avalonia") if name in rows]
    keys: dict[str, str | None] = {}
    absolute: dict[str, bool] = {}
    for name in compared:
        row = rows[name]
        raw = row.get("raw")
        if raw is None:
            keys[name] = None
            absolute[name] = False
        else:
            keys[name] = normalize(raw, root)
            absolute[name] = os.path.isabs(raw)

    errors = [name for name in compared if rows[name].get("error")]
    consistent = (not errors
                  and len({keys[name] for name in compared}) == 1
                  and all(absolute[name] for name in compared))

    return {
        "key": scenario.key,
        "kind": scenario.kind,
        "title": scenario.title,
        "note": scenario.note,
        "env": env_value,
        "envSet": env_value is not None,
        "root": str(root),
        "programDir": str(program_dir),
        "backendConfig": str(config_file),
        "frozen": scenario.frozen,
        "frontends": [name for name in ("wpf", "avalonia") if name in rows],
        "results": {name: {"raw": rows[name].get("raw"),
                           "normalized": keys.get(name),
                           "absolute": absolute.get(name, False),
                           "error": rows[name].get("error")}
                    for name in compared},
        "consistent": consistent,
    }


# --------------------------------------------------------------------------- #
# 打印
# --------------------------------------------------------------------------- #

def shorten(text: str | None, result: dict) -> str:
    if text is None:
        return "（无）"
    out = text
    for prefix, label in ((result.get("root"), "<布局>"),):
        if prefix:
            out = out.replace(prefix, label)
    return out


def print_report(result: dict) -> None:
    verdict = "一致" if result["consistent"] else "不一致"
    env_text = ("（未设置）" if not result["envSet"]
                else f"ECHODESK_DATA={result['env']!r}")
    print(f"  {env_text}")
    if result["kind"] == "edge" and not result["consistent"]:
        # 边界项的差异要显式指出"只报告"，避免被当成 contract 失败
        pass
    for name, row in result["results"].items():
        if row["error"]:
            print(f"    {api_label(name):<46} 运行失败：{row['error']}")
        else:
            print(f"    {api_label(name):<46} = {shorten(row['raw'], result)}")
    print(f"    结论：{verdict}")
    if not result["consistent"]:
        if result["note"]:
            print(f"    成因：{result['note']}")
        detail = []
        for name, row in result["results"].items():
            if row["error"]:
                detail.append(f"{name}=运行失败")
                continue
            flag = "" if row["absolute"] else "（非绝对路径 → 随当前工作目录漂移）"
            detail.append(f"{name}={row['normalized'] or row['raw']}{flag}")
        print("    差异：" + " | ".join(detail))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验后端 config.user_data_root() 与前端 UserPaths.DataRoot / "
                    "ShortcutService.UserDataDirectory 在不同模拟环境里是否算出同一个目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fail-on-edge", action="store_true",
                        help="边界项（edge）不一致也计入退出码（默认只报告）")
    parser.add_argument("--json", metavar="PATH", help="把结果另存为 JSON（供记录/后续门禁引用）")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录，便于排查")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 老解释器没有 reconfigure 就按默认输出
        pass

    for rel in SOURCE_FILES.values():
        if not (ROOT / rel).is_file():
            print(f"[错误] 找不到被测源文件：{rel}", file=sys.stderr)
            return 2

    dotnet = shutil.which("dotnet")
    if dotnet is None:
        print("[错误] 本机没有 dotnet SDK，无法编译前端侧的真实实现；"
              "本工具不做「复刻算法」式降级（那会随两侧改动静默漂移）。", file=sys.stderr)
        return 2

    is_windows = os.name == "nt"
    workdir = Path(tempfile.mkdtemp(prefix="echodesk-datadir-"))
    exit_code = 2
    try:
        print("== 前后端数据目录一致性门禁 ==")
        print(f"仓库：{ROOT}")
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, env=child_env(None))
        dirty = run(["git", "status", "--porcelain"], cwd=ROOT, env=child_env(None))
        print(f"HEAD：{head.stdout.strip() or '(未知)'}"
              f"{'  工作区有未提交改动' if dirty.stdout.strip() else ''}")
        print(f"宿主：{sys.platform}（python {sys.version.split()[0]}）"
              f"{'；WPF 前端仅 Windows 有，非 Windows 上不参与对照' if not is_windows else ''}")
        print("被测算法源文件（本工具只读，不修改）：")
        for name, rel in SOURCE_FILES.items():
            print(f"  {name:<18} {rel}  sha256={sha256_of(ROOT / rel)[:16]}…")
        print()
        print("构建前端侧探针（链接真实 C# 源文件）…")
        frontend = FrontendProbe(workdir, dotnet, is_windows)
        frontend.build()
        print(f"  dotnet {frontend.dotnet_version}；产物 {frontend.output_dir}")
        backend = BackendProbe(workdir)
        print(f"  后端探针解释器：{backend.python}")
        print()

        spare = spare_drive_letter()
        results = []
        for kind, heading in (("contract", "一、发布布局（必须一致，不一致即失败）"),
                              ("edge", "二、边界输入与源码检出（只报告，不参与退出码）"),
                              ("skip", "三、本机无法执行的形态（如实列为未覆盖）")):
            subset = [s for s in SCENARIOS if s.kind == kind]
            print(f"{heading}")
            print("-" * 72)
            for scenario in subset:
                print(f"[{scenario.key}] {scenario.title}")
                if kind == "skip":
                    print(f"    未执行：{scenario.skip_reason}")
                    print()
                    results.append({"key": scenario.key, "kind": kind,
                                    "title": scenario.title, "skip": scenario.skip_reason})
                    continue
                if scenario.windows_only and not is_windows:
                    reason = "Windows 专属形态（盘符/盘未挂载），本机不是 Windows"
                    print(f"    未执行：{reason}")
                    print()
                    results.append({"key": scenario.key, "kind": "skip",
                                    "title": scenario.title, "skip": reason})
                    continue
                if "wpf" in scenario.frontends and not frontend.has_wpf:
                    print("    说明：WPF 前端只在 Windows 上发布，本机不参与对照")
                outcome = run_scenario(workdir, scenario, backend, frontend, spare)
                results.append(outcome)
                print_report(outcome)

        contract = [r for r in results if r["kind"] == "contract"]
        edge = [r for r in results if r["kind"] == "edge"]
        skipped = [r for r in results if r["kind"] == "skip"]
        bad_contract = [r for r in contract if not r["consistent"]]
        bad_edge = [r for r in edge if not r["consistent"]]

        print("=" * 72)
        print(f"发布布局：{len(contract) - len(bad_contract)}/{len(contract)} 一致")
        for row in bad_contract:
            print(f"  ✗ 不一致：{row['key']} —— {row['title']}")
        print(f"边界/开发形态：{len(edge) - len(bad_edge)}/{len(edge)} 一致；"
              f"{len(bad_edge)} 项差异（只报告，逐项成因见上）")
        for row in bad_edge:
            values = "，".join(f"{short_label(name)}={item.get('raw')!r}"
                              for name, item in row["results"].items())
            print(f"  ! {row['key']}：{values}")
        print(f"未覆盖（本机无法执行）：{len(skipped)} 项 —— "
              + "、".join(r["key"] for r in skipped))

        if bad_contract:
            print("\n判定：不一致 —— 发布布局下两侧算出的数据目录不同，见上面的差异明细。")
            print("      本工具只报告，不修改任何一侧的算法（任务书硬性要求）。")
            exit_code = 1
        elif args.fail_on_edge and bad_edge:
            print("\n判定：不一致（--fail-on-edge）—— 边界项存在差异，见上。")
            exit_code = 1
        else:
            print("\n判定：一致 —— 发布布局下两侧算出的数据目录相同。")
            if bad_edge:
                print("      注意：边界/开发形态仍有差异（上面已逐项列出），"
                      "本轮按「只报告」处理；是否修改需维护者裁定。")
            if skipped:
                print("      注意：被跳过的形态**没有被证明一致**（跨平台分支只能在对应平台上执行）。")
            exit_code = 0

        if args.json:
            payload = {
                "head": head.stdout.strip(),
                "workdir": str(workdir),
                "host": sys.platform,
                "sources": {name: {"path": rel, "sha256": sha256_of(ROOT / rel)}
                            for name, rel in SOURCE_FILES.items()},
                "results": results,
                "ok": exit_code == 0,
            }
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8", newline="\n")
            print(f"\n结果已写入：{args.json}")
    finally:
        if args.keep_temp:
            print(f"\n临时目录保留在：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
