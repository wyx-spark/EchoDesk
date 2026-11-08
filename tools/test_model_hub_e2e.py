"""模型选择栏后端协议端到端测试（新增四）。

直接驱动**打包后的后端**（backend\\EchoDeskBackend.exe）走 JSON 行协议，验证：

* model_hub_status 报告数据源与模型库规模；
* model_hub_search 能通过镜像/官方源搜到 GGUF 模型（含作者的下载量/更新时间）；
* model_hub_detail 返回该仓库所有 GGUF 量化文件与大小；
* model_hub_registry 列出本地模型库，且当前正在使用的模型被标记；
* model_hub_config_set 能切换数据源并即时生效。

不实际下载大模型（下载机制另有 tools/test_results_dir.py 之外的单元验证：
镜像站小文件走完了 下载→校验→登记→断点续传 全链路）。

运行：python tools/test_model_hub_e2e.py [后端exe路径]
"""
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
# 版本号唯一来源：``backend/version.py``。此前这里写死 ``publish/EchoDesk-v0.2.2``，
# 升到 v0.2.4 后门禁会在"找不到后端"处失败，而不是验证真实行为。
from version import APP_VERSION  # noqa: E402


def _resolve_backend() -> str:
    """定位打包后的后端 exe：新布局（便携版三层）优先，其次兼容旧的扁平布局。

    修复七把程序本体收进了 app\\，这个脚本当时留在旧路径上，表现为"找不到后端"
    而不是"测试失败"。同 test_backend_e2e.py 的处理保持一致：发布树从
    ``backend/version.py`` 派生，升版本不再需要修改本脚本。
    """
    publish = os.path.join(ROOT, "publish")
    trees = [os.path.join(publish, f"EchoDesk-{APP_VERSION}")]
    if os.path.isdir(publish):
        trees += [os.path.join(publish, name) for name in sorted(os.listdir(publish), reverse=True)
                  if name.startswith("EchoDesk-v") and os.path.isdir(os.path.join(publish, name))]
    candidates = []
    for tree in trees:
        candidates += [
            os.path.join(tree, "app", "backend", "EchoDeskBackend.exe"),
            os.path.join(tree, "backend", "EchoDeskBackend.exe"),
            os.path.join(tree, "EchoDeskBackend.exe"),
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


DEFAULT_BACKEND = _resolve_backend()

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'✅' if condition else '❌'} {name}" + ("" if condition else f"  ← {detail!r}"))


class Backend:
    def __init__(self, exe):
        self.proc = subprocess.Popen(
            [exe], cwd=os.path.dirname(exe),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lock = threading.Lock()
        self.ready = None
        self.stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())
            if len(self.stderr_lines) > 400:
                del self.stderr_lines[:200]

    def send(self, payload):
        with self.lock:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def read_until(self, types, timeout=180):
        """读到指定 type 的行为止，返回该行 dict（跳过非法/其它行）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"后端输出结束（退出码 {self.proc.poll()}）")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "ready" and self.ready is None:
                self.ready = data
            if data.get("type") in types:
                return data
        raise TimeoutError(f"等待 {types} 超时")

    def close(self):
        try:
            self.send({"action": "exit"})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        if self.proc.poll() is None:
            self.proc.kill()


def _provide_model(backend_exe: str):
    """把开发机上的 .gguf **硬链接**进被测后端目录，返回收尾用的清理函数。

    为什么需要（v0.2.1 默认模型切换一轮）：
      安装程序与便携发布目录**都不含模型**（模型按需下载，见 tools\\installer.iss
      头部说明），被测后端因此一开始就是"尚无模型"状态 —— `need_download` 先到，
      `ready` 里 `model_ready: false`。而本测试有一批断言只在"真有模型"时成立：
      「模型库非空（至少当前模型）」「当前使用的模型被标记」「删除正在使用的模型被拒绝」。
    做法：
      * 用**硬链接**（`os.link`）：同卷瞬间完成、不额外占盘、不复制 472 MB
        （test_no_model.py 上一轮也是这么做的）；
      * 名字取被测后端 `config.json` 里的 `model_path`，后端启动即自动加载它；
      * 返回的清理函数负责删掉链接与（若空的）`models` 目录 —— **必须删干净**：
        发布目录里留着 .gguf 会让下一次 make_installer.ps1 的"不含模型"检查直接失败。
    """
    backend_dir = os.path.dirname(backend_exe)
    models_dir = os.path.join(backend_dir, "models")
    src_dir = os.path.join(ROOT, "backend", "models")
    if not os.path.isdir(src_dir):
        return None

    want = ""
    try:
        with open(os.path.join(backend_dir, "config.json"), encoding="utf-8-sig") as fh:
            want = str(json.load(fh).get("model_path") or "")
    except Exception:  # noqa: BLE001
        want = ""
    names = [n for n in sorted(os.listdir(src_dir)) if n.lower().endswith(".gguf")]
    if not names:
        return None
    name = want if want in names else names[0]
    src = os.path.join(src_dir, name)
    if not os.path.isfile(src):
        return None

    os.makedirs(models_dir, exist_ok=True)
    dst = os.path.join(models_dir, name)

    def cleanup():
        try:
            if os.path.isfile(dst):
                os.remove(dst)
            # 只在自己刚刚建的那个空目录上收尾，别把别人放的东西删了
            if os.path.isdir(models_dir) and not os.listdir(models_dir):
                os.rmdir(models_dir)
        except OSError:
            pass

    if os.path.isfile(dst):
        return cleanup
    try:
        os.link(src, dst)
        print(f"已硬链接测试模型：{name} → {models_dir}（跑完删除）")
    except OSError as exc:  # 跨卷等情况退回复制；失败则照旧跑（会得到 need_download）
        try:
            import shutil
            shutil.copy2(src, dst)
            print(f"已复制测试模型：{name}（硬链接不可用：{exc}）")
        except OSError:
            return None
    return cleanup


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BACKEND
    if not os.path.isfile(exe):
        print(f"未找到后端可执行文件：{exe}")
        return 2
    print(f"后端：{exe}")
    cleanup_model = _provide_model(exe)
    backend = Backend(exe)
    try:
        ready = backend.read_until({"ready", "need_download", "system", "error"}, timeout=300)
        check("后端启动并就绪", ready.get("type") == "ready", str(ready)[:200])
        if ready.get("type") != "ready":
            return 1

        # ---- 状态 ----
        backend.send({"action": "model_hub_status"})
        resp = backend.read_until({"system", "error"})
        status = resp.get("model_hub_status") or {}
        print(f"   数据源：{status.get('source_note')}")
        print(f"   模型库：{status.get('count')} 个，{status.get('total_size_human')}，目录 {status.get('models_dir')}")
        check("状态含数据源说明", bool(status.get("source_note")), str(status)[:200])
        check("状态含镜像与官方地址", status.get("mirror") == "https://hf-mirror.com"
              and status.get("official") == "https://huggingface.co", str(status)[:200])
        check("状态含模型库目录与规模", bool(status.get("models_dir")) and "count" in status, str(status)[:200])

        # ---- 搜索 ----
        # v0.2.1 模型市场优化后：一次调用取回**该关键词的全部结果**（受安全上限约束），
        # 并在本地做筛选/排序/分页，返回第 1 页。
        backend.send({"action": "model_hub_search", "keyword": "qwen2.5 0.5b gguf"})
        resp = backend.read_until({"system", "error"}, timeout=300)
        hub = resp.get("model_hub") or {}
        items = hub.get("items") or []
        print(f"   搜索：已加载 {hub.get('loaded')} 条，第 {hub.get('page', 1)} 页显示 {len(items)} 个结果 "
              f"| 源：{hub.get('source_note')}")
        for it in items[:3]:
            print(f"     - {it['repo']}（⬇{it['downloads']}，更新于 {it['updated_at']}）")
        check("搜索返回结果", len(items) > 0, str(hub)[:200])
        check("搜索未离线失败", hub.get("offline") is False, str(hub.get("message")))
        # 远端结果必须带下载量与更新时间；本地/默认来源没有远端元数据，
        # 要求它们也有更新时间就是在要求接口返回它根本没有的字段（旧断言如此，一直是红的）。
        remote_items = [it for it in items if it.get("origin") == "remote"]
        check("每条远端结果含仓库/作者/下载量/更新时间",
              remote_items and all(it.get("repo") and it.get("author") and "downloads" in it
                                   and it.get("updated_at") for it in remote_items),
              str(remote_items[:1]))
        check("非远端来源的条目带来源标注（而不是显示成 0 次下载）",
              all(it.get("origin") == "remote" or it.get("note_key")
                  for it in items), str(items[:2]))
        check("搜索结果标注了数据源", bool(hub.get("source_note")), str(hub.get("source_note")))
        check("下载量口径标注为历史总下载量或已混合标注",
              hub.get("downloads_metric") in ("all_time", "mixed"),
              str(hub.get("downloads_metric")))
        check("返回分页查询块（第 1 页 20 条）",
              (hub.get("query") or {}).get("page_size") == 20
              and "pages" in (hub.get("query") or {}), str(hub.get("query"))[:200])
        check("默认模型单独钉住、不混在分页数据集里",
              isinstance(hub.get("pinned"), list)
              and all(it["repo"] not in {p["repo"] for p in hub.get("pinned") or []}
                      for it in items), str(hub.get("pinned"))[:200])

        # ---- 本地查询：翻页 / 排序 / 筛选（不重新请求远端）----
        backend.send({"action": "model_hub_query", "page": 1, "page_size": 20})
        query = (backend.read_until({"system", "error"}).get("hub_query") or {})
        check("本地查询返回三层数字",
              all(k in query for k in ("filtered", "loaded", "pages")), str(query)[:200])
        backend.send({"action": "model_hub_query", "page": 9999, "page_size": 20})
        clamped = (backend.read_until({"system", "error"}).get("hub_query") or {})
        check("越界页码被夹到最后一页并标记",
              clamped.get("page") == clamped.get("pages") and clamped.get("page_corrected") is True,
              str(clamped)[:200])
        backend.send({"action": "model_hub_query", "page": 1, "sort": "likes"})
        by_likes = (backend.read_until({"system", "error"}).get("hub_query") or {})
        likes = [it.get("likes") or 0 for it in by_likes.get("items") or []]
        check("本地按点赞排序生效",
              all(likes[i] >= likes[i + 1] for i in range(len(likes) - 1)), str(likes))

        # ---- 详情 ----
        repo = items[0]["repo"] if items else "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
        backend.send({"action": "model_hub_detail", "repo": repo})
        resp = backend.read_until({"system", "error"}, timeout=120)
        detail = resp.get("detail") or {}
        files = detail.get("files") or []
        print(f"   详情 {repo}：{len(files)} 个 GGUF 文件，许可 {detail.get('license')}")
        for f in files[:3]:
            print(f"     - {f['filename']} {f['size_human']} {f['quantization']}")
        check("详情含 GGUF 文件列表", len(files) > 0, str(detail)[:200])
        check("详情文件含大小与量化格式",
              all(f.get("size") and f.get("quantization") for f in files), str(files[:1]))
        check("详情文件按大小升序", [f["size"] for f in files] == sorted(f["size"] for f in files))
        check("详情标注了是否已下载", all("installed" in f for f in files), str(files[:1]))

        # ---- 模型库 ----
        backend.send({"action": "model_hub_registry"})
        resp = backend.read_until({"system", "error"})
        reg = resp.get("registry") or {}
        models = reg.get("models") or []
        current = [m for m in models if m.get("current")]
        print(f"   模型库：{len(models)} 个（{reg.get('total_size_human')}），当前使用 {[m['name'] for m in current]}")
        check("模型库非空（至少当前模型）", len(models) > 0, str(reg)[:200])
        check("当前使用的模型被标记", len(current) == 1, str([m.get("name") for m in models]))
        check("模型库条目含大小/量化/来源/最后使用",
              all("size_human" in m and "quantization" in m and "source" in m and "last_used" in m
                  for m in models), str(models[:1]))
        check("模型库合计占用大于 0", int(reg.get("total_size", 0)) > 0, str(reg.get("total_size")))

        # ---- 切换到官方源 ----
        backend.send({"action": "model_hub_config_set", "model_hub": {"source": "official"}})
        resp = backend.read_until({"system", "error"})
        check("切换官方源被接受", resp.get("type") == "system" and "已保存" in (resp.get("content") or ""),
              str(resp)[:200])
        backend.send({"action": "model_hub_status"})
        resp = backend.read_until({"system", "error"})
        check("官方源即时生效", (resp.get("model_hub_status") or {}).get("source_setting") == "official",
              str(resp.get("model_hub_status"))[:200])
        backend.send({"action": "model_hub_config_set", "model_hub": {"source": "auto"}})
        backend.read_until({"system", "error"})

        # ---- 删除保护：正在使用的模型不允许删除 ----
        if current:
            backend.send({"action": "model_hub_uninstall", "name": current[0]["name"]})
            resp = backend.read_until({"system", "error"})
            content = resp.get("content") or ""
            check("删除正在使用的模型被拒绝", "❌" in content, content)
    except Exception as exc:  # noqa: BLE001
        check("端到端流程未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        tail = backend.stderr_lines[-6:]
        if tail:
            print("   后端 stderr 末尾：")
            for line in tail:
                print("     " + line[:160])
        backend.close()
        # 收尾：删掉临时硬链接的测试模型 —— 发布目录里留着 .gguf 会让
        # make_installer.ps1 的"安装程序不含模型"检查失败（下一轮构建会踩到）
        if cleanup_model:
            cleanup_model()

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
