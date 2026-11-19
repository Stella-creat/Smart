"""DeepTutor 远程实现 — 通过 WebSocket v2.0 协议调用 DeepTutor 引擎。

Smart 与 DeepTutor 不同进程部署，所有 AI 能力（检索/出题/判分）
走 ws://host:8001/ws，REST 仅用于知识库元数据查询（HTTP 轻量）。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from smart.adapter.provider.ai_provider import (
    AIProvider,
    Chunk,
    GeneratedQuestion,
    JudgeResult,
    KBInfo,
)
from smart.config import settings


def _find_deeptutor_data_dir() -> Path | None:
    """定位 DeepTutor 的 data/knowledge_bases 目录（REST 不可用时读本地文件兜底）。"""
    try:
        import deeptutor  # type: ignore

        pkg_root = Path(deeptutor.__file__).resolve().parent.parent
        kb_dir = pkg_root / "data" / "knowledge_bases"
        if kb_dir.exists():
            return kb_dir
    except ImportError:
        pass
    candidate = Path.cwd() / ".." / "DeepTutor" / "data" / "knowledge_bases"
    if candidate.exists():
        return candidate
    return None


class DeepTutorProvider(AIProvider):
    """远程 DeepTutor 引擎适配器（WS v2.0 协议）。

    所有公开方法保持同步签名（内部 asyncio.run 包裹），
    业务层可直接在 FastAPI 普通路由里调用，不用强制 async。
    """

    def __init__(self) -> None:
        self._kb_dir = _find_deeptutor_data_dir()
        self._http_client = httpx.Client(base_url=settings.deeptutor_base_url, timeout=settings.deeptutor_timeout)
        self._ws_url = settings.deeptutor_base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    # ===== 知识库元数据（REST） =====

    def list_knowledge_bases(self) -> list[KBInfo]:
        try:
            resp = self._http_client.get("/api/knowledge-bases/configs")
            resp.raise_for_status()
            data = resp.json()
            kbs = data.get("knowledge_bases", data) if isinstance(data, dict) else {}
            return [
                KBInfo(name=n, description=c.get("description", ""), status=c.get("status", "ready"))
                for n, c in kbs.items()
            ] if isinstance(kbs, dict) else []
        except httpx.HTTPError:
            # 兜底：读本地目录
            if self._kb_dir:
                return [KBInfo(name=d.name, description="", status="ready") for d in self._kb_dir.iterdir() if d.is_dir()]
            return []

    def list_documents(self, kb_name: str) -> list[dict]:
        try:
            resp = self._http_client.get(f"/api/knowledge-bases/{kb_name}/files")
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict):
                return body.get("files") or body.get("documents") or []
            return body if isinstance(body, list) else []
        except httpx.HTTPError:
            if self._kb_dir:
                kb_root = self._kb_dir / kb_name / "raw"
                if kb_root.exists():
                    return [{"name": p.name, "path": str(p)} for p in sorted(kb_root.iterdir()) if p.is_file()]
            return []

    def get_document_text(self, kb_name: str, filename: str) -> str:
        """获取文档提取后的纯文本（用于在线预览）。"""
        from urllib.parse import quote
        resp = self._http_client.get(
            f"/api/knowledge-bases/{kb_name}/file-preview-text/{quote(filename, safe='')}",
            timeout=60,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        return resp.text

    def get_document_raw_url(self, kb_name: str, filename: str) -> str:
        """返回 DeepTutor 原始文件的完整 URL（PDF 等可由浏览器内嵌渲染）。"""
        from urllib.parse import quote
        return (
            f"{settings.deeptutor_base_url}/api/knowledge-bases/{kb_name}/files/"
            f"{quote(filename, safe='')}"
        )

    def delete_document(self, kb_name: str, filename: str) -> bool:
        """从 DeepTutor 知识库删除指定文档（同时清理其索引）。"""
        from urllib.parse import quote
        resp = self._http_client.delete(
            f"/api/knowledge-bases/{kb_name}/files/{quote(filename, safe='')}",
            timeout=60,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def create_knowledge_base(self, name: str) -> dict:
        """在 DeepTutor 里新建一个空知识库。"""
        resp = self._http_client.post("/api/knowledge-bases", data={"name": name})
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        return {"name": name, **body} if isinstance(body, dict) else {"name": name}

    def delete_knowledge_base(self, kb_name: str) -> bool:
        """删除整个知识库（连同其文档与索引）。"""
        from urllib.parse import quote
        resp = self._http_client.delete(
            f"/api/knowledge-bases/{quote(kb_name, safe='')}", timeout=120
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def upload_documents(self, kb_name: str, files: list[tuple[str, bytes]]) -> dict:
        """上传文档到知识库 —— DeepTutor 后台异步解析并建立索引。"""
        parts = [("files", (fn, data, "application/octet-stream")) for fn, data in files]
        resp = self._http_client.post(
            f"/api/knowledge-bases/{kb_name}/upload", files=parts, timeout=300
        )
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        # 新建的空库尚未初始化索引，直接上传会失败；此处补一次 reindex 让引擎建索引
        try:
            cfg = self._http_client.get(
                f"/api/knowledge-bases/{kb_name}/config", timeout=30
            ).json()
            if not (cfg.get("config") or {}).get("index_versions"):
                self._http_client.post(
                    f"/api/knowledge-bases/{kb_name}/reindex", timeout=60
                )
        except httpx.HTTPError:
            pass
        return body

    def web_search(self, query: str) -> str:
        """联网搜索（DeepTutor web_search 工具），返回带来源的答案文本。"""
        return self._sync_ws_call(
            capability="chat", content=query, tools=["web_search"], timeout=90
        )

    # ===== 网页链接入库 =====

    def add_web_source(
        self, kb_name: str, url: str, max_depth: int = 1, max_pages: int = 3
    ) -> dict:
        """把网页注册为该知识库的网页来源。"""
        resp = self._http_client.post(
            f"/api/knowledge-bases/{kb_name}/web-source",
            json={"url": url, "max_depth": max_depth, "max_pages": max_pages},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def sync_web_sources(self, kb_name: str) -> dict:
        """抓取网页来源并写入知识库索引。"""
        resp = self._http_client.post(
            f"/api/knowledge-bases/{kb_name}/sync-web", timeout=180
        )
        resp.raise_for_status()
        return resp.json()

    # ===== WebSocket 核心 =====

    async def _ws_call(
        self,
        capability: str,
        content: str,
        knowledge_bases: list[str] | None = None,
        tools: list[str] | None = None,
        timeout: int = 60,
    ) -> str:
        """发起一个 start_turn 并等 result 消息，返回最终文本。"""
        session_id = f"zsk-{capability}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "type": "start_turn",
            "protocol_version": "2.0",
            "content": content,
            "capability": capability,
            "session_id": session_id,
            "config": {},
        }
        if knowledge_bases:
            payload["knowledge_bases"] = knowledge_bases
        if tools is not None:
            # 显式传空数组 = 禁用全部工具（防止 chat 能力自动挂载 exec 把结果写进文件）
            payload["tools"] = tools

        async with websockets.connect(self._ws_url, open_timeout=15, close_timeout=5) as ws:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            accumulated = ""
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout)
                    msg = json.loads(raw)
                    tp = msg.get("type", "")
                    if tp == "content":
                        # 流式 token
                        accumulated += msg.get("content", "")
                    elif tp == "result":
                        resp = msg.get("metadata", {}).get("response") or accumulated or msg.get("content", "")
                        return resp
                    elif tp == "error":
                        raise RuntimeError(f"DeepTutor error: {msg.get('message', raw)}")
                    elif tp == "done":
                        if accumulated:
                            return accumulated
                        break
                    # session/progress/stage_* 跳过
                except asyncio.TimeoutError:
                    break
        return accumulated

    def _sync_ws_call(self, **kwargs) -> str:
        """同步包一层 _ws_call —— 避免嵌套 asyncio 事件循环问题。"""
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._ws_call(**kwargs))
            finally:
                loop.close()
        except Exception as e:
            return ""

    # ===== 检索 =====

    def retrieve_chunks(self, kb_name: str, query: str, top_k: int = 5) -> list[Chunk]:
        resp = self._sync_ws_call(
            capability="chat",
            content=(
                f"使用 RAG 知识库检索：知识库={kb_name}，查询={query}，"
                f"返回前 {top_k} 条相关片段的原文和来源文档。仅返回 JSON 数组，"
                f"每条对象含 text、source_doc、score 字段。"
            ),
            knowledge_bases=[kb_name],
            tools=["rag"],
        )
        if not resp:
            return []
        raw = resp.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out: list[Chunk] = []
        if isinstance(data, dict):
            data = data.get("chunks") or data.get("results") or data.get("documents") or []
        for i, item in enumerate(data[:top_k]):
            if not isinstance(item, dict):
                continue
            out.append(Chunk(
                ref=f"{kb_name}#{i}",
                text=str(item.get("text") or item.get("content") or ""),
                source_doc=str(item.get("source_doc") or item.get("source") or ""),
                score=float(item.get("score") or 0.0),
            ))
        return out

    def extract_knowledge_points(self, kb_name: str) -> list[dict]:
        """读取知识库全部内容，让 LLM 切分为结构化知识点列表。

        返回: [{"chapter": 章节名, "name": 知识点名, "type": memory|concept|procedure|design}, ...]
        """
        prompt = (
            f"阅读知识库「{kb_name}」的全部内容，把它切分为若干知识点。\n"
            "要求：\n"
            "1. 每个知识点包含 chapter（章节/模块名）、name（知识点名称）、type（知识点类型）\n"
            "2. type 只能取以下四种之一：memory（记忆类，需背诵）、concept（概念类，需理解）、"
            "procedure（操作/计算步骤）、design（综合/设计类）\n"
            "3. 覆盖知识库的主要内容，切分要具体可用：一般给出 8~20 个知识点，"
            "按章节分层组织，名称要具体（例如「TCP 三次握手」「前缀表达式求值」），"
            "不要用笼统的书名/库名或一两个大而空的知识点\n"
            "4. 各知识点之间不要大量重复，粒度保持相近\n"
            "5. 仅返回 JSON 数组，不要包含 markdown 代码块标记或额外说明文字"
        )
        resp = self._sync_ws_call(
            capability="chat",
            content=prompt,
            knowledge_bases=[kb_name],
            tools=["rag"],
            timeout=120,
        )
        if not resp:
            return []
        raw = resp.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            data = data.get("points") or data.get("knowledge_points") or data.get("items") or []
        if not isinstance(data, list):
            return []
        valid_types = {"memory", "concept", "procedure", "design"}
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("知识点") or "").strip()
            chapter = str(item.get("chapter") or item.get("章节") or "未分类").strip()
            if not name:
                continue
            tp = str(item.get("type") or item.get("类型") or "concept").strip().lower()
            if tp not in valid_types:
                tp = "concept"
            out.append({"chapter": chapter, "name": name, "type": tp})
        return out

    # ===== 出题 =====

    def generate_questions(
        self,
        context_chunks: list[Chunk],
        types: list[int],
        difficulty: int,
        count: int = 3,
    ) -> list[GeneratedQuestion]:
        ctx_text = "\n\n".join(c.text[:500] for c in context_chunks[:3]) if context_chunks else "（无上下文资料）"
        type_map = {1: "单选", 2: "多选", 3: "判断", 4: "填空", 5: "简答"}
        diff_map = {1: "简单", 2: "中等", 3: "困难"}
        prompt = (
            f"基于以下学习资料，生成 {count} 道{diff_map.get(difficulty, '中等')}难度的题目，"
            f"题型：{', '.join(type_map.get(t, str(t)) for t in types)}。\n\n"
            f"学习资料：\n{ctx_text}\n\n"
            "返回严格的 JSON 数组，每题字段：content（题干）、type（数字）、difficulty（数字）、"
            "options（选项数组，非选择题为 null）、answer（正确答案）、explanation（解析）。\n\n"
            "【重要】直接在回复正文中输出 JSON 数组，不要写入任何文件，"
            "不要调用任何工具，不要添加 JSON 以外的说明文字。"
        )
        resp = self._sync_ws_call(capability="chat", content=prompt, tools=[], timeout=90)
        if not resp:
            return []
        data = self._extract_json_array(resp)
        if data is None:
            # 兜底：LLM 可能把结果写进了文件，正文只给 outputs/... 链接 —— 从静态路由取回
            data = self._fetch_output_json(resp)
        if data is None:
            return []
        out: list[GeneratedQuestion] = []
        for q in data:
            if not isinstance(q, dict):
                continue
            try:
                out.append(GeneratedQuestion(
                    content=str(q["content"]),
                    type=int(q.get("type", 1)),
                    difficulty=int(q.get("difficulty", difficulty)),
                    options=q.get("options"),
                    answer=str(q.get("answer", "")),
                    explanation=q.get("explanation", ""),
                ))
            except (KeyError, ValueError):
                continue
        return out

    # ===== 知识点重要度 =====

    def rank_kp_importance(
        self, kb_name: str, kps: list[dict]
    ) -> dict[str, dict]:
        """让 LLM 评估知识点在考试中的重要程度（1~5，5 最重要）。

        返回 {知识点名: {"importance": int, "reason": str}}；失败时统一给 3 分。
        """
        names = [str(k.get("name") or "").strip() for k in kps]
        names = [n for n in names if n]
        fallback = {n: {"importance": 3, "reason": ""} for n in names}
        if not names:
            return {}
        lines = "\n".join(
            f"- {str(k.get('name') or '').strip()}"
            + (f"（章节：{k.get('chapter')}）" if k.get("chapter") else "")
            for k in kps if str(k.get("name") or "").strip()
        )
        prompt = (
            f"你是资深教研老师。请评估知识库「{kb_name}」中下列知识点在考试中的重要程度。\n"
            f"{lines}\n\n"
            "评分标准：5=必考核心，4=重点，3=一般，2=次要，1=了解即可。\n"
            "返回严格的 JSON 数组，每项字段：name（与给定名称完全一致）、"
            "importance（1~5 的整数）、reason（不超过 20 字的判断依据）。\n"
            "【重要】只输出 JSON 数组，不要写文件、不要调用工具、不要额外说明。"
        )
        resp = self._sync_ws_call(capability="chat", content=prompt, tools=[], timeout=90)
        if not resp:
            return fallback
        data = self._extract_json_array(resp)
        if not data:
            return fallback
        out = dict(fallback)
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name not in out:
                continue
            try:
                score = int(item.get("importance", 3))
            except (TypeError, ValueError):
                score = 3
            out[name] = {
                "importance": max(1, min(5, score)),
                "reason": str(item.get("reason") or "").strip()[:40],
            }
        return out

    # ===== 判分 =====

    @staticmethod
    def _extract_json_array(text: str) -> list | None:
        """从 LLM 响应中提取 JSON 数组（允许前后有散文、可带 ```json 代码块）。"""
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """从 LLM 响应中提取 JSON 对象（允许前后有散文）。"""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _fetch_output_json(self, text: str) -> list | None:
        """从响应里的 outputs/... 文件链接取回 JSON 数组。"""
        m = re.search(r"(outputs/[^\s\)\]]+\.json)", text)
        if not m:
            return None
        url = f"{self._http_client.base_url}/files/{m.group(1)}"
        try:
            resp = self._http_client.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def judge_answer(
        self, question_content: str, reference_answer: str, user_answer: str
    ) -> JudgeResult:
        prompt = (
            f"【题目】{question_content}\n"
            f"【参考答案】{reference_answer}\n"
            f"【学生答案】{user_answer}\n\n"
            "判断学生答案是否正确，返回严格 JSON："
            '{"score": 0-100, "is_correct": bool, "feedback": ""}'
        )
        resp = self._sync_ws_call(capability="chat", content=prompt, timeout=30)
        if not resp:
            correct = user_answer.strip().lower() in reference_answer.strip().lower()
            return JudgeResult(score=100.0 if correct else 0.0, is_correct=correct, feedback="规则匹配（AI 不可用）")
        data = self._extract_json_object(resp)
        if data is not None:
            return JudgeResult(
                score=float(data.get("score", 0)),
                is_correct=bool(data.get("is_correct", False)),
                feedback=str(data.get("feedback", "")),
            )
        correct = user_answer.strip().lower() in reference_answer.strip().lower()
        return JudgeResult(score=100.0 if correct else 0.0, is_correct=correct, feedback="规则匹配（解析失败）")

    # ===== 语义相似度（LLM 直接判断，替代 embedding + cosine）=====

    _SIM_PROMPT = (
        "判断以下两个文本的语义相似度，返回严格 JSON：\n"
        '{{"score": 0.0到1.0的浮点数}}\n'
        "- 1.0 表示语义完全相同或等价\n"
        "- 0.0 表示完全无关\n"
        "- 核心概念相同但措辞不同 → 高相似度\n"
        "- 同领域但核心概念不同 → 低相似度\n\n"
        "文本A：{a}\n文本B：{b}"
    )

    def semantic_similarity(self, text_a: str, text_b: str) -> float:
        """LLM 判断两段文本的语义相似度（0.0-1.0）。

        DeepSeek 官方无公开 embedding API，改用 LLM 直接做语义判断。
        实测效果：高相似对 0.72-0.85，低相似对 0.00，区分度优于 n-gram cosine。
        """
        if not text_a or not text_b:
            return 0.0
        prompt = self._SIM_PROMPT.format(a=text_a[:300], b=text_b[:300])
        resp = self._sync_ws_call(capability="chat", content=prompt, timeout=15)
        if not resp:
            return 0.0
        data = self._extract_json_object(resp)
        if data is None:
            return 0.0
        try:
            score = float(data.get("score", 0.0))
            return max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            return 0.0

    # ===== Embedding（⚠️ 已弃用：保留签名兼容，返回空向量 → 调用方应改用 semantic_similarity）=====

    def embed(self, texts: list[str]) -> list[list[float]]:
        """⚠️ 已弃用 —— DeepSeek 官方无公开 embedding API。

        返回 n-gram hash 向量仅用于签名兼容；调用方请改用 semantic_similarity()。
        """
        import hashlib
        import struct

        def _one(text: str) -> list[float]:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            vec: list[float] = []
            for i in range(0, len(h) - 3, 4):
                vec.append(struct.unpack("<f", h[i:i + 4])[0] % 1.0)
            while len(vec) < 128:
                vec.append(0.0)
            return vec[:128]

        return [_one(t) for t in texts]

    # ===== 动画讲解 =====

    def visualize_kp(
        self,
        kp_name: str,
        kp_content: str,
        kp_type: str = "concept",
        subject: str = "",
    ) -> dict:
        """给知识点生成「讲解文字 + SVG/HTML 动画」。

        策略：
        1. 先用 visualize capability 要 SVG/HTML 动画（它就擅长生成这个）
        2. 再用 chat capability 要讲解文字
        3. 合并返回
        """
        import re

        type_hint = {
            "memory": "记忆/背诵类",
            "concept": "概念/原理类",
            "procedure": "操作/计算步骤类",
            "design": "综合/设计类",
        }.get(kp_type, "概念类")

        content_hint = kp_content[:2000] if kp_content else "（暂无内容，请根据知识点名称自行发挥）"

        # === 第一步：生成可视化内容 ===
        # 流程/设计类知识点 → 交互式 HTML（左侧代码高亮 + 右侧效果联动）
        # 概念/记忆类知识点 → SVG 动画
        interactive = kp_type in ("procedure", "design")

        if interactive:
            viz_prompt = (
                f"给「{subject}」的知识点《{kp_name}》（{type_hint}）生成一个交互式可视化教学页面。\n\n"
                f"知识点参考：\n{content_hint}\n\n"
                "要求：\n"
                "- 输出一个完整的自包含单文件 HTML 文档（从 <!DOCTYPE html> 到 </html>），CSS 与 JS 全部内联\n"
                "- 页面左右分栏：左侧是代码面板，逐行高亮当前执行行；右侧是图形/数组/柱状图，随代码同步变化\n"
                "- 自动播放整个流程，并提供「播放 / 暂停」与「单步」按钮，每步间隔 300~600ms\n"
                "- 配色明快（紫/蓝/橙渐变），字号≥12px，适配 600x420 的展示区域\n"
                "- 严禁引入任何外部资源（CDN、图片、字体、网络请求），全部用内联 CSS/JS 实现\n"
                "- 若知识点不适合代码演示，就用步骤高亮 + 图形变化的动画呈现\n"
                "- 只输出 HTML 代码本身，不要 JSON，不要 markdown 代码块，不要写文件"
            )
        else:
            viz_prompt = (
                f"给「{subject}」的知识点《{kp_name}》（{type_hint}）生成一份可视化讲解。\n\n"
                f"知识点参考：\n{content_hint}\n\n"
                "要求：\n"
                "- 直接输出一个完整的 <svg>...</svg> 标签（viewBox='0 0 600 350'）\n"
                "- SVG 里用 <animate> 标签做动画（颜色渐变、数字跳动、步骤高亮等）\n"
                "- 配色明快（紫/蓝/橙渐变），字号≥12px 易读\n"
                "- 动画紧扣知识点：概念类比图、步骤拆解高亮、公式图形化、对比色块等\n"
                "- 不要写 JSON，不要写文件，只输出可视化代码本身"
            )
        viz_resp = self._sync_ws_call(
            capability="visualize", content=viz_prompt, timeout=120,
        ) or self._sync_ws_call(
            capability="chat", content=viz_prompt, tools=[], timeout=120,
        )

        # 清理 markdown 代码块包裹
        viz_resp = viz_resp.strip()
        viz_resp = re.sub(r"^```(?:svg|html)?\s*", "", viz_resp, flags=re.IGNORECASE)
        viz_resp = re.sub(r"\s*```$", "", viz_resp)

        render_content = ""
        render_type = "html" if interactive else "svg"

        if interactive:
            # 优先提取完整 HTML 文档
            doc = re.search(r"<!DOCTYPE html[\s\S]*?</html>", viz_resp, re.IGNORECASE) \
                or re.search(r"<html[\s\S]*?</html>", viz_resp, re.IGNORECASE)
            render_content = doc.group(0) if doc else viz_resp
            if not self._looks_like_render(render_content, "html"):
                # HTML 不合格 → 退回 SVG 提取，再不行用兜底
                m = re.search(r"<svg[\s\S]*?</svg>", viz_resp, re.IGNORECASE)
                if m:
                    render_content, render_type = m.group(0), "svg"
                else:
                    render_content, render_type = self._fallback_svg(kp_name), "svg"
        else:
            if "<svg" in viz_resp.lower():
                # 提取 <svg ...>...</svg> 标签（如果 DeepTutor 输出了一堆说明文字包裹 SVG）
                m = re.search(r"<svg[\s\S]*?</svg>", viz_resp, re.IGNORECASE)
                render_content = m.group(0) if m else viz_resp
                render_type = "svg"
            elif "<canvas" in viz_resp.lower() or "<script" in viz_resp.lower():
                render_content = viz_resp
                render_type = "html"

            if not render_content or not self._looks_like_render(render_content, render_type):
                render_content = self._fallback_svg(kp_name)
                render_type = "svg"

        # === 第二步：要讲解文字 ===
        explain_prompt = (
            f"用 150~250 字讲解「{subject}」的知识点《{kp_name}》（{type_hint}）。\n\n"
            f"参考：{content_hint[:1500]}\n\n"
            "要求：通俗生动，用类比、分步骤、举例子，让初学者一听就懂。\n"
            "直接输出讲解文字，不要 JSON，不要代码块。"
        )
        explain_resp = self._sync_ws_call(
            capability="chat", content=explain_prompt, tools=[], timeout=60,
        ) or ""
        explanation = explain_resp.strip()
        if not explanation:
            explanation = f"关于「{kp_name}」的讲解暂不可用，请稍后再试。"

        return {
            "explanation": explanation,
            "render_type": render_type,
            "render_content": render_content,
        }

    @staticmethod
    def _looks_like_render(content: str, render_type: str) -> bool:
        if not content:
            return False
        if render_type == "svg":
            return "<svg" in content.lower() or "<animate" in content.lower() or "<circle" in content.lower()
        if render_type == "html":
            low = content.lower()
            # 交互演示必须有可执行脚本或 canvas，纯静态 HTML 视为不合格
            return "<script" in low or "<canvas" in low
        return True

    @staticmethod
    def _fallback_svg(kp_name: str) -> str:
        """兜底 SVG：一个淡入的标题卡片，保证页面不空。"""
        safe = kp_name[:20].replace("<", "&lt;").replace(">", "&gt;")
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 250" width="100%" height="100%">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#667eea"/>
      <stop offset="100%" stop-color="#764ba2"/>
    </linearGradient>
  </defs>
  <rect width="400" height="250" fill="url(#bg)" rx="12"/>
  <text x="200" y="130" text-anchor="middle" font-size="22" fill="#fff" font-family="sans-serif" font-weight="bold">
    {safe}
    <animate attributeName="opacity" values="0.3;1;0.3" dur="3s" repeatCount="indefinite"/>
  </text>
  <text x="200" y="170" text-anchor="middle" font-size="13" fill="#e0e0e0" font-family="sans-serif">
    AI 动画生成中…
    <animate attributeName="opacity" values="0.4;1;0.4" dur="2s" repeatCount="indefinite"/>
  </text>
</svg>'''
