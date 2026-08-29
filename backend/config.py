"""EchoDesk 后端配置管理。

支持"基础配置 + 用户补丁覆盖"（config.user.json 中的字段优先），
仿 DeepSeek-Harness 的 profile patch 机制：应用只写回基础配置，
用户手工补丁保持不变。
"""
import json
import os
import sys
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class Config(BaseModel):
    """应用配置，字段与 config.json 一一对应。"""

    model_path: str = "Qwen3.5-0.8B-Q4_K_M.gguf"
    model_repo: str = "unsloth/Qwen3.5-0.8B-GGUF"
    hf_endpoint: str = "https://hf-mirror.com"
    default_mode: str = "general"
    context_length: int = 4096
    max_history: int = 10
    search_default: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = 1024
    search_results: int = 3
    file_max_chars: int = 10000
    theme: str = "dark"
    warmup: bool = True
    # 推理性能：0 表示按本机 CPU 核数自动设置
    n_threads: int = 0
    n_batch: int = 512
    # GPU 加速层数：0 = 纯 CPU（有 NVIDIA 显卡可调大，如 99=全部）
    n_gpu_layers: int = 0
    # 会话持久化文件（空字符串表示关闭自动保存）
    session_file: str = "session.json"
    # 模型档案注册表：别名 -> {path, repo, context_length, temperature}
    models: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        """按优先级查找配置文件，并叠加用户补丁（config.user.json 同目录优先）。"""
        base_path = cls._find(path)
        if base_path is None:
            raise FileNotFoundError(f"找不到配置文件: {path}")
        with open(base_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 用户补丁覆盖层：与基础配置同目录的 config.user.json，字段优先
        user_path = os.path.join(os.path.dirname(base_path), "config.user.json")
        if os.path.exists(user_path):
            with open(user_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            data.update(user)

        return cls(**data)

    def save(self, path: str = "config.json") -> None:
        """将当前配置写回基础配置 JSON 文件（不触碰 config.user.json 覆盖层）。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4, ensure_ascii=False)

    @staticmethod
    def _find(path: str) -> str | None:
        """按优先级查找配置文件的真实路径。"""
        candidates = [os.path.join(os.getcwd(), path)]
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), path))
        if getattr(sys, "_MEIPASS", False):
            candidates.append(os.path.join(sys._MEIPASS, path))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), path))
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None
