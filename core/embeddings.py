"""EmbeddingClient —— KB 向量化抽象层。

设计：所有 KB embedding 走这个抽象，provider 可换不动调用方。

默认实现：SentenceTransformersClient (本地 offline)，model 默认
`intfloat/multilingual-e5-small`（118MB, 384 dim, 中英多语 / 100+ 语言）。
首次启动自动从 HuggingFace 下载到 `~/.cache/huggingface/`。

替换 provider：写新 class 实现 EmbeddingClient 接口，env var
`HARNESS_EMBEDDING_PROVIDER=maas|openai|local` 切换。

性能：sentence-transformers CPU 每秒 ~50-200 短文本；GPU 每秒数千。dogfood
阶段够用。10000 条 claim rebuild < 1 分钟。
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

import numpy as np

# huggingface `tokenizers`（sentence-transformers 的依赖）会在首次用到时起一个
# 并行线程池；这个模块加载完模型后，进程里几乎必然会再 fork/spawn 子进程
# （run_bash / execute_python / compile_latex 等工具），tokenizers 检测到
# "线程池活跃时被 fork" 会打印一遍警告防死锁——不是错误，但**每次 fork 都打
# 一遍**，chat.py 一个 session 里刷屏好几次。官方文档写的解法就是这行：在
# sentence_transformers 真正被 import 之前（见下面 SentenceTransformersClient
# 里的懒加载）把并行关掉，连触发条件都不成立，不是"抑制警告"，是从根上不让
# 它需要警告。放在这个模块（不是各个 CLI 入口分别设）——这是仓库里唯一
# import sentence_transformers 的地方，任何调用路径最终都会先跑到这行。
# setdefault：不覆盖用户自己显式设置的值（真想要并行可以自己设 =true）。
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

log = logging.getLogger("embeddings")


# ── EmbeddingClient 抽象 ────────────────────────────────────────────────────

class EmbeddingClient(ABC):
    """嵌入提供方的统一接口。

    实现要求：
      - embed(texts) 返 (N, dim) float32 ndarray，每行 L2 归一化（cosine 直算）
      - dim 是常量（同一 model 期间不变）
      - model_id 是稳定字符串，决定 manifest signature
    """
    model_id: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        """批量嵌入。空 list 返 shape (0, dim) 空矩阵。"""
        ...

    def embed_one(self, text: str) -> np.ndarray:
        """单条便捷方法。返 (dim,) 1-D 向量。"""
        return self.embed([text])[0]


# ── 默认：sentence-transformers 本地 ────────────────────────────────────────

DEFAULT_MODEL = os.getenv(
    "HARNESS_EMBEDDING_MODEL",
    "intfloat/multilingual-e5-small",
)


class SentenceTransformersClient(EmbeddingClient):
    """本地 sentence-transformers CPU/GPU 推理。

    懒加载：首次 embed 时才 load model（避免 import 时阻塞 / 自动测试时绕过）。
    模型缓存在 `~/.cache/huggingface/`，首次下载 ~120MB（multilingual-e5-small）。

    e5 系列要求 query / passage 加 prefix —— 但我们 KB dedup 是 symmetric 比较
    （claim vs claim），不区分 query/passage，统一用 'passage: ' prefix。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self.model_id = f"st:{model_name}"
        self._model = None
        self._dim: int | None = None

    @property
    def dim(self) -> int:  # type: ignore[override]
        if self._dim is None:
            self._ensure_loaded()
        return self._dim  # type: ignore[return-value]

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers 未安装（v0.x 起为 optional dependency）。\n"
                "修法 1（推荐，本仓库 root 跑）：\n"
                "    pip install -e \".[embeddings]\"\n"
                "修法 2：\n"
                "    pip install sentence-transformers\n"
                "或临时跳过 KB semantic dedup：\n"
                "    export HARNESS_DISABLE_SEMANTIC_DEDUP=1"
            ) from e
        log.info("Loading embedding model %s (lazy)...", self.model_name)
        try:
            self._model = SentenceTransformer(self.model_name)
        except Exception as e:
            raise RuntimeError(
                f"加载 embedding model {self.model_name!r} 失败：{type(e).__name__}: {e}\n\n"
                "可能原因：\n"
                "  1. 模型未下载且网络不通 —— 跑 `bash scripts/install_embedding_model.sh` 预下载\n"
                "     中国大陆慢：脚本已自动设 HF mirror（hf-mirror.com）\n"
                "  2. 离线 GPU server —— 在有网机器跑脚本，然后拷 `~/.cache/huggingface/`\n"
                "     到目标机，设 `HF_HOME=<拷过来的路径>` 指向它\n"
                "  3. 模型名错 —— 检查 `HARNESS_EMBEDDING_MODEL` env var\n"
                "  4. 临时绕过：`export HARNESS_DISABLE_SEMANTIC_DEDUP=1`（KB 仍可用，"
                "只是退到 substring 匹配）"
            ) from e
        # 探维度
        probe = self._model.encode(["probe"], show_progress_bar=False)
        self._dim = int(probe.shape[1])
        log.info("Model loaded: dim=%d", self._dim)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        self._ensure_loaded()
        # e5 prefix（symmetric 任务用 passage:）
        prefixed = [f"passage: {t}" for t in texts]
        emb = self._model.encode(  # type: ignore[union-attr]
            prefixed,
            show_progress_bar=False,
            normalize_embeddings=True,         # L2 归一化，cosine = 点积
            convert_to_numpy=True,
        )
        return emb.astype(np.float32)


# ── Stub providers（占位 —— 未来实现）─────────────────────────────────────

class MaasEmbeddingClient(EmbeddingClient):
    """团队 Maas embedding endpoint 占位。

    待 maas 提供 embedding endpoint 后实现（参考 LLMClient 写法）。
    """
    def __init__(self) -> None:
        raise NotImplementedError(
            "MaasEmbeddingClient 待实现。当前用 SentenceTransformersClient。"
        )

    def embed(self, texts):
        raise NotImplementedError()


class OpenAIEmbeddingClient(EmbeddingClient):
    """OpenAI text-embedding-3 占位。要 OPENAI_API_KEY。"""
    def __init__(self) -> None:
        raise NotImplementedError(
            "OpenAIEmbeddingClient 待实现。当前用 SentenceTransformersClient。"
        )

    def embed(self, texts):
        raise NotImplementedError()


# ── Factory ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_default_embedding_client() -> EmbeddingClient:
    """全局单例 client。重 import 不重 load model。

    Provider 选择优先级：
      HARNESS_EMBEDDING_PROVIDER env var → 'local' | 'maas' | 'openai'
      默认 'local'。
    """
    provider = os.getenv("HARNESS_EMBEDDING_PROVIDER", "local").lower()
    if provider == "local":
        return SentenceTransformersClient()
    if provider == "maas":
        return MaasEmbeddingClient()
    if provider == "openai":
        return OpenAIEmbeddingClient()
    raise ValueError(f"未知 HARNESS_EMBEDDING_PROVIDER={provider!r}")


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """归一化向量的 cosine 就是点积。

    a: (dim,) 单向量
    b: (N, dim) 矩阵
    返：(N,) 相似度
    """
    if b.ndim == 1:
        return float(np.dot(a, b))  # type: ignore[return-value]
    return b @ a   # (N, dim) @ (dim,) → (N,)


def cosine_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """批量 cosine：a (M, dim) × b (N, dim) → (M, N)。"""
    return a @ b.T
