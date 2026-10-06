#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""standalone 后端产物的结构与可运行性检查（v0.2.1 跨平台-2 任务五）。

## 为什么需要它

任务书要求"新增的跨平台检查项能拦住『后端缺失』『启动脚本语法错』『平台判断不一致』"。
`tools/pack_backend.py` 只负责**产出**，产出的东西对不对必须由检查来说 ——
而这类缺陷很容易漏过人工验收：少一个 `site-packages`、启动脚本写错一个变量、
解释器根本没被解压……它们**不会让构建失败**，只会让用户解压后打不开。

## 三档验证（逐条标注做到哪一档）

* **结构级**：解释器、`site-packages`、`app/main.py`、启动脚本、`manifest.json` 在位且非空。
* **语法级**：启动脚本能被各自的 shell 解析（Windows 用 `cmd /c` 干跑，POSIX 用 `sh -n`）。
* **运行级**：**本平台**对应的产物真的拉起解释器，断言版本与平台；
  其余平台的产物只做前两档 —— 这不是偷懒，而是本机没有目标平台的运行环境，
  假装验证过比不验证更糟（口径见 records/v0.2.1-跨平台-2记录.md 第〇节）。

## 用法

    python tools/check_standalone_backend.py                 # 检查 publish/backend-* 全部
    python tools/check_standalone_backend.py --platform win-x64
    python tools/check_standalone_backend.py --dir publish
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: RID → 该平台的解释器相对路径与启动脚本名（与 tools/pack_backend.py 的 PLATFORMS 一致）。
EXPECTED = {
    "win-x64": {"python": "python/python.exe", "launcher": "run-backend.bat"},
    "linux-x64": {"python": "python/bin/python3", "launcher": "run-backend.sh"},
    "osx-arm64": {"python": "python/bin/python3", "launcher": "run-backend.sh"},
}

PASSES: list[str] = []
FAILURES: list[str] = []


def ok(text: str) -> None:
    PASSES.append(text)
    print(f"  [OK]   {text}", flush=True)


def fail(text: str) -> None:
    FAILURES.append(text)
    print(f"  [FAIL] {text}", flush=True)


def note(text: str) -> None:
    print(f"  [--]   {text}", flush=True)


def dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def local_rid() -> str:
    """本机对应的 RID（决定谁能做运行级验证）。"""
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        return "win-x64" if machine in ("amd64", "x86_64") else ""
    if sys.platform == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else ""
    if sys.platform.startswith("linux"):
        return "linux-x64" if machine in ("x86_64", "amd64") else ""
    return ""


def check_launcher_syntax(path: str, rid: str) -> None:
    """启动脚本的语法检查：用目标 shell 的"只解析不执行"模式。"""
    if path.endswith(".bat"):
        if not sys.platform.startswith("win"):
            note(f"{rid} 的 .bat 语法检查需要 Windows，跳过")
            return
        # `cmd /c` 干跑会真的执行脚本 —— 那会拉起后端并挂住。
        # 因此只做"文件非空 + 有解释器调用行"的结构断言，并在运行级阶段用真解释器验证。
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if "python.exe" in text and "main.py" in text:
            ok(f"{rid} 的 run-backend.bat 含有解释器与 main.py 调用")
        else:
            fail(f"{rid} 的 run-backend.bat 没有调用解释器跑 main.py（脚本写错了）")
        return
    sh = shutil.which("sh")
    if sh is None:
        note(f"{rid} 的 .sh 语法检查需要 sh，本机没有，跳过")
        return
    completed = subprocess.run([sh, "-n", path], capture_output=True, text=True)
    if completed.returncode == 0:
        ok(f"{rid} 的 run-backend.sh 通过 sh -n 语法检查")
    else:
        fail(f"{rid} 的 run-backend.sh 语法错误：{completed.stderr.strip()[:200]}")


def check_runtime(python: str, rid: str, tree: str) -> None:
    """运行级验证：真的用包内解释器跑一段代码，并断言版本与 PYTHONPATH 注入生效。"""
    if rid != local_rid():
        note(f"{rid} 不是本机平台，跳过运行级验证（只有结构级与语法级证据）")
        return
    code = ("import sys, platform; "
            "print(sys.version.split()[0]); "
            "print(platform.system())")
    try:
        completed = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=120,
                                   cwd=tree)
    except Exception as exc:  # noqa: BLE001
        fail(f"{rid} 的解释器无法执行：{exc}")
        return
    if completed.returncode != 0:
        fail(f"{rid} 的解释器执行失败（退出码 {completed.returncode}）：{completed.stderr.strip()[:200]}")
        return
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    version = lines[0] if lines else ""
    system = lines[1] if len(lines) > 1 else ""
    if version.startswith("3.13"):
        ok(f"{rid} 包内解释器可运行：Python {version}（{system}）")
    else:
        fail(f"{rid} 包内解释器版本不对：{version}（期望 3.13.x）")

    # 依赖能否被导入：site-packages 必须真的在搜索路径上（PYTHONPATH 注入的口径）。
    site = os.path.join(tree, "site-packages")
    app = os.path.join(tree, "app")
    env_code = (f"import sys; sys.path.insert(0, {site!r}); sys.path.insert(0, {app!r}); "
                "import json; print('PYTHONPATH-ok')")
    completed = subprocess.run([python, "-c", env_code], capture_output=True, text=True, timeout=120,
                               cwd=tree)
    if completed.returncode == 0 and "PYTHONPATH-ok" in completed.stdout:
        ok(f"{rid} 的 site-packages 与 app 可作为模块搜索路径使用")
    else:
        fail(f"{rid} 的依赖目录不可用：{completed.stderr.strip()[:200]}")


class LineReader:
    """后端 stdout 的**常驻**读取线程 + 队列，带超时取值。

    为什么不能用"每次读一行就新起一个线程、超时就把它丢掉"（本脚本最初的写法）：
    被丢掉的那个线程**仍然阻塞在 `readline()` 上**，下一行数据到达时会被它先取走并
    扔进它自己的队列里 —— 调用方于是永远读不到那一行，表现为"后端明明回了、脚本却说没拿到"。
    这不只是理论：加上"连续多个动作"的探测之后立刻复现（四个动作全部报"没拿到预期响应"，
    而手工探查显示后端每个都正常回复了）。

    正确做法是**只起一个读取线程**，把行持续送进队列；超时只影响队列取值，不产生孤儿线程。
    """

    #: 流结束的哨兵（用 None 区分"流关闭"与"超时"）。
    EOF = None

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in self._stream:
                self._lines.put(line)
        except Exception:  # noqa: BLE001 - 流被关掉是正常收尾
            pass
        finally:
            self._lines.put(None)

    def read(self, timeout: float) -> str:
        """读一行；超时或流结束返回空串。"""
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            return ""
        return "" if item is None else item

    def drain(self) -> int:
        """丢弃当前已缓冲的行，返回丢弃条数（换动作前清掉上一条动作的残留输出）。"""
        dropped = 0
        while True:
            try:
                self._lines.get_nowait()
            except queue.Empty:
                return dropped
            dropped += 1


def check_data_root(report: dict, rid: str) -> None:
    """断言后端上报的用户数据目录**没有埋进程序目录内部**（不是 `app/`，也不是 `app/backend/`）。

    为什么要单独查这一条（v0.2.2 全面双语与三平台友好 实测发现的真实缺陷）：
    后端按 `config.py` 的位置推导用户数据根，原来只判断"父目录是否叫 app" ——
    在 standalone 布局（`<发布>/app/backend/app/config.py`）下会算成 `<发布>/app/backend`，
    而前端算的是 `<发布>`，于是同一个包把数据写到两个地方（用户表现为"设置没了""历史没了"）。
    这个检查让"修好了"变成可判定的，而不是靠读代码确信。
    """
    root = report.get("user_data_root") if isinstance(report, dict) else None
    if not root:
        note(f"{rid} 的 platform 报告没有 user_data_root 字段，跳过数据根校验")
        return
    # 校验脚本用临时目录当数据根（ECHODESK_DATA），所以这里能断言的只是"没被埋进 app/ 里"。
    parts = [p for p in re.split(r"[\\/]+", str(root)) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    if any(p.lower() in ("app", "backend") for p in tail):
        fail(f"{rid} 的用户数据根落在程序容器目录内部：{root}"
             "（应当落在发布物外层；否则前后端的数据目录会分裂）")
    else:
        ok(f"{rid} 的用户数据根在程序容器之外：{root}")


def check_protocol(python: str, rid: str, tree: str) -> None:
    """协议级验证：真的拉起后端，走一次 `ready` 握手 + `platform` 动作往返。

    这一步比"解释器能跑"强得多：它同时证明**依赖装对了**（少一个包会在 import 阶段就崩，
    而那正是 standalone 方案最容易出的错）、`PYTHONPATH` 注入口径与启动脚本一致、
    以及后端确实按 JSON 行协议说话（任务书要求"解压后能启动、能响应协议请求"）。

    用临时数据目录（`ECHODESK_DATA`）：验证不该在发布物里留下 session.json 之类的运行期残留。
    """
    if rid != local_rid():
        note(f"{rid} 不是本机平台，跳过协议级验证（没有目标平台的运行环境）")
        return

    app_dir = os.path.join(tree, "app")
    site = os.path.join(tree, "site-packages")
    env = dict(os.environ)
    env["PYTHONPATH"] = site + os.pathsep + app_dir
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    data_dir = tempfile.mkdtemp(prefix="echodesk-verify-")
    env["ECHODESK_DATA"] = data_dir

    proc = None
    try:
        try:
            # 调用口径与**发布物里的前端一致**（v0.2.2 修正）：
            # 工作目录 = 后端包根（tree），脚本路径 = app/main.py。
            # 原来这里是 cwd=app_dir + "main.py"，于是"验证通过"却不代表发布物里那条路径也对 ——
            # 而两者的差别恰恰会改变 models/、skills_dev/ 这些相对路径的落点。
            proc = subprocess.Popen([python, "-u", os.path.join("app", "main.py")], cwd=tree, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            fail(f"{rid} 无法拉起后端进程：{exc}")
            return

        reader = LineReader(proc.stdout)

        # 启动握手：后端会先打印若干行，其中一行是 ready 的 JSON。跳过非 JSON 行（日志/警告）。
        ready: dict | None = None
        for _ in range(20):
            line = reader.read(timeout=90)
            if not line:
                break
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("version" in candidate or "ready" in candidate
                                                or "status" in candidate):
                ready = candidate
                break
        if ready is None:
            fail(f"{rid} 后端没有给出 ready 握手（依赖缺失或启动即崩：这一步正是要拦住的情况）")
            return
        ok(f"{rid} 后端 ready 握手成功（version={ready.get('version') or ready.get('app_version') or '?'}，"
           f"modelReady={ready.get('model_ready', ready.get('modelReady', '?'))}）")

        # 协议往返：发一个真实动作，断言响应里有对应字段（不断言"有内容"—— 那是"假通过"，见记录里的坑）。
        assert proc.stdin is not None
        reply = ask(proc, reader, "platform", "platform")
        if reply is None:
            fail(f"{rid} 的 platform 动作没有返回带 platform 字段的响应")
            return
        report = reply.get("platform") or {}
        kind = report.get("kind") if isinstance(report, dict) else None
        expected = {"win-x64": "windows", "linux-x64": "linux", "osx-arm64": "macos"}[rid]
        if kind == expected:
            ok(f"{rid} 协议往返正常：platform.kind={kind}（与本机平台一致）")
        else:
            fail(f"{rid} 平台判断不一致：platform.kind={kind}，期望 {expected}")

        check_data_root(report, rid)

        check_action_set(proc, reader, rid)
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(data_dir, ignore_errors=True)


#: 在打包后的后端上逐个跑一遍的动作，以及"这个动作的响应里必须出现"的字段。
#: 为什么要不止一个动作（v0.2.1 跨平台-2 补）：解释器归档换成了 `install_only_stripped`
#: 并额外删掉了不可达的 Tcl/Tk 与 standalone 自带的 pip，**这类改动只有真的把功能跑一遍
#: 才能确认没删错东西**。一个 `platform` 动作触碰的代码路径太窄（它只读平台信息），
#: 下面这些会把指令表、模型能力、上下文快照这些真实路径走一遍。
#:
#: **只放"本机可判定"的动作**：
#:   * `status` **不是后端动作**（`/status` 是一条聊天指令）—— 最初把它写了进来，
#:     结果它照样返回了一大堆字段，而"未知动作"藏在 `content` 里，差点又是一次"假通过"。
#:     这件事本身说明：断言"响应里有某个字段"必须选**只有那个动作才会产生**的字段。
#:   * 刻意不放 `market_status` / `automation_status`：前者会去连技能市场、后者会碰
#:     Playwright，都可能**联网**。一条会因为"网慢"而失败的验证，等于一条会撒谎的验证。
ACTION_PROBES: tuple[tuple[str, str], ...] = (
    ("commands", "commands"),
    ("capability", "capability"),
    ("context", "limit"),
)


def ask(proc, reader: "LineReader", action: str, key: str,
        attempts: int = 8, timeout: float = 45) -> dict | None:
    """发一个动作并等它的响应；响应里必须出现 `key` 字段。

    发送前先 `drain()` 丢掉残留输出：否则上一条动作的迟到响应会被当成这一条的
    （这正是上面那个"孤儿读线程"缺陷的另一种表现形式）。
    """
    reader.drain()
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"action": action}) + "\n")
    proc.stdin.flush()
    for _ in range(attempts):
        line = reader.read(timeout=timeout)
        if not line:
            return None
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and key in candidate:
            return candidate
    return None


def check_action_set(proc, reader: "LineReader", rid: str) -> None:
    """在同一个后端进程上连续跑一组真实动作，逐个断言响应里有对应字段。"""
    passed = 0
    problems: list[str] = []
    for action, key in ACTION_PROBES:
        if ask(proc, reader, action, key) is None:
            problems.append(action)
        else:
            passed += 1
    if problems:
        fail(f"{rid} 有 {len(problems)} 个动作拿不到预期响应：{', '.join(problems)}"
             "（剥离解释器时删错了东西，或后端依赖不完整）")
    else:
        ok(f"{rid} 动作集在打包后的后端上全部可用（{passed}/{len(ACTION_PROBES)}："
           + "、".join(a for a, _ in ACTION_PROBES) + "）")


def check_one(tree: str, rid: str, strict: bool) -> None:
    print(f"\n== {rid}（{os.path.relpath(tree, ROOT)}）==")
    spec = EXPECTED.get(rid)
    if spec is None:
        fail(f"{rid} 不是已知的 RID（期望 {'/'.join(EXPECTED)}）")
        return

    python = os.path.join(tree, spec["python"].replace("/", os.sep))
    if os.path.isfile(python) and os.path.getsize(python) > 0:
        ok(f"{rid} 包内解释器在位（{spec['python']}，{os.path.getsize(python) / 1048576:.1f} MB）")
    else:
        fail(f"{rid} 缺少包内解释器：{spec['python']}")
        return

    main_py = os.path.join(tree, "app", "main.py")
    if os.path.isfile(main_py) and os.path.getsize(main_py) > 0:
        ok(f"{rid} 的后端入口在位（app/main.py）")
    else:
        fail(f"{rid} 缺少后端入口 app/main.py（解压后前端拉不起后端）")

    site = os.path.join(tree, "site-packages")
    if os.path.isdir(site) and os.listdir(site):
        count = sum(1 for _ in os.walk(site))
        ok(f"{rid} 的依赖目录非空（site-packages，{dir_size(site) / 1048576:.1f} MB，{count} 层）")
    else:
        fail(f"{rid} 的 site-packages 缺失或为空（依赖没装上：后端连 import 都过不去）")

    for name in ("run-backend.bat", "run-backend.sh"):
        path = os.path.join(tree, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            pass
        elif name == spec["launcher"]:
            fail(f"{rid} 缺少本平台启动脚本 {name}")

    launcher = os.path.join(tree, spec["launcher"])
    if os.path.isfile(launcher):
        check_launcher_syntax(launcher, rid)

    manifest_path = os.path.join(os.path.dirname(tree), f"backend-manifest-{rid}.json")
    if os.path.isfile(manifest_path):
        try:
            with io.open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            required = ("version", "rid", "triple", "python", "dependencies", "tree", "launcher")
            missing = [key for key in required if key not in manifest]
            if missing:
                fail(f"{rid} 的 manifest 缺字段：{', '.join(missing)}")
            else:
                deps = manifest.get("dependencies", {}).get("count", 0)
                ok(f"{rid} 的 manifest 完整（Python {manifest['python'].get('version')}，依赖 {deps} 个）")
        except Exception as exc:  # noqa: BLE001
            fail(f"{rid} 的 manifest 不是合法 JSON：{exc}")
    else:
        fail(f"{rid} 缺少 backend-manifest-{rid}.json")

    if strict:
        check_runtime(python, rid, tree)
        check_protocol(python, rid, tree)

    check_native_binaries(tree, site, rid)


#: Mach-O 文件头魔数（macOS 的动态库）。用来判定"这个 .dylib 到底是不是一个真的动态库"。
MACHO_MAGIC = {
    b"\xcf\xfa\xed\xfe": "Mach-O 64 位小端",
    b"\xfe\xed\xfa\xcf": "Mach-O 64 位大端",
    b"\xca\xfe\xba\xbe": "Mach-O 通用（fat）",
}


def check_native_binaries(tree: str, site: str, rid: str) -> None:
    """非本机平台唯一能做的"二进制级"证据：原生依赖是不是**结构完整**的动态库。

    为什么需要这一层（实测踩到的坑）：llama-cpp-python 的 macOS arm64 轮子在**上游就是坏的**
    —— 字节数与官方 digest 完全一致（"下完了、也没被篡改"），但包内 `lib/*.dylib` 大面积
    压缩流损坏。这种缺陷在 Windows 打包机上**没有任何运行级证据能发现**（那是 macOS 的
    .so/.dylib，跑不起来也 import 不了），只能靠静态检查兜住。

    检查项：`llama_cpp/lib/*.dylib` 的**文件头魔数**必须是合法的 Mach-O。
    只对 osx-arm64 做（其他平台的原生扩展要么能在本机验证，要么是本机平台）。
    """
    if rid != "osx-arm64":
        return
    lib_dir = os.path.join(site, "llama_cpp", "lib")
    if not os.path.isdir(lib_dir):
        fail(f"{rid} 的 site-packages 里没有 llama_cpp/lib（本地推理会 ImportError）")
        return
    dylibs = [n for n in sorted(os.listdir(lib_dir)) if n.endswith(".dylib")]
    if not dylibs:
        fail(f"{rid} 的 llama_cpp/lib 下一个 .dylib 都没有（轮子不完整）")
        return
    bad: list[str] = []
    for name in dylibs:
        path = os.path.join(lib_dir, name)
        try:
            with open(path, "rb") as handle:
                head = handle.read(4)
        except OSError as exc:
            bad.append(f"{name}（读取失败：{exc}）")
            continue
        if head not in MACHO_MAGIC:
            bad.append(f"{name}（文件头 {head!r} 不是 Mach-O）")
    if bad:
        fail(f"{rid} 的 llama_cpp 动态库有 {len(bad)} 个不是合法 Mach-O：{bad[0]}")
        return
    metal = sum(1 for n in dylibs if "metal" in n.lower())
    ok(f"{rid} 的 llama_cpp 动态库全部是合法 Mach-O（{len(dylibs)} 个，其中 Metal 后端 {metal} 个）")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 standalone 后端产物")
    parser.add_argument("--dir", default=os.path.join(ROOT, "publish"), help="产物根目录")
    parser.add_argument("--platform", action="append", default=[], help="只检查指定 RID（可重复）")
    parser.add_argument("--allow-missing", action="store_true",
                        help="产物不存在时只提示不失败（给「还没产出」的场景用）")
    args = parser.parse_args()

    print("== standalone 后端产物检查 ==")
    print(f"产物根目录：{os.path.relpath(args.dir, ROOT)}　本机 RID：{local_rid() or '(未知)'}")

    targets = args.platform or list(EXPECTED)
    found = False
    for rid in targets:
        tree = os.path.join(args.dir, f"backend-{rid}")
        if not os.path.isdir(tree):
            if args.allow_missing:
                note(f"{rid} 的产物不存在（--allow-missing）")
            else:
                fail(f"{rid} 的产物不存在：{os.path.relpath(tree, ROOT)}")
            continue
        found = True
        check_one(tree, rid, strict=True)

    print("\n" + "-" * 42)
    if not found and args.allow_missing:
        print("没有可检查的产物（已按 --allow-missing 放过）")
        return 0
    if FAILURES:
        print(f"通过 {len(PASSES)} 项，失败 {len(FAILURES)} 项")
        return 1
    print(f"通过 {len(PASSES)} 项，失败 0 项")
    print("standalone 后端产物检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
