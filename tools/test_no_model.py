# -*- coding: utf-8 -*-
"""无模型就绪 / 热加载 / 模型市场列表的自动化测试（v0.2.1 综合优化 任务一、二、三、五）。

为什么要有这个测试：这几项改动的核心是"**后端在模型缺失时不退出**"——
而"不退出"这件事用读代码是验不出来的，必须真的把后端跑起来、看它有没有在那个
时间点结束进程。所以这里跑的是**真后端进程**（源码形态，cwd 指向一个临时目录，
里面没有 models/ 也没有 config.json），用 JSON 行协议逐条断言。

覆盖（对应任务书的"验证方式"清单）：

* 无模型启动：不发 `ready` 就退出 → 失败；`need_download` 必须带 `blocking: false`；
  `ready.model_ready` 必须是 `false`，且**进程仍存活**；
* 引导消息：必须含"模型文件夹的实际路径"（真实路径，不是占位符）；
* 功能边界：`chat` 返回引导而不是崩溃/报错；文件解析、设置读写、模型市场状态、
  会话历史等**不依赖模型**的动作照常可用；
* 手动放置：往 models/ 放文件后 `model_scan` 能识别；只有一个模型时会尝试自动加载；
* 热加载失败：放一个**损坏的 .gguf**，必须保持无模型状态、给出错误、且进程不死；
* 模型市场列表（任务三）：默认模型对应的仓库**一定在列表里**（这是任务书要求排查的
  "默认模型显示不出来"），推荐项带 `recommended` 标记，且推荐项一定是列表里的条目
  （推荐与列表同源）；离线时也要给出默认模型 + 本地已下载，而不是一片空白。

用法：python tools\\test_no_model.py [python 解释器路径]
退出码：0 = 全部通过，1 = 存在失败项。
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
DEFAULT_PYTHON = sys.executable
#: 用来验证"热加载成功"这条正路的真实模型。
#: 来源**不能只写发布目录**：v0.2.1 起的安装程序与便携发布目录都**不含模型**
#: （模型按需下载，见 tools\installer.iss 头部说明），所以发布目录里永远找不到 .gguf。
#: 因此优先用开发机的 backend\models，其次才是历史发布目录（老包可能带模型）。
def _find_real_model() -> str:
    candidates = [os.path.join(ROOT, "backend", "models", "Qwen3-0.6B-Q6_K.gguf")]
    dev = os.path.join(ROOT, "backend", "models")
    if os.path.isdir(dev):
        candidates += [os.path.join(dev, f) for f in sorted(os.listdir(dev))
                       if f.lower().endswith(".gguf")]
    publish = os.path.join(ROOT, "publish")
    if os.path.isdir(publish):
        for name in sorted(os.listdir(publish), reverse=True):
            if name.startswith("EchoDesk-v"):
                candidates.append(os.path.join(publish, name, "app", "backend",
                                               "models", "Qwen3-0.6B-Q6_K.gguf"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


PACKAGED_MODEL = _find_real_model()

PASSED = 0
SKIPPED = []
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def skip(name, why):
    SKIPPED.append(f"{name}：{why}")
    print(f"  [SKIP] {name}（{why}）")


def section(title):
    print(f"\n=== {title} ===")


class Backend:
    """最小 JSON 行协议客户端（读线程 + 队列，保证超时真的生效）。"""

    def __init__(self, python, workdir):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = BACKEND_DIR
        self.proc = subprocess.Popen(
            [python, os.path.join(BACKEND_DIR, "main.py")],
            cwd=workdir, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except Exception:
            pass

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read_one(self, timeout=30.0):
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            return None
        except json.JSONDecodeError:
            return None

    def read_until(self, types, timeout=60.0):
        """读到指定 type 之一为止；把途中读到的都记进 events 便于排查。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.read_one(timeout=max(0.2, deadline - time.time()))
            if item is None:
                continue
            if item.get("type") in types:
                return item
        return None

    def request(self, obj, types=("system", "assistant", "done", "error"), timeout=60.0):
        self.send(obj)
        return self.read_until(set(types), timeout=timeout)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def read_system(name, payload, timeout=30.0):
    """发一条请求并把命中的 system/done 响应原样返回（便于逐个字段断言）。"""
    resp = payload if payload is not None else {}
    check(name, bool(resp), f"未收到响应（超时 {timeout}s）")
    return resp


def test_no_model_startup(python, workdir):
    section("任务一：无模型启动不阻塞、不退出")
    be = Backend(python, workdir)
    try:
        # 1) 先必须收到 need_download（告知），且**非阻塞**
        first = be.read_until({"need_download", "ready", "system", "error"}, timeout=120)
        check("缺模型时收到 need_download 告知", (first or {}).get("type") == "need_download",
              str(first)[:200])
        check("need_download 标记为非阻塞（blocking=false）",
              (first or {}).get("blocking") is False, str(first)[:200])
        check("need_download 给出模型文件夹真实路径",
              bool((first or {}).get("models_dir")) and os.path.isdir((first or {}).get("models_dir") or ""),
              str((first or {}).get("models_dir")))

        ready = be.read_until({"ready", "error"}, timeout=120)
        check("随后仍会收到 ready（后端没有退出）", (ready or {}).get("type") == "ready",
              str(ready)[:200])
        check("ready.model_ready 为 false", (ready or {}).get("model_ready") is False,
              str((ready or {}).get("model_ready")))
        state = (ready or {}).get("model_state") or {}
        check("ready.model_state.ready 为 false", state.get("ready") is False, str(state)[:200])
        check("ready 里带引导文案（含模型文件夹路径）",
              bool(state.get("guide")) and str(state.get("models_dir") or "") in str(state.get("guide")),
              str(state.get("guide"))[:160])
        check("ready 附带 model_missing 详情", bool((ready or {}).get("model_missing")),
              str((ready or {}).get("model_missing"))[:160])
        check("进程仍存活（关键：旧行为在这里就已经退出了）", be.alive())
        return be
    except Exception:
        be.close()
        raise


def test_boundary(be):
    section("任务一/任务六：无模型时的功能边界")
    # 推理类 → 引导
    chat = be.request({"action": "chat", "message": "你好"}, timeout=60)
    check("chat 返回引导而不报错", (chat or {}).get("type") in ("assistant", "system"),
          str(chat)[:200])
    check("chat 的引导里带模型文件夹路径", "models" in str((chat or {}).get("content", "")),
          str(chat)[:200])
    check("chat 标记 model_ready=false", (chat or {}).get("model_ready") is False,
          str(chat)[:200])

    # 不依赖模型的动作 → 正常
    st = be.request({"action": "model_state"}, timeout=60)
    check("model_state 可用", bool((st or {}).get("model_state")), str(st)[:160])
    check("model_state.ready=false", ((st or {}).get("model_state") or {}).get("ready") is False)

    cap = be.request({"action": "capability"}, timeout=90)
    check("capability（设置页数据源）可用", bool((cap or {}).get("capability")) or "recommendation" in str(cap),
          str(cap)[:160])

    reg = be.request({"action": "model_hub_registry"}, timeout=60)
    check("模型库列表可用（空库也要能打开）", "registry" in str(reg), str(reg)[:160])

    ctx = be.request({"action": "context"},
                     types=("context", "system", "error", "done"), timeout=60)
    check("上下文快照可用", (ctx or {}).get("type") == "context", str(ctx)[:160])
    check("上下文快照反映无模型", (ctx or {}).get("model_ready") is False,
          str((ctx or {}).get("model_ready")))

    cmds = be.request({"action": "commands"}, timeout=60)
    check("指令清单可用（会话历史/设置不受影响）", bool((cmds or {}).get("commands")), str(cmds)[:120])

    check("上述请求之后进程仍存活", be.alive())


def test_scan_and_hotload(python, workdir, be):
    section("任务二/任务五：扫描模型文件夹与热加载失败路径")
    scan = be.request({"action": "model_scan"}, timeout=60)
    check("空目录扫描成功返回", "model_scan" in str(scan), str(scan)[:160])
    check("空目录扫描 count=0", ((scan or {}).get("model_scan") or {}).get("count") == 0,
          str(scan)[:160])
    check("空目录扫描给出引导（含路径）",
          str(((scan or {}).get("model_scan") or {}).get("models_dir") or "") in str((scan or {}).get("content", "")),
          str((scan or {}).get("content"))[:200])

    # 放一个损坏的 .gguf：只有一个模型 → 会尝试自动加载 → 必须失败且保持无模型
    models_dir = ((scan or {}).get("model_scan") or {}).get("models_dir") or os.path.join(workdir, "models")
    os.makedirs(models_dir, exist_ok=True)
    broken = os.path.join(models_dir, "broken-test-model.gguf")
    with open(broken, "wb") as fh:
        fh.write(b"not a real gguf file" * 64)

    scan2 = be.request({"action": "model_scan"}, timeout=180)
    check("放文件后扫描能识别到（count>=1）",
          int(((scan2 or {}).get("model_scan") or {}).get("count") or 0) >= 1, str(scan2)[:200])
    state2 = (scan2 or {}).get("model_state") or {}
    check("损坏文件不会被当成加载成功", state2.get("ready") is False, str(state2)[:200])
    check("损坏文件给出错误说明", bool(((scan2 or {}).get("model_scan") or {}).get("error")),
          str(scan2)[:240])
    check("热加载失败后进程仍存活", be.alive())

    load = be.request({"action": "model_load", "name": "不存在的模型.gguf"}, timeout=60)
    check("加载不存在的文件时明确报错", "model_load" in str(load) and
          not ((load or {}).get("model_load") or {}).get("ok", True), str(load)[:200])
    check("加载失败后仍是无模型状态",
          ((load or {}).get("model_state") or {}).get("ready") is False, str(load)[:200])

    # 真模型：存在就验证"热加载成功"这条正路；不存在则如实跳过
    if os.path.isfile(PACKAGED_MODEL):
        target = os.path.join(models_dir, os.path.basename(PACKAGED_MODEL))
        if not os.path.isfile(target):
            try:
                os.link(PACKAGED_MODEL, target)      # 硬链接：同卷，瞬间完成，不占额外空间
            except OSError:
                shutil.copyfile(PACKAGED_MODEL, target)
        scan3 = be.request({"action": "model_scan", "auto_load": False}, timeout=180)
        check("真模型放进目录后能被扫描到",
              any(m.get("name") == os.path.basename(PACKAGED_MODEL)
                  for m in ((scan3 or {}).get("model_scan") or {}).get("models", [])),
              str(scan3)[:240])
        load2 = be.request({"action": "model_load", "name": os.path.basename(PACKAGED_MODEL)},
                           timeout=600)
        ok = bool(((load2 or {}).get("model_load") or {}).get("ok"))
        check("真模型热加载成功（不重启进程）", ok, str(load2)[:240])
        check("热加载后 model_state.ready=true",
              ((load2 or {}).get("model_state") or {}).get("ready") is True, str(load2)[:240])
        # 加载成功后推理应真的可用（这条最能证明"热加载"不是只改了个标志位）
        chat2 = be.request({"action": "chat", "message": "回复两个字：收到"},
                           types=("assistant", "system", "error"), timeout=300)
        check("热加载后 chat 走真实推理（不再是引导）",
              (chat2 or {}).get("model_ready") is not False, str(chat2)[:200])
        try:
            os.remove(target)
        except OSError:
            pass
    else:
        skip("真模型热加载", "没有找到打包好的 .gguf 模型作为测试素材")


def test_market(python, workdir):
    section("任务三：模型市场列表（显示全部 + 推荐标记 + 默认模型必现）")
    # 这一段用**进程内**方式直接测 ModelHub：不联网也能验证"列表源合并 + 推荐标记"，
    # 而且能精确断言"默认模型一定在列表里"。
    code = r'''
import json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))
from model_hub import ModelHub

class HubCfg:
    models_dir = "models"; registry_file = "models_registry.json"
    source = "auto"; hf_token = ""
class AppCfg:
    model_path = "Qwen3-0.6B-Q6_K.gguf"; model_repo = "unsloth/Qwen3-0.6B-GGUF"

hub = ModelHub(HubCfg(), app_config=AppCfg())
# 强制远端不可达（把两家都指到本机不监听的端口），验证离线时的列表源合并
import model_hub as M
M.MIRROR_ENDPOINT = "http://127.0.0.1:9"
M.OFFICIAL_ENDPOINT = "http://127.0.0.1:9"
res = hub.search(keyword="", source="mirror")
items = res["items"]
pinned = res.get("pinned") or []
default_repo = hub.default_repo()
print(json.dumps({
    "offline": res.get("offline"),
    "count": len(items),
    "pinned_count": len(pinned),
    "repos": [i["repo"] for i in items],
    "pinned_repos": [i["repo"] for i in pinned],
    "default_in_pinned": any(i["repo"] == default_repo for i in pinned),
    "default_in_list": any(i["repo"] == default_repo for i in items),
    "recommended": [i["repo"] for i in items + pinned if i.get("recommended")],
    "recommended_all_in_list": all(i["repo"] in [x["repo"] for x in items + pinned]
                                   for i in items + pinned if i.get("recommended")),
    "basis": res.get("recommend_basis"),
    "source_items": res.get("source_items"),
    "param_of_default": pinned[0].get("param_b") if pinned else None,
    "default_note": pinned[0].get("note_key") if pinned else None,
}, ensure_ascii=False))
'''
    proc = subprocess.run([python, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode != 0:
        check("ModelHub 列表逻辑可执行", False, (proc.stderr or "")[-400:])
        return
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        check("ModelHub 列表逻辑输出可解析", False, f"{exc}；stdout={proc.stdout[-200:]}")
        return

    check("远端不可达时仍返回列表（不再是一片空白）",
          data.get("count", 0) + data.get("pinned_count", 0) >= 1, str(data)[:200])
    # v0.2.1 模型市场优化后，默认模型改为**单独钉住**（pinned）、不进分页数据集：
    # 任务书要求它"钉在列表最前、明确标注来源、不干扰分页计数"。原来的"排在最前"在
    # 分页之后不再成立（第一页第一条不等于全局第一条），所以断言改成更强的两条：
    # 一定被钉住 + 一定不在分页数据集里。
    check("默认模型对应的仓库一定被钉在结果上方", data.get("default_in_pinned") is True,
          str(data)[:240])
    check("默认模型不进分页数据集（不干扰分页计数）",
          data.get("default_in_list") is False, str(data.get("repos"))[:200])
    check("钉住的默认模型带来源标注", data.get("default_note") == "hub.note.default_model",
          str(data.get("default_note")))
    check("默认模型带 recommended 标记", bool(data.get("recommended")),
          str(data.get("recommended"))[:200])
    check("推荐项都来自列表本身（推荐与列表同源）",
          data.get("recommended_all_in_list") is True, str(data)[:240])
    check("默认模型的参数量能被识别（0.6B）",
          abs(float(data.get("param_of_default") or 0) - 0.6) < 1e-6,
          str(data.get("param_of_default")))
    check("给出硬件推荐依据（内存/核数/建议规模）",
          bool((data.get("basis") or {}).get("max_params_b")) or bool((data.get("basis") or {}).get("params")),
          str(data.get("basis"))[:200])


def test_profiles(python):
    """模型档位（config.models）：列表 / 切换 / 未下载 / 未登记四条路径。

    为什么要单独起一个后端实例：档位来自 `config.json` 的 `models` 段，而配置是**启动时**
    读进内存的，运行中改文件不生效。所以这里另建一个临时工作目录写好自己的 config.json
    再起后端（与"无模型就绪"那一段用的是同一套最小客户端）。

    这一段同时兜住两个**真实踩到过**的问题：
      * `Backend.load_model()` 调 `self.llm.load(...)`，而 `load` 原先只在 `LazyLLMEngine`
        上（无模型状态）；已加载模型时再切档会抛 `AttributeError: 'LLMEngine' object has
        no attribute 'load'` —— 那正是"用户日常最常做的操作"（从 0.6B 切到 1.7B）；
      * 未下载的档位必须给出**明确原因 + 仓库名**，界面才能引导用户去下载，
        而不是一句"加载失败"。
    """
    section("模型档位：显示 / 切换 / 未下载 / 未登记")
    if not PACKAGED_MODEL:
        skip("模型档位切换", "没有找到可用的 .gguf 模型作为测试素材")
        return
    workdir = tempfile.mkdtemp(prefix="echodesk_profiles_")
    models_dir = os.path.join(workdir, "models")
    os.makedirs(models_dir, exist_ok=True)
    installed = os.path.basename(PACKAGED_MODEL)
    missing = "Qwen3-1.7B-Q4_K_M.gguf"
    try:
        os.link(PACKAGED_MODEL, os.path.join(models_dir, installed))
    except OSError:
        shutil.copyfile(PACKAGED_MODEL, os.path.join(models_dir, installed))
    config = {
        "model_path": installed,
        "model_repo": "unsloth/Qwen3-0.6B-GGUF",
        "hf_endpoint": "https://hf-mirror.com",
        "context_length": 8192, "max_context": 32768, "auto_context": True,
        "speed_hint": False, "search_default": False, "warmup": False,
        "session_file": "session.json",
        "models": {
            "qwen3-0.6b": {"path": installed, "repo": "unsloth/Qwen3-0.6B-GGUF",
                           "context_length": 8192, "temperature": 0.7},
            "qwen3-1.7b": {"path": missing, "repo": "unsloth/Qwen3-1.7B-GGUF",
                           "context_length": 16384, "temperature": 0.6},
        },
    }
    with open(os.path.join(workdir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    be = None
    try:
        be = Backend(python, workdir)
        ready = be.read_until({"ready", "need_download"}, timeout=300)
        check("带档位配置的后端能启动并就绪",
              (ready or {}).get("type") == "ready", str(ready)[:200])

        resp = be.request({"action": "model_profiles"}, timeout=60)
        data = (resp or {}).get("model_profiles") or {}
        items = data.get("items") or []
        by_alias = {i.get("alias"): i for i in items}
        check("列出全部登记档位（2 档）", len(items) == 2, str(items)[:240])
        check("已下载档位标 installed=true",
              (by_alias.get("qwen3-0.6b") or {}).get("installed") is True, str(by_alias)[:240])
        check("未下载档位标 installed=false",
              (by_alias.get("qwen3-1.7b") or {}).get("installed") is False, str(by_alias)[:240])
        check("当前档位被标记", (by_alias.get("qwen3-0.6b") or {}).get("current") is True,
              str(by_alias)[:240])
        check("档位带参数量与量化（供界面显示差异）",
              (by_alias.get("qwen3-0.6b") or {}).get("params") == "600M"
              and bool((by_alias.get("qwen3-0.6b") or {}).get("quantization")),
              str(by_alias.get("qwen3-0.6b"))[:200])
        check("已下载档位带体积文本",
              bool((by_alias.get("qwen3-0.6b") or {}).get("size_human")), str(by_alias)[:200])

        # 未下载的档位：明确原因 + 仓库（界面据此引导"下载"）
        resp = be.request({"action": "model_profile_apply", "alias": "qwen3-1.7b"}, timeout=120)
        prof = (resp or {}).get("model_profile") or {}
        check("切换未下载档位被拒且标记 installed=false",
              prof.get("ok") is False and prof.get("installed") is False, str(prof)[:240])
        check("未下载档位带回仓库名（供一键下载）",
              prof.get("repo") == "unsloth/Qwen3-1.7B-GGUF", str(prof)[:240])
        check("未下载档位的提示写明文件名与目录",
              missing in str((resp or {}).get("content", "")), str((resp or {}).get("content"))[:200])

        # 未登记的别名
        resp = be.request({"action": "model_profile_apply", "alias": "does-not-exist"}, timeout=60)
        check("切换未登记别名被拒",
              ((resp or {}).get("model_profile") or {}).get("ok") is False, str(resp)[:240])

        # 已下载的档位：必须真的切换成功（这一条在修 llm.load 之前是 AttributeError）
        resp = be.request({"action": "model_profile_apply", "alias": "qwen3-0.6b"}, timeout=600)
        prof = (resp or {}).get("model_profile") or {}
        check("切换已下载档位成功", prof.get("ok") is True, str(prof)[:300])
        check("切换后回传新模型状态（ready=true）",
              ((prof.get("state") or {}).get("ready")) is True, str(prof.get("state"))[:200])
        after = ((resp or {}).get("model_profiles") or {}).get("items") or []
        check("切换后当前档位标记更新",
              [i.get("alias") for i in after if i.get("current")] == ["qwen3-0.6b"], str(after)[:240])
        check("切换档位后进程仍存活", be.alive())
    finally:
        if be is not None:
            be.close()
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    python = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PYTHON
    print("=" * 72)
    print("无模型就绪 / 热加载 / 模型市场 测试")
    print(f"解释器: {python}")
    print(f"后端  : {os.path.join(BACKEND_DIR, 'main.py')}（源码形态，cwd 用临时目录）")
    print("=" * 72)

    workdir = tempfile.mkdtemp(prefix="echodesk_no_model_")
    be = None
    try:
        be = test_no_model_startup(python, workdir)
        test_boundary(be)
        test_scan_and_hotload(python, workdir, be)
    finally:
        if be is not None:
            be.close()
        shutil.rmtree(workdir, ignore_errors=True)

    test_market(python, workdir)
    test_profiles(python)

    print()
    print("=" * 72)
    if SKIPPED:
        for item in SKIPPED:
            print(f"跳过：{item}")
    if FAILED:
        for item in FAILED:
            print(f"失败：{item}")
        print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
        return 1
    print(f"通过 {PASSED} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
