"""AIProvider — 业务层唯一的 AI 能力抽象接口。

业务代码只 import 此 Protocol，不直接依赖 DeepTutor 或 DeepSeek。
更换 AI 平台只需新增实现类 + 在 DI 容器里换绑定，业务表不动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class KBInfo:
    name: str
    description: str = ""
    status: str = "ready"


@dataclass
class Chunk:
    """DeepTutor RAG 检索返回的一个 chunk。"""
    ref: str              # 外部引用（DeepTutor chunk id 或路径）
    text: str
    source_doc: str = ""
    score: float = 0.0


@dataclass
class GeneratedQuestion:
    """AI 出题结果。"""
    content: str
    type: int             # QuestionType enum 值
    difficulty: int       # Difficulty enum 值
    options: list | None = None
    answer: str = ""
    explanation: str = ""


@dataclass
class JudgeResult:
    """AI 判分结果。"""
    score: float          # 0-100
    is_correct: bool
    feedback: str = ""


@runtime_checkable
class AIProvider(Protocol):
    """业务层通过此接口获取 AI 能力 —— DeepTutor 实现 or DeepSeek 实现可热切换。"""

    # —— 知识库相关 ——
    def list_knowledge_bases(self) -> list[KBInfo]: ...
    def list_documents(self, kb_name: str) -> list[dict]: ...
    def get_document_text(self, kb_name: str, filename: str) -> str: ...
    def get_document_raw_url(self, kb_name: str, filename: str) -> str: ...
    def delete_document(self, kb_name: str, filename: str) -> bool: ...
    def retrieve_chunks(
        self, kb_name: str, query: str, top_k: int = 5
    ) -> list[Chunk]: ...
    def create_knowledge_base(self, name: str) -> dict: ...
    def delete_knowledge_base(self, kb_name: str) -> bool: ...
    def upload_documents(
        self, kb_name: str, files: list[tuple[str, bytes]]
    ) -> dict: ...
    def web_search(self, query: str) -> str: ...
    def add_web_source(
        self, kb_name: str, url: str, max_depth: int = 1, max_pages: int = 3
    ) -> dict: ...
    def sync_web_sources(self, kb_name: str) -> dict: ...
    def extract_knowledge_points(self, kb_name: str) -> list[dict]: ...

    # —— 出题 ——
    def generate_questions(
        self,
        context_chunks: list[Chunk],
        types: list[int],
        difficulty: int,
        count: int = 3,
    ) -> list[GeneratedQuestion]: ...

    # —— 知识点重要度评估（用于按权重分配出题量）——
    def rank_kp_importance(
        self, kb_name: str, kps: list[dict]
    ) -> dict[str, dict]: ...

    # —— 判分 ——
    def judge_answer(
        self, question_content: str, reference_answer: str, user_answer: str
    ) -> JudgeResult: ...

    # —— 语义相似度（替代 embedding + cosine，LLM 直接判断）——
    def semantic_similarity(self, text_a: str, text_b: str) -> float: ...

    # —— embedding（⚠️ 已弃用：DeepSeek 无公开 endpoint，降级走 LLM）——
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    # —— 动画讲解：给知识点生成 SVG/HTML 动画 + 讲解文字 ——
    def visualize_kp(
        self,
        kp_name: str,
        kp_content: str,
        kp_type: str = "concept",
        subject: str = "",
    ) -> dict:
        """返回 {"explanation": "讲解文字", "render_type": "svg|html", "render_content": "..."}"""
        ...
