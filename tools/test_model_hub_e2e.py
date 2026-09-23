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
DEFAULT_BACKEND = os.path.join(ROOT, "publish", "EchoDesk-v0.2.1", "backend", "EchoDeskBackend.exe")

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


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BACKEND
    if not os.path.isfile(exe):
        print(f"未找到后端可执行文件：{exe}")
        return 2
    print(f"后端：{exe}")
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
        backend.send({"action": "model_hub_search", "keyword": "qwen2.5 0.5b gguf", "limit": 5})
        resp = backend.read_until({"system", "error"}, timeout=120)
        hub = resp.get("model_hub") or {}
        items = hub.get("items") or []
        print(f"   搜索：{len(items)} 个结果 | 源：{hub.get('source_note')}")
        for it in items[:3]:
            print(f"     - {it['repo']}（⬇{it['downloads']}，更新于 {it['updated_at']}）")
        check("搜索返回结果", len(items) > 0, str(hub)[:200])
        check("搜索未离线失败", hub.get("offline") is False, str(hub.get("message")))
        check("每条结果含仓库/作者/下载量/更新时间",
              all(it.get("repo") and it.get("author") and "downloads" in it and it.get("updated_at")
                  for it in items), str(items[:1]))
        check("搜索结果标注了数据源", bool(hub.get("source_note")), str(hub.get("source_note")))

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

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
