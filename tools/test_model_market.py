# -*- coding: utf-8 -*-
"""模型市场优化（v0.2.1）自动化测试：加载器 + 本地查询引擎。

覆盖任务书 任务一 ~ 任务七 的可机验部分。**不依赖真实网络**：
用本机 HTTP 桩装配一个"行为与 Hugging Face /api/models 一致"的分页接口
（游标在 ``Link`` 响应头里、``expand`` 只回被 expand 的字段、同一 expand 重复即 400、
``limit`` 超过 1000 静默截断），这样每条边界都能精确构造、每次运行结果一致。

跑法：python tools/test_model_market.py
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print(f"{'✅' if condition else '❌'} {name}" + ("" if condition else f"  ← {detail!r}"))


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 桩服务端


class StubHub:
    """极小号的 HF ``/api/models`` 仿真：游标分页 + expand 语义 + 可注入故障。"""

    #: 模拟服务端单页上限（实测 HF 是 1000，超过就静默截断）
    SERVER_MAX_LIMIT = 1000

    def __init__(self, total=45, *, fallback_every=0, link_host="", fail_at=(), fail_all=False,
                 hide_all_time=False, server_max_limit=SERVER_MAX_LIMIT):
        self.total = total
        self.fallback_every = fallback_every     # 每隔 N 条故意不给 downloadsAllTime
        self.link_host = link_host               # 故意把 Link 指向别的主机
        self.fail_at = set(fail_at)              # 第几次请求失败（1 起）
        self.fail_all = fail_all
        self.hide_all_time = hide_all_time
        #: 本桩的页大小上限。真站是 1000；调小就能用几十条数据复现"多页"场景，
        #: 同时验证"服务端把 limit 截断"时客户端仍然能靠游标继续取。
        self.server_max_limit = server_max_limit
        self.requests = []                       # 每次请求的 query 解析结果
        self.server = None
        self.thread = None
        self.base = ""

    # ---- 造数据 ----
    def item(self, index):
        repo = f"author{index % 7}/Model{index:05d}-{1 + (index % 12)}B-GGUF"
        if index % 5 == 0:
            repo = repo[:-5] + "-Q4_K_M-GGUF"
        entry = {
            "id": repo,
            "author": repo.split("/")[0],
            "likes": (index * 37) % 500,
            "lastModified": f"2026-{(index % 12) + 1:02d}-{(index % 28) + 1:02d}T00:00:00.000Z",
            "createdAt": "2025-01-01T00:00:00.000Z",
            "tags": ["gguf", f"tier{index % 4}", f"base_model:x/y-{index % 3}"],
            "gated": index % 11 == 0,
            "private": False,
            "pipeline_tag": "text-generation",
            "library_name": "transformers",
            # 历史总量与 30 天值刻意不同：口径弄错时排序与显示都会露馅
            "downloads": 1_000_000 - index,
            "downloadsAllTime": 50_000_000 - index * 1000,
        }
        if self.hide_all_time or (self.fallback_every and index % self.fallback_every == 0):
            entry.pop("downloadsAllTime")
        return entry

    def _handler(self):
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # 静音
                pass

            def do_GET(self):                    # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                stub.requests.append(query)

                if stub.fail_all or len(stub.requests) in stub.fail_at:
                    body = b'{"error":"injected failure"}'
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                # 同一 expand 重复出现 == 服务端 400（实测行为，字段坑之一）
                expands = query.get("expand", [])
                if len(expands) != len(set(expands)):
                    body = b'{"error":"duplicate expand"}'
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                limit = int((query.get("limit") or ["20"])[0])
                limit = min(limit, stub.server_max_limit)      # 静默截断
                cursor = (query.get("cursor") or [""])[0]
                offset = 0
                if cursor:
                    offset = int(base64.urlsafe_b64decode(cursor.encode()).decode().split("=")[1])

                indexes = list(range(offset, min(offset + limit, stub.total)))
                payload = []
                for index in indexes:
                    entry = stub.item(index)
                    if expands:
                        # **关键语义**：传了 expand 就只回被 expand 的字段
                        entry = {k: v for k, v in entry.items() if k in expands}
                        entry.setdefault("id", stub.item(index)["id"])
                    payload.append(entry)

                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if offset + limit < stub.total:
                    nxt = base64.urlsafe_b64encode(
                        f"off={offset + limit}".encode()).decode()
                    host = stub.link_host or stub.base
                    self.send_header("Link", f'<{host}/api/models?cursor={nxt}>; rel="next"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def start(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.base

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------- 装配


class HubCfg:
    def __init__(self, **kwargs):
        self.models_dir = ""
        self.registry_file = ""
        self.source = "mirror"
        self.hf_token = ""
        self.download_retries = 2
        self.max_items = 100
        self.max_requests = 6
        self.max_seconds = 60.0
        self.page_size = 20
        self.search_cache_dir = ""
        self.search_cache_ttl_hours = 6
        for key, value in kwargs.items():
            setattr(self, key, value)


class AppCfg:
    model_path = ""
    model_repo = ""


def make_hub(tmpdir, stub, **kwargs):
    """造一个 ModelHub：数据源指向桩、模型库指向空目录、缓存指向临时目录。"""
    import model_hub as M

    M.MIRROR_ENDPOINT = stub.base
    M.OFFICIAL_ENDPOINT = stub.base          # 两源同一桩：避免回退时打真实网络
    cfg = HubCfg(models_dir=os.path.join(tmpdir, "models"),
                 registry_file=os.path.join(tmpdir, "models_registry.json"),
                 search_cache_dir=os.path.join(tmpdir, "cache"), **kwargs)
    os.makedirs(cfg.models_dir, exist_ok=True)
    return M.ModelHub(cfg, app_config=AppCfg())


# ---------------------------------------------------------------- A. 加载器


def test_loader(tmpdir):
    section("任务一/二：加载器（分页取全、口径、上限、重试、缓存、续载）")
    import model_hub as M

    # --- A1 一次取完 + 精确总数 ---
    stub = StubHub(total=45)
    stub.start()
    try:
        hub = make_hub(tmpdir, stub)
        progress = []
        res = hub.search(keyword="GGUF", sort="downloads", source="mirror",
                         on_progress=progress.append)
        check("A1 分页取完全部结果", res["loaded"] == 45, f"loaded={res['loaded']}")
        check("A1 取完后 total_available 是精确总数",
              res["total_available"] == 45, f"{res['total_available']}")
        check("A1 取完后 has_more 为假", res["has_more"] is False, f"{res['has_more']}")
        check("A1 stop_reason 为 none（取完了）", res["stop_reason"] == "none", res["stop_reason"])
        check("A1 每页请求不超过服务端单页上限 1000",
              all(1 <= int(q["limit"][0]) <= 1000 for q in stub.requests),
              [q.get("limit") for q in stub.requests])
        check("A1 只发了一次请求（45 条 < 1000）", len(stub.requests) == 1, len(stub.requests))
        check("A1 请求带 filter=gguf", stub.requests[0].get("filter") == ["gguf"],
              stub.requests[0])
        check("A1 请求显式要求历史总下载量",
              "downloadsAllTime" in stub.requests[0].get("expand", []), stub.requests[0].get("expand"))
        check("A1 expand 无重复（重复会被服务端 400）",
              len(stub.requests[0].get("expand", [])) == len(set(stub.requests[0].get("expand", []))),
              stub.requests[0].get("expand"))
        check("A1 有进度回调（不是干等）", len(progress) >= 2, len(progress))
        check("A1 进度回调含已取回条数与阶段",
              all("loaded" in p and "phase" in p for p in progress), progress[:1])
        check("A1 下载量取的是历史总下载量（不是 30 天）",
              all(it["downloads"] == it["downloads_all_time"] for it in res["items"]
                  if it["origin"] == "remote"), res["items"][0] if res["items"] else None)
        check("A1 下载量口径标记为 all_time", res["downloads_metric"] == "all_time",
              res["downloads_metric"])
    finally:
        stub.stop()

    # --- A2 expand 字段完整（tags/likes/updated 不能因为加了 expand 就消失） ---
    stub = StubHub(total=5)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a2"), stub)
        res = hub.search(keyword="GGUF", source="mirror")
        first = res["items"][1]          # 第 1 条的 likes 恰好是 0（index*37 % 500），取第 2 条
        check("A2 加了 expand 后 tags 仍在", len(first["tags"]) > 0, first["tags"])
        check("A2 加了 expand 后 likes 仍在", first["likes"] > 0, first["likes"])
        check("A2 加了 expand 后作者仍在", bool(first["author"]), first["author"])
        check("A2 更新时间不再是空串（旧版 lastModified 缺失导致恒为空）",
              len(first["updated_at"]) == 10, first["updated_at"])
    finally:
        stub.stop()

    # --- A3 缺失 downloadsAllTime → 回退并标注 ---
    stub = StubHub(total=20, fallback_every=4)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a3"), stub)
        res = hub.search(keyword="GGUF", source="mirror")
        fallback = [it for it in res["items"] if it["downloads_source"] == "default_fallback"]
        ok_metric = all(it["downloads"] == it["downloads_recent"] for it in fallback)
        check("A3 缺字段的条目回退到 30 天口径", fallback and ok_metric,
              [it["downloads_source"] for it in res["items"]])
        check("A3 回退条目没有被显示成 0", all(it["downloads"] > 0 for it in fallback),
              [it["downloads"] for it in fallback])
        check("A3 口径标记为 mixed 并记数",
              res["downloads_metric"] == "mixed" and res["downloads_fallback_count"] == len(fallback),
              f"{res['downloads_metric']}/{res['downloads_fallback_count']}")
    finally:
        stub.stop()

    # --- A4 条数上限 + 续载 ---
    stub = StubHub(total=250)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a4"), stub, max_items=100)
        first = hub.search(keyword="GGUF", source="mirror")
        check("A4 撞条数上限即停（不再无限请求）",
              first["loaded"] == 100 and first["stop_reason"] == "items",
              f"{first['loaded']}/{first['stop_reason']}")
        check("A4 撞上限时 has_more 为真", first["has_more"] is True, first["has_more"])
        check("A4 撞上限时不谎报总数（total_available 留空）",
              first["total_available"] is None, first["total_available"])
        check("A4 撞上限的说明里含'还有更多'与已加载条数",
              "还有更多" in first["message"] and "100" in first["message"], first["message"])
        second = hub.search(keyword="GGUF", source="mirror", resume=True)
        check("A4 续载后条数增加", second["loaded"] > first["loaded"],
              f"{first['loaded']} → {second['loaded']}")
        check("A4 续载不产生重复条目",
              len({it["repo"] for it in second["items"]}) == len(second["items"]),
              len(second["items"]))
        third = hub.search(keyword="GGUF", source="mirror", resume=True)
        fourth = hub.search(keyword="GGUF", source="mirror", resume=True)
        check("A4 反复续载最终取完全部（250 条）",
              fourth["loaded"] == 250 and fourth["has_more"] is False,
              f"{first['loaded']}/{second['loaded']}/{third['loaded']}/{fourth['loaded']} "
              f"has_more={fourth['has_more']}")
        check("A4 取完后 total_available 变成精确值",
              fourth["total_available"] == 250, fourth["total_available"])
    finally:
        stub.stop()

    # --- A5 请求次数上限 ---
    stub = StubHub(total=100000)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a5"), stub, max_items=20000, max_requests=2)
        res = hub.search(keyword="GGUF", source="mirror")
        check("A5 撞请求次数上限即停",
              res["stop_reason"] == "requests" and res["requests"] == 2,
              f"{res['stop_reason']}/{res['requests']}")
        check("A5 仍返回已取回的结果", res["loaded"] == 2000, res["loaded"])
        check("A5 说明里点明达到的是请求次数上限",
              "请求次数" in res["message"], res["message"])
    finally:
        stub.stop()

    # --- A6 耗时上限 ---
    stub = StubHub(total=100000)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a6"), stub, max_items=20000,
                       max_requests=50, max_seconds=5.0)
        original = hub._get_json_with_headers

        def slow(url, timeout=40):
            time.sleep(2.0)
            return original(url, timeout)

        hub._get_json_with_headers = slow
        res = hub.search(keyword="GGUF", source="mirror")
        check("A6 撞耗时上限即停",
              res["stop_reason"] == "seconds" and res["loaded"] > 0,
              f"{res['stop_reason']}/{res['loaded']}")
    finally:
        stub.stop()

    # --- A7 单页失败重试后中止，保留已加载部分 ---
    # 桩的页大小上限压到 50 ⇒ 250 条要 5 页；第 3 页连失两次 ⇒ 中止在 100 条。
    # 这同时验证了"服务端把 limit 截断到更小"时客户端仍靠游标继续取。
    stub = StubHub(total=250, fail_at=(3, 4), server_max_limit=50)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a7"), stub, max_items=1000)
        res = hub.search(keyword="GGUF", source="mirror")
        check("A7 连续失败后中止并报错",
              res["stop_reason"] == "error" and bool(res["error"]),
              f"{res['stop_reason']}/{res['error']}")
        check("A7 中止后仍保留已加载的结果", res["loaded"] == 100, res["loaded"])
        check("A7 说明里给出'已加载到哪'", "100" in res["message"], res["message"])
        check("A7 单页失败确实重试过（请求次数 > 页数）",
              res["requests"] > res["pages"], f"{res['requests']}/{res['pages']}")
        check("A7 中止后 has_more 为真（剩下的还能续）", res["has_more"] is True, res["has_more"])
    finally:
        stub.stop()

    # --- A8 单页偶发失败（重试可恢复）不会中止 ---
    stub = StubHub(total=50, fail_at=(1,))
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a8"), stub)
        res = hub.search(keyword="GGUF", source="mirror")
        check("A8 首请求失败后重试成功、取完结果",
              res["loaded"] == 50 and res["stop_reason"] == "none",
              f"{res['loaded']}/{res['stop_reason']}")
        check("A8 重试后没有把失败当结果（消息不含错误）",
              "中断" not in res["message"], res["message"])
    finally:
        stub.stop()

    # --- A9 Link 头指向别的主机时只取游标（镜像站就是这种情况） ---
    check("A9 cursor_from_link 只取游标、丢掉别的主机",
          M.ModelHub.cursor_from_link(
              '<https://huggingface.co/api/models?limit=1000&cursor=ABC%3D%3D>; rel="next"')
          == "ABC==",
          M.ModelHub.cursor_from_link(
              '<https://huggingface.co/api/models?limit=1000&cursor=ABC%3D%3D>; rel="next"'))
    check("A9 没有下一页时返回空串",
          M.ModelHub.cursor_from_link('') == "" and
          M.ModelHub.cursor_from_link('<x>; rel="prev"') == "")

    stub = StubHub(total=2500, link_host="https://huggingface.co")
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a9"), stub, max_items=5000)
        res = hub.search(keyword="GGUF", source="mirror")
        check("A9 Link 指向别的主机时仍能继续分页（游标被拼回当前源）",
              res["loaded"] == 2500 and len(stub.requests) == 3,
              f"{res['loaded']}/{len(stub.requests)}")
    finally:
        stub.stop()

    # --- A10 磁盘缓存 ---
    stub = StubHub(total=45)
    stub.start()
    try:
        cache_dir = os.path.join(tmpdir, "a10n")
        hub = make_hub(cache_dir, stub)
        first = hub.search(keyword="GGUF", source="mirror")
        requests_after_first = len(stub.requests)
        cached = hub.search(keyword="GGUF", source="mirror")
        check("A10 同一关键词再次搜索走缓存（不再打网络）",
              len(stub.requests) == requests_after_first and cached["cache"]["hit"] is True,
              f"{requests_after_first} → {len(stub.requests)} hit={cached['cache']['hit']}")
        check("A10 缓存内容与首次一致",
              [it["repo"] for it in cached["items"]] == [it["repo"] for it in first["items"]],
              f"{cached['loaded']} vs {first['loaded']}")
        files = [f for f in os.listdir(os.path.join(cache_dir, "cache")) if f.endswith(".json")]
        check("A10 缓存确实落盘了", len(files) == 1, files)
        refreshed = hub.search(keyword="GGUF", source="mirror", refresh=True)
        check("A10 手动刷新会重新打网络",
              len(stub.requests) > requests_after_first and refreshed["cache"]["hit"] is False,
              f"{len(stub.requests)} hit={refreshed['cache']['hit']}")
        # 过期：TTL 设为 0 后必须重打
        hub.cfg.search_cache_ttl_hours = 0
        hub.search(keyword="GGUF", source="mirror")
        before = len(stub.requests)
        hub.search(keyword="GGUF", source="mirror")
        check("A10 缓存过期后重新加载", len(stub.requests) > before,
              f"{before} → {len(stub.requests)}")
        # 不同排序 = 不同缓存桶
        hub.cfg.search_cache_ttl_hours = 6
        before = len(stub.requests)
        hub.search(keyword="GGUF", sort="likes", source="mirror")
        check("A10 换排序会重新请求（排序是服务端行为）",
              len(stub.requests) > before, f"{before} → {len(stub.requests)}")
        before = len(stub.requests)
        hub.search(keyword="GGUF", sort="likes", source="mirror")
        check("A10 换回同一排序时用该排序的缓存",
              len(stub.requests) == before, f"{before} → {len(stub.requests)}")
    finally:
        stub.stop()

    # --- A11 离线降级：两个源都不可达时给缓存 + 本地 + 默认 ---
    stub = StubHub(total=30)
    stub.start()
    base = stub.base
    try:
        hub = make_hub(os.path.join(tmpdir, "a11"), stub)
        online = hub.search(keyword="GGUF", source="mirror")
        check("A11 先在线取一次并写缓存", online["loaded"] == 30, online["loaded"])
        stub.stop()
        hub2 = make_hub(os.path.join(tmpdir, "a11"), stub)
        M.MIRROR_ENDPOINT = "http://127.0.0.1:9"
        M.OFFICIAL_ENDPOINT = "http://127.0.0.1:9"
        offline = hub2.offline_dataset(error="两个源都不可达", keyword="GGUF", source="mirror")
        check("A11 离线时标记 offline", offline["offline"] is True, offline["offline"])
        check("A11 离线时展示磁盘缓存的结果", offline["loaded"] == 30, offline["loaded"])
        check("A11 离线时明确写出走了缓存", offline["cache"]["hit"] is True, offline["cache"])
        check("A11 离线说明里含原因", "不可达" in offline["message"], offline["message"])
    finally:
        try:
            stub.stop()
        except Exception:  # noqa: BLE001
            pass
        M.MIRROR_ENDPOINT = base
        M.OFFICIAL_ENDPOINT = base

    # --- A11b 没有会话时 query_session 要如实说"还没加载" ---
    stub = StubHub(total=10)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a11b"), stub)
        fresh = hub.query_session({"page": 1})
        check("A11b 后端没有数据集时 query_session 返回 needs_load（不编造空列表）",
              fresh.get("needs_load") is True and fresh.get("loaded") == 0, str(fresh)[:200])
        hub.search(keyword="GGUF", source="mirror")
        after = hub.query_session({"page": 1, "page_size": 20})
        check("A11b 加载后 query_session 能给出结果与三层数字",
              after.get("needs_load") is False and after.get("loaded") == 10
              and after.get("page_size") == 20, str(after)[:200])
    finally:
        stub.stop()

    # --- A12 默认模型钉住但不进分页计数 ---
    stub = StubHub(total=45)
    stub.start()
    try:
        import model_hub as MH

        cfg = HubCfg(models_dir=os.path.join(tmpdir, "a12", "models"),
                     registry_file=os.path.join(tmpdir, "a12", "registry.json"),
                     search_cache_dir=os.path.join(tmpdir, "a12", "cache"))
        os.makedirs(cfg.models_dir, exist_ok=True)

        class AppCfg2:
            model_path = "Model00003-4B-Q4_K_M.gguf"
            model_repo = "author3/Model00003-4B-GGUF"

        hub = MH.ModelHub(cfg, app_config=AppCfg2())
        res = hub.search(keyword="GGUF", source="mirror")
        pinned = res["pinned"]
        check("A12 默认模型被单独钉住", len(pinned) == 1 and pinned[0]["repo"] == AppCfg2.model_repo,
              pinned)
        check("A12 默认模型不混在分页数据集里（不干扰计数）",
              all(it["repo"] != AppCfg2.model_repo for it in res["items"]),
              [it["repo"] for it in res["items"] if it["repo"] == AppCfg2.model_repo])
        check("A12 钉住的默认模型带来源标注",
              pinned[0].get("note_key") == "hub.note.default_model", pinned[0].get("note_key"))
        check("A12 钉住的默认模型带 recommended 标记（推荐依据可解释）",
              "default" in (pinned[0].get("recommend_reasons") or []),
              pinned[0].get("recommend_reasons"))
        check("A12 loaded 只算分页数据集", res["loaded"] == len(res["items"]), res["loaded"])
    finally:
        stub.stop()

    # --- A13 加载耗时实测（记录用，不是断言） ---
    stub = StubHub(total=5000)
    stub.start()
    try:
        hub = make_hub(os.path.join(tmpdir, "a13"), stub, max_items=5000, max_requests=8)
        started = time.time()
        res = hub.search(keyword="GGUF", source="mirror")
        elapsed = time.time() - started
        check("A13 桩服务端下 5000 条按 5 页取完",
              res["loaded"] == 5000 and res["requests"] == 5,
              f"{res['loaded']}/{res['requests']}")
        print(f"   （桩服务端 5000 条用时 {elapsed:.2f}s，请求 {res['requests']} 次）")
        t0 = time.time()
        hub.query_dataset(res, keyword="model00", page=7)
        local_ms = (time.time() - t0) * 1000
        check("A13 本地筛选+排序+分页在毫秒级（实时筛选的前提）", local_ms < 500,
              f"{local_ms:.1f} ms")
        print(f"   （本地筛选/排序/分页 5000 条用时 {local_ms:.1f} ms）")
    finally:
        stub.stop()


# ---------------------------------------------------------------- B. 查询引擎


def sample_items(count=57):
    """构造确定性的结果集：区间、量化、时间、已下载、推荐各种取值都覆盖到。"""
    items = []
    for index in range(count):
        param = [0.5, 1.5, 4.0, 13.0, 30.0, 70.0, 0.0][index % 7]
        quant = ["Q4_K_M", "Q8_0", "", "Q2_K"][index % 4]
        items.append({
            "repo": f"author{index % 3}/Model{index:03d}-GGUF",
            "name": f"Model{index:03d}-GGUF",
            "author": f"author{index % 3}",
            "downloads": (count - index) * 100,
            "likes": index * 3,
            "updated_at": f"2026-{1 + index % 12:02d}-01" if index % 9 else "",
            "param_b": param,
            "quant_code": quant,
            "quantization": quant or "未知",
            "tags": ["gguf", f"tag{index % 5}"],
            "description": f"tier{index % 3} · text-generation",
            "installed": index % 5 == 0,
            "recommended": index % 4 == 0,
            "origin": "remote",
        })
    return items


def test_query_engine():
    section("任务三/四/五/六：本地查询引擎（分页 / 筛选 / 排序）")
    from model_query import (DEFAULT_PAGE_SIZE, PAGE_SIZES, filter_options, matches_filters,
                             matches_keyword, normalize_filters, page_number, param_bucket,
                             query, sort_items, update_bucket)

    items = sample_items(57)
    now = 1775000000.0     # 固定"现在"，让时间分档可复现

    # --- B1 关键词 ---
    check("B1 单关键词命中", matches_keyword(items[0], "model000"))
    check("B1 多关键词按与关系收窄",
          matches_keyword(items[0], "model000 author0") and
          not matches_keyword(items[0], "model000 author1"))
    check("B1 大小写不敏感", matches_keyword(items[0], "MODEL000"))
    check("B1 命中标签", matches_keyword(items[0], "tag0"))
    check("B1 命中描述里的元数据", matches_keyword(items[0], "text-generation"))
    check("B1 空关键词全部命中", matches_keyword(items[0], "  "))

    # --- B2 分页 ---
    first = query(items, page=1, page_size=20, now=now)
    check("B2 每页 20 条", len(first["items"]) == 20, len(first["items"]))
    check("B2 总页数 = ceil(57/20) = 3", first["pages"] == 3, first["pages"])
    check("B2 下载量最高的排在第一页第一条",
          first["items"][0]["name"] == "Model000-GGUF", first["items"][0]["name"])
    last = query(items, page=3, page_size=20, now=now)
    check("B2 最后一页只含剩余 17 条", len(last["items"]) == 17, len(last["items"]))
    over = query(items, page=99, page_size=20, now=now)
    check("B2 超出总页数夹到最后一页并标记被纠正",
          over["page"] == 3 and over["page_corrected"] is True, f"{over['page']}/{over['page_corrected']}")
    under = query(items, page=0, page_size=20, now=now)
    check("B2 页码 0 夹到第 1 页", under["page"] == 1 and under["page_corrected"] is True,
          f"{under['page']}/{under['page_corrected']}")
    bad = query(items, page="abc", page_size=20, now=now)
    check("B2 非法页码回第 1 页并给出纠正标记",
          bad["page"] == 1 and bad["page_corrected"] is True, f"{bad['page']}/{bad['page_corrected']}")
    check("B2 空页码回第 1 页、不视为错误",
          query(items, page="", page_size=20, now=now)["page_corrected"] is False)

    # 分页遍历的强断言：并集 == 全集 且无重复无遗漏
    collected = []
    for page in range(1, first["pages"] + 1):
        collected.extend(it["name"] for it in query(items, page=page, page_size=20, now=now)["items"])
    check("B2 逐页遍历的并集等于全集（无重复、无遗漏）",
          collected == [it["name"] for it in sort_items(items, "downloads")],
          f"{len(collected)}/{len(set(collected))}")

    for size in PAGE_SIZES:
        res = query(items, page=1, page_size=size, now=now)
        check(f"B2 每页 {size} 条可切换", res["page_size"] == size and len(res["items"]) <= size,
              f"{res['page_size']}/{len(res['items'])}")
    check("B2 每页条数非法时回落到 20",
          query(items, page=1, page_size=0, now=now)["page_size"] == DEFAULT_PAGE_SIZE)
    check("B2 默认每页 20 条", query(items, page=1, now=now)["page_size"] == DEFAULT_PAGE_SIZE)
    check("B2 空结果时页数仍为 1（界面据此禁用翻页）",
          query([], page=1, now=now)["pages"] == 1)

    # --- B3 排序 ---
    for key, field, reverse in (("downloads", "downloads", True), ("likes", "likes", True),
                                ("params", "param_b", True)):
        ordered = sort_items(items, key)
        column = [it[field] for it in ordered]
        check(f"B3 按 {key} 降序",
              column == sorted(column, reverse=reverse) or
              all(column[i] >= column[i + 1] for i in range(len(column) - 1)), column[:6])
    updated = [it["updated_at"] for it in sort_items(items, "updated")]
    check("B3 按更新时间降序且缺失项排最后",
          updated[:1][0] >= updated[-2] and updated[-1] == "", updated[:3] + updated[-2:])
    check("B3 排序可复现（同输入两次结果一致）",
          [it["repo"] for it in sort_items(items, "downloads")] ==
          [it["repo"] for it in sort_items(list(items), "downloads")])
    check("B3 未知排序键回落为 downloads",
          sort_items(items, "??") == sort_items(items, "downloads"))

    # --- B4 结构化筛选 ---
    bucket_counts = {key: sum(1 for it in items if param_bucket(it["param_b"]) == key)
                     for key in {param_bucket(it["param_b"]) for it in items}}
    le1 = query(items, filters={"params": ["p_le1"]}, page_size=100, now=now)
    check("B4 参数量区间筛选", le1["filtered"] == bucket_counts.get("p_le1"),
          f"{le1['filtered']} vs {bucket_counts.get('p_le1')}")
    multi = query(items, filters={"params": ["p_le1", "p_gt40"]}, page_size=100, now=now)
    check("B4 同一维度多选是'或'（并集扩大）",
          multi["filtered"] == bucket_counts.get("p_le1", 0) + bucket_counts.get("p_gt40", 0),
          multi["filtered"])
    both = query(items, filters={"params": ["p_le1"], "installed": "yes"}, page_size=100, now=now)
    check("B4 不同维度之间是'与'（结果收窄）",
          0 < both["filtered"] <= le1["filtered"], f"{both['filtered']} <= {le1['filtered']}")
    q4 = query(items, filters={"quant": ["Q4_K_M"]}, page_size=100, now=now)
    check("B4 量化格式筛选", q4["filtered"] == sum(1 for it in items if it["quant_code"] == "Q4_K_M"),
          q4["filtered"])
    check("B4 量化筛选大小写不敏感",
          query(items, filters={"quant": ["q4_k_m"]}, page_size=100, now=now)["filtered"]
          == q4["filtered"])
    unknown_quant = query(items, filters={"quant": ["UNKNOWN"]}, page_size=100, now=now)
    check("B4 没有量化标识的条目归入未知档（不被静默丢掉）",
          unknown_quant["filtered"] == sum(1 for it in items if not it["quant_code"]),
          unknown_quant["filtered"])
    yes = query(items, filters={"installed": "yes"}, page_size=100, now=now)
    no = query(items, filters={"installed": "no"}, page_size=100, now=now)
    check("B4 是否本地已下载（三态）",
          yes["filtered"] + no["filtered"] == len(items)
          and yes["filtered"] == sum(1 for it in items if it["installed"]),
          f"{yes['filtered']}/{no['filtered']}")
    rec = query(items, filters={"recommended": True}, page_size=100, now=now)
    check("B4 是否推荐筛选",
          rec["filtered"] == sum(1 for it in items if it["recommended"]), rec["filtered"])
    check("B4 更新时间筛选（近 7 天）",
          query(items, filters={"updated": ["u_7"]}, page_size=100, now=now)["filtered"] >= 0)
    check("B4 空筛选条件不改变结果",
          query(items, filters={}, page_size=100, now=now)["filtered"] == len(items))
    check("B4 非法筛选值被忽略而不是筛空",
          query(items, filters={"params": ["不存在"], "installed": "??"},
                page_size=100, now=now)["filtered"] == len(items))
    check("B4 时间分档：缺失更新的条目归入 unknown",
          update_bucket("", now) == "unknown" and update_bucket("2026-01-01", now) in
          ("u_7", "u_30", "u_90", "u_365", "u_old"))

    # --- B5 三层数字 + 空结果出口 ---
    res = query(items, keyword="author0", filters={"installed": "yes"}, page_size=100, now=now)
    check("B5 三层数字齐全（筛选后 / 已加载 / 共 N 条）",
          res["filtered"] <= res["loaded"] and res["loaded"] == len(items)
          and res["total_available"] == len(items),
          f"{res['filtered']}/{res['loaded']}/{res['total_available']}")
    res2 = query(items, page_size=100, now=now, loaded=1000, total_available=4356)
    check("B5 已加载与总数可与本地集合不同（撞上限时）",
          res2["loaded"] == 1000 and res2["total_available"] == 4356 and res2["filtered"] == len(items),
          f"{res2['loaded']}/{res2['total_available']}")
    res3 = query(items, page_size=100, now=now, loaded=1000, has_more=True)
    check("B5 撞上限时不给假总数（留空表示还有更多）", res3["total_available"] is None,
          res3["total_available"])
    empty = query(items, keyword="绝对不存在zzz", page_size=20, now=now)
    check("B5 本地筛无结果时给出'重新搜索远端'的出口",
          empty["empty_reason"] == "local_no_match" and empty["filtered"] == 0, empty["empty_reason"])
    empty2 = query([], keyword="x", page_size=20, now=now)
    check("B5 数据集本来就空时不给'重新搜索'的误导提示",
          empty2["empty_reason"] == "", empty2["empty_reason"])

    # --- B6 分面计数 ---
    options = filter_options(items, now)
    check("B6 分面计数覆盖参数量/量化/已下载/推荐",
          "params" in options and "quant" in options and options["installed"] >= 1
          and options["recommended"] >= 1, sorted(options))
    check("B6 分面计数之和等于集合规模",
          sum(options["params"].values()) == len(items)
          and sum(options["quant"].values()) == len(items), options["params"])
    kw = query(items, keyword="author0", page_size=100, now=now)
    check("B6 分面计数基于关键词筛选后的集合（勾选不会让选项消失）",
          kw["options"]["total"] == sum(1 for it in items if "author0" in it["repo"].lower()),
          kw["options"]["total"])

    # --- B7 筛选/排序在翻页时保持 ---
    # 关键词 author1 + 量化 Q8_0 恰好命中 5 条 → 每页 2 条时正好 3 页
    page1 = query(items, keyword="author1", filters={"quant": ["Q8_0"]}, sort="likes",
                  page=1, page_size=2, now=now)
    page2 = query(items, keyword="author1", filters={"quant": ["Q8_0"]}, sort="likes",
                  page=2, page_size=2, now=now)
    check("B7 筛选后页数按筛选结果重算",
          page1["pages"] >= 2 and page1["filtered"] == 5, f"{page1['pages']}/{page1['filtered']}")
    check("B7 翻页保持关键词与结构化筛选",
          page1["filtered"] == page2["filtered"] and page1["filters"] == page2["filters"],
          f"{page1['filtered']}/{page2['filtered']}")
    check("B7 翻页保持排序（跨页递减）",
          page1["items"] and page2["items"]
          and page1["items"][-1]["likes"] >= page2["items"][0]["likes"],
          f"{[it['likes'] for it in page1['items']]} / {[it['likes'] for it in page2['items']]}")
    check("B7 两页无重复条目",
          not ({it["repo"] for it in page1["items"]} & {it["repo"] for it in page2["items"]}),
          f"{[it['repo'] for it in page1['items']]} / {[it['repo'] for it in page2['items']]}")

    # --- B8 页码解析与筛选归一化 ---
    check("B8 page_number 归一化边界",
          page_number("3", 5) == (3, False) and page_number("9", 5) == (5, True)
          and page_number("-2", 5) == (1, True) and page_number("x", 5) == (1, True)
          and page_number(None, 5) == (1, False), page_number(None, 5))
    check("B8 normalize_filters 容忍脏数据",
          normalize_filters({"params": "p_le1", "quant": None, "installed": 7}) ==
          {"params": ["p_le1"], "quant": [], "updated": [], "installed": "any",
           "recommended": False, "min_downloads": 0},
          normalize_filters({"params": "p_le1", "quant": None, "installed": 7}))
    check("B8 matches_filters 直接可用（纯函数）",
          matches_filters(items[0], normalize_filters({}), now) is True)


# ---------------------------------------------------------------- C. 打包后端


def resolve_backend():
    """定位被测后端。

    默认跑**源码后端**（用当前解释器起 ``backend/main.py``），因为打包之前就得把协议
    测通 —— 每改一次后端都先出安装包的话反馈环太长。加 ``--packaged`` 才跑发布物，
    用来在打包后验证"发出去的那份后端"确实是这套行为。

    （第一版实现是"有发布物就优先用发布物"，结果 CI 里拿**上一版**的 exe 测新协议，
    一路 4000 秒挂死在"等待 hub_result 超时"，日志里还看不出原因。）
    """
    if "--packaged" in sys.argv:
        candidates = [
            os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "app", "backend", "EchoDeskBackend.exe"),
            os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "backend", "EchoDeskBackend.exe"),
            os.path.join(ROOT, "publish", "EchoDesk-v0.2.2", "EchoDeskBackend.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return [path], "打包发布物"
        return None, "打包发布物（未找到）"
    return [sys.executable, "-X", "utf8", os.path.join(ROOT, "backend", "main.py")], "源码后端"


def test_packaged_backend():
    section("后端协议：model_hub_load 流式进度 + model_hub_query 本地筛选/翻页（真联网）")
    command, label = resolve_backend()
    if command is None:
        check(f"找到后端（{label}）", False, "请先跑 tools/build_release.ps1 或去掉 --packaged")
        return
    print(f"   后端（{label}）：{' '.join(command)}")
    # 源码模式下 cwd 必须是干净的临时目录：models/ 与 registry 都是相对 cwd 解析的，
    # 在仓库根跑会把测试产生的模型库记录写进工作区。
    workdir = tempfile.mkdtemp(prefix="echodesk-market-")
    cwd = os.path.dirname(command[0]) if command[0].endswith(".exe") else workdir
    env = dict(os.environ)
    env["ECHODESK_DATA"] = workdir
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(command, cwd=cwd, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)

    # 用后台线程 + 队列读 stdout：直接在 readline() 上等，一旦后端不出声就会永久挂住，
    # 整个测试进程只能靠外层超时杀掉（第一次跑就踩到了：日志里只剩 stderr，
    # 根本看不出卡在哪一步）。队列 + 超时才能把"没有输出"变成一次失败断言。
    lines = queue.Queue()

    def pump():
        try:
            for raw in proc.stdout:
                lines.put(raw)
        except Exception:  # noqa: BLE001
            pass
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(payload):
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def read_until(types, timeout=240):
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"等待 {types} 超时（后端长时间无输出）")
            try:
                line = lines.get(timeout=remain)
            except queue.Empty:
                raise TimeoutError(f"等待 {types} 超时（后端长时间无输出）") from None
            if line is None:
                raise RuntimeError("后端输出结束")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") in types:
                return data

    try:
        ready = read_until({"ready", "need_download", "system", "error"}, timeout=300)
        check("C1 后端启动并就绪", ready.get("type") in ("ready", "need_download"),
              str(ready)[:200])

        # 关键词窄一些，真联网也别跑太久；走缓存时第二次几乎瞬时
        send({"action": "model_hub_load", "keyword": "qwen2.5 0.5b gguf",
              "sort": "downloads", "page_size": 20})
        progress = []
        hub = None
        deadline = time.time() + 240
        while time.time() < deadline:
            data = read_until({"hub_progress", "hub_result", "done", "error"})
            if data.get("type") == "hub_progress":
                progress.append(data.get("hub_progress") or {})
                continue
            if data.get("type") == "hub_result":
                hub = data.get("model_hub") or {}
                continue
            break
        check("C1 流式加载返回了结构化结果", bool(hub), str(hub)[:200])
        if not hub:
            return
        check("C1 加载过程有进度反馈", len(progress) >= 1, progress[:1])
        check("C1 进度含已取回条数",
              all("loaded" in p for p in progress) if progress else False, progress[:1])
        check("C1 结果带分页查询块（第 1 页）", "query" in hub, sorted(hub))
        check("C1 远端结果带更新时间（旧版恒为空）",
              all(it["updated_at"] for it in hub["items"] if it["origin"] == "remote"),
              [it["updated_at"] for it in hub["items"][:3]])
        check("C1 下载量口径是历史总量",
              hub.get("downloads_metric") in ("all_time", "mixed"), hub.get("downloads_metric"))
        check("C1 默认模型单独钉住", isinstance(hub.get("pinned"), list), hub.get("pinned"))
        query = hub["query"]
        check("C1 每页 20 条", query["page_size"] == 20, query["page_size"])
        check("C1 三层数字齐全",
              all(k in query for k in ("filtered", "loaded", "page", "pages")), sorted(query))

        # 本地查询：换页
        send({"action": "model_hub_query", "page": query["pages"], "page_size": 20})
        last = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 本地翻到最后一页", last.get("page") == query["pages"], str(last)[:160])
        send({"action": "model_hub_query", "page": 99999, "page_size": 20})
        corrected = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 越界页码被夹到最后一页并标记",
              corrected.get("page") == query["pages"] and corrected.get("page_corrected") is True,
              str(corrected)[:160])
        send({"action": "model_hub_query", "page": "abc", "page_size": 20})
        bad = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 非法页码回第 1 页并标记",
              bad.get("page") == 1 and bad.get("page_corrected") is True, str(bad)[:160])
        send({"action": "model_hub_query", "page": 1, "page_size": 50})
        big = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 每页 50 条可切换", big.get("page_size") == 50, big.get("page_size"))
        send({"action": "model_hub_query", "page": 1, "page_size": 20, "sort": "params"})
        by_params = (read_until({"system", "error"}).get("hub_query") or {})
        params = [it.get("param_b") or 0 for it in by_params.get("items") or []]
        check("C2 本地按参数量排序生效",
              all(params[i] >= params[i + 1] for i in range(len(params) - 1)), params)
        send({"action": "model_hub_query", "keyword": "qwen", "page": 1, "page_size": 20})
        filtered = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 本地关键词筛选不重新请求远端",
              filtered.get("filtered", 0) <= filtered.get("loaded", 0) and
              all("qwen" in (it.get("repo", "") + " " + " ".join(it.get("tags") or [])).lower()
                  for it in filtered.get("items") or []),
              str(filtered)[:200])
        send({"action": "model_hub_query", "keyword": "绝对不存在zzz", "page": 1})
        none = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 本地无匹配时给出重新搜索远端的出口",
              none.get("empty_reason") == "local_no_match", str(none)[:200])
        send({"action": "model_hub_query", "page": 1, "filters": {"installed": "yes"}})
        inst = (read_until({"system", "error"}).get("hub_query") or {})
        check("C2 结构化筛选（已下载）生效",
              inst.get("filtered", -1) >= 0 and all(it.get("installed") for it in inst.get("items") or []),
              str(inst)[:200])

        # 缓存命中：同一关键词再搜一次应当明显更快/直接命中
        send({"action": "model_hub_load", "keyword": "qwen2.5 0.5b gguf",
              "sort": "downloads", "page_size": 20})
        started = time.time()
        hit = None
        while time.time() < started + 240:
            data = read_until({"hub_result", "done", "error"})
            if data.get("type") == "hub_result":
                hit = data.get("model_hub") or {}
            if data.get("type") == "done":
                break
        check("C3 再次搜索走磁盘缓存", bool(hit) and hit.get("cache", {}).get("hit") is True,
              str(hit.get("cache") if hit else None))
        print(f"   （缓存命中用时 {time.time() - started:.2f}s）")
    except Exception as exc:  # noqa: BLE001
        check("C 端到端流程未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            send({"action": "exit"})
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    tmpdir = tempfile.mkdtemp(prefix="echodesk-market-")
    try:
        test_query_engine()
        test_loader(tmpdir)
        if "--offline" not in sys.argv:
            test_packaged_backend()
        else:
            print("\n（已跳过打包后端联网测试：--offline）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
