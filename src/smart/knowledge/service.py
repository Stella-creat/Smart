"""知识库模块 — 知识点树 CRUD + DeepTutor 客户端封装。

DeepTutor 知识库是扁平的 RAG chunk 索引，不支持三级知识点树；
本模块自建知识点树（t_knowledge_point），通过适配层把 DeepTutor chunk
映射到知识点树上，形成业务层的"内容 → 知识点"闭环。
"""
from __future__ import annotations

import datetime as dt
import os
import re

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from smart.adapter.provider.ai_provider import AIProvider
from smart import models


# 掌握度四档阈值 —— 与前端 masteryLevel() 保持一致：
# <40 初学 / 40–70 练习 / 70–90 熟练 / ≥90 精通
MASTERY_LEVELS: tuple[str, ...] = ("初学", "练习", "熟练", "精通")
MASTERY_LEVEL_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (40, "初学"),
    (70, "练习"),
    (90, "熟练"),
)


def mastery_level(score: float) -> str:
    """掌握度 → 四档名称（与前端 masteryLevel() 严格一致）。"""
    for threshold, name in MASTERY_LEVEL_THRESHOLDS:
        if score < threshold:
            return name
    return MASTERY_LEVELS[-1]


# ===== 联网搜索结果 → PDF =====

_PDF_FONT_NAME = "ZSKCJK"
_PDF_FONT_READY = False
_PDF_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _ensure_pdf_font() -> str:
    """注册中文字体（Windows 用微软雅黑，Linux 回退 Noto/DejaVu）。"""
    global _PDF_FONT_READY
    if _PDF_FONT_READY:
        return _PDF_FONT_NAME
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _PDF_FONT_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        try:
            if path.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME, path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(_PDF_FONT_NAME, path))
            _PDF_FONT_READY = True
            return _PDF_FONT_NAME
        except Exception:
            continue
    return "Helvetica"


def _strip_markdown(line: str) -> str:
    """把一行 Markdown 粗略转成纯文本（保留可读性，去掉标记符号）。"""
    line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)          # 图片
    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", line)    # 链接
    line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)                  # 标题符号
    line = re.sub(r"^\s*>\s?", "", line)                           # 引用
    line = re.sub(r"^\s*[-*+]\s+", "· ", line)                     # 无序列表
    line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)                # 粗体
    line = re.sub(r"`([^`]*)`", r"\1", line)                       # 行内代码
    return line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_pdf(title: str, query: str, content: str, saved_at: dt.datetime) -> bytes:
    """把联网搜索结果渲染成 A4 PDF（标题 + 来源信息 + 正文）。"""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font = _ensure_pdf_font()
    title_style = ParagraphStyle(
        "zsk-title", fontName=font, fontSize=17, leading=24, spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "zsk-meta", fontName=font, fontSize=9.5, leading=15, textColor="#666666",
    )
    body_style = ParagraphStyle(
        "zsk-body", fontName=font, fontSize=11, leading=19, wordWrap="CJK",
        spaceAfter=4,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
        title=title,
    )
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    story = [
        Paragraph(safe_title, title_style),
        Spacer(1, 3 * mm),
        Paragraph(
            f"来源：联网搜索「{query}」<br/>保存时间：{saved_at:%Y-%m-%d %H:%M:%S}", meta_style
        ),
        Spacer(1, 5 * mm),
    ]
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 2.5 * mm))
            continue
        story.append(Paragraph(_strip_markdown(line), body_style))
    doc.build(story)
    return buf.getvalue()


class KnowledgeService:
    """业务层知识库服务 — 知识点树 CRUD + DeepTutor 取数封装。"""

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ===== 知识点树 =====

    def list_knowledge_points(
        self, db: Session, user_id: int, subject: str | None = None
    ) -> list[models.KnowledgePoint]:
        stmt = select(models.KnowledgePoint).where(
            (models.KnowledgePoint.user_id == user_id) | (models.KnowledgePoint.user_id == 0)
        )
        if subject:
            stmt = stmt.where(models.KnowledgePoint.subject == subject)
        stmt = stmt.order_by(models.KnowledgePoint.parent_id, models.KnowledgePoint.id)
        return list(db.execute(stmt).scalars().all())

    def create_knowledge_point(
        self,
        db: Session,
        user_id: int,
        subject: str,
        chapter: str,
        name: str,
        parent_id: int | None = None,
    ) -> models.KnowledgePoint:
        kp = models.KnowledgePoint(
            user_id=user_id, subject=subject, chapter=chapter,
            name=name, parent_id=parent_id,
        )
        db.add(kp)
        db.commit()
        db.refresh(kp)
        return kp

    def ensure_kp_for_kb(
        self, db: Session, user_id: int, kb_name: str
    ) -> models.KnowledgePoint:
        """知识库 → 占位知识点（科目/名称都取知识库名），供题库沿用知识点模型。"""
        row = db.execute(
            select(models.KnowledgePoint).where(
                models.KnowledgePoint.user_id == user_id,
                models.KnowledgePoint.subject == kb_name,
                models.KnowledgePoint.name == kb_name,
            )
        ).scalars().first()
        if row:
            return row
        return self.create_knowledge_point(db, user_id, kb_name, "知识库", kb_name)

    def ensure_subject_kp(self, db: Session, user_id: int, subject: str) -> None:
        """计划按科目排期：该科目没有任何知识点时补一个占位点，避免生成 0 条任务。"""
        row = db.execute(
            select(models.KnowledgePoint.id)
            .where(
                models.KnowledgePoint.user_id == user_id,
                models.KnowledgePoint.subject == subject,
            )
            .limit(1)
        ).first()
        if not row:
            self.create_knowledge_point(db, user_id, subject, "知识库", subject)

    def get_knowledge_point(self, db: Session, kp_id: int) -> models.KnowledgePoint | None:
        return db.get(models.KnowledgePoint, kp_id)

    # ===== 从 DeepTutor 取数 =====

    def sync_knowledge_bases(self) -> list[dict]:
        """列出 DeepTutor 里的所有知识库 —— 供前端选择同步哪些。"""
        return [kb.__dict__ for kb in self.provider.list_knowledge_bases()]

    def sync_documents(self, kb_name: str) -> list[dict]:
        """列出指定 DeepTutor 知识库的所有原始文档。"""
        return self.provider.list_documents(kb_name)

    def create_knowledge_base(self, name: str) -> dict:
        """在 DeepTutor 里新建知识库。"""
        return self.provider.create_knowledge_base(name)

    def kb_dependents(self, db: Session, user_id: int, kb_name: str) -> dict:
        """查询知识库关联的学习计划与题库，供删除前让用户选择。"""
        plan = db.execute(
            select(models.LearningPlan).where(
                models.LearningPlan.user_id == user_id,
                models.LearningPlan.subject == kb_name,
            )
        ).scalars().first()
        banks = db.execute(
            select(models.QuestionBank).where(
                models.QuestionBank.user_id == user_id,
                models.QuestionBank.kb_name == kb_name,
            )
        ).scalars().all()
        bank_rows = []
        for b in banks:
            cnt = db.execute(
                select(func.count()).select_from(models.Question).where(
                    models.Question.bank_id == b.id
                )
            ).scalar() or 0
            bank_rows.append({"id": b.id, "name": b.name, "question_count": cnt})
        return {
            "kb_name": kb_name,
            "plan": (
                {"id": plan.id, "duration_days": plan.duration_days}
                if plan else None
            ),
            "banks": bank_rows,
        }

    def delete_knowledge_base(
        self, db: Session, user_id: int, kb_name: str,
        delete_plan: bool = False, delete_bank: bool = False,
    ) -> dict:
        """删除知识库：先删引擎侧文档与索引，再清本地资源记录。

        delete_plan / delete_bank 由用户在删除确认框里勾选：
        关联数据未勾选时拒绝删除，避免计划任务或题库悬空。
        """
        dep = self.kb_dependents(db, user_id, kb_name)
        if dep["plan"] and not delete_plan:
            raise ValueError(
                f"「{kb_name}」已关联学习计划（#{dep['plan']['id']}），"
                "请勾选一并删除或先手动删除"
            )
        if dep["banks"] and not delete_bank:
            names = "、".join(b["name"] for b in dep["banks"])
            raise ValueError(
                f"「{kb_name}」已关联题库（{names}），请勾选一并删除或先手动删除"
            )

        if not self.provider.delete_knowledge_base(kb_name):
            raise FileNotFoundError(f"知识库「{kb_name}」不存在或已被删除")

        plan_ids = [dep["plan"]["id"]] if dep["plan"] and delete_plan else []
        if plan_ids:
            db.execute(
                models.PlanTask.__table__.delete().where(
                    models.PlanTask.plan_id.in_(plan_ids)
                )
            )
            db.execute(
                models.LearningPlan.__table__.delete().where(
                    models.LearningPlan.id.in_(plan_ids)
                )
            )

        bank_ids = [b["id"] for b in dep["banks"]] if delete_bank else []
        if bank_ids:
            db.execute(
                models.Question.__table__.delete().where(
                    models.Question.bank_id.in_(bank_ids)
                )
            )
            db.execute(
                models.QuestionBank.__table__.delete().where(
                    models.QuestionBank.id.in_(bank_ids)
                )
            )

        removed = db.execute(
            models.KBResource.__table__.delete().where(
                models.KBResource.user_id == user_id,
                models.KBResource.kb_name == kb_name,
            )
        ).rowcount
        db.commit()
        return {
            "deleted": True, "name": kb_name,
            "resources_removed": removed or 0,
            "plans_deleted": len(plan_ids),
            "banks_deleted": len(bank_ids),
        }

    def upload_documents(self, kb_name: str, files: list[tuple[str, bytes]]) -> dict:
        """上传文档到 DeepTutor 知识库（解析与索引由引擎后台完成）。"""
        return self.provider.upload_documents(kb_name, files)

    # ===== 知识库附属资源：文档记录 / 视频 / 链接 =====

    def list_resources(self, db: Session, user_id: int, kb_name: str) -> list[models.KBResource]:
        return (
            db.query(models.KBResource)
            .filter_by(user_id=user_id, kb_name=kb_name)
            .order_by(models.KBResource.id.desc())
            .all()
        )

    def add_document_records(
        self, db: Session, user_id: int, kb_name: str, files: list[tuple[str, int]]
    ) -> None:
        """文档以 DeepTutor 为权威源，这里只留存上传记录供前端展示。"""
        for name, size in files:
            db.add(
                models.KBResource(
                    user_id=user_id,
                    kb_name=kb_name,
                    kind=models.KbResourceKind.DOC.value,
                    title=name,
                    size_bytes=size,
                )
            )
        db.commit()

    def add_link(
        self, db: Session, user_id: int, kb_name: str, title: str, url: str
    ) -> tuple[models.KBResource, bool, str]:
        """链接入库：先登记为引擎网页来源并触发一次抓取，失败则仅保留链接。"""
        url = url.strip()
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("链接必须是以 http:// 或 https:// 开头的地址")
        row = models.KBResource(
            user_id=user_id,
            kb_name=kb_name,
            kind=models.KbResourceKind.LINK.value,
            title=title.strip() or url,
            url=url,
        )
        db.add(row)

        indexed, error = False, ""
        try:
            self.provider.add_web_source(kb_name, url)
            sync = self.provider.sync_web_sources(kb_name)
            results = sync.get("results") or []
            pages = sum(int(item.get("page_count") or 0) for item in results)
            if pages > 0:
                indexed = True
            else:
                error = next(
                    (str(item.get("error")) for item in results if item.get("error")),
                    "抓取未获得任何页面",
                )[:200]
        except Exception as exc:  # 抓取失败不影响链接留存
            error = str(exc)[:200]

        db.commit()
        db.refresh(row)
        return row, indexed, error

    def get_resource(
        self, db: Session, user_id: int, kb_name: str, resource_id: int
    ) -> models.KBResource | None:
        return (
            db.query(models.KBResource)
            .filter_by(id=resource_id, user_id=user_id, kb_name=kb_name)
            .first()
        )

    def delete_resource(
        self, db: Session, user_id: int, kb_name: str, resource_id: int
    ) -> bool:
        row = self.get_resource(db, user_id, kb_name, resource_id)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True

    # ===== 从知识库切分知识点 =====

    def extract_and_save_kps(
        self, db: Session, user_id: int, kb_name: str
    ) -> tuple[list[models.KnowledgePoint], int]:
        """调用 AI 读取知识库内容，切分为知识点并落库。

        返回: (该知识库下的全部知识点列表, 本次新建数量)
        知识点创建后 subject=知识库名，学习计划按 subject 自动纳入。
        """
        extracted = self.provider.extract_knowledge_points(kb_name)

        type_map = {
            "memory": models.KnowledgeType.MEMORY,
            "concept": models.KnowledgeType.CONCEPT,
            "procedure": models.KnowledgeType.PROCEDURE,
            "design": models.KnowledgeType.DESIGN,
        }

        created = 0
        for item in extracted:
            chapter = item.get("chapter") or "未分类"
            name = item.get("name") or ""
            if not name:
                continue
            # 幂等：同用户、同科目、同章节、同名不重复创建
            exists = db.execute(
                select(models.KnowledgePoint).where(
                    models.KnowledgePoint.user_id == user_id,
                    models.KnowledgePoint.subject == kb_name,
                    models.KnowledgePoint.chapter == chapter,
                    models.KnowledgePoint.name == name,
                )
            ).scalar_one_or_none()
            if exists:
                continue
            kp = models.KnowledgePoint(
                user_id=user_id,
                subject=kb_name,
                chapter=chapter,
                name=name,
                knowledge_type=type_map.get(item.get("type"), models.KnowledgeType.CONCEPT),
            )
            db.add(kp)
            created += 1
        db.commit()

        all_kps = self.list_knowledge_points(db, user_id, kb_name)
        return all_kps, created

    def search(self, kb_name: str, query: str, top_k: int = 5) -> list[dict]:
        """知识库语义检索 —— 返回命中的 chunk 原文与来源。"""
        return [c.__dict__ for c in self.provider.retrieve_chunks(kb_name, query, top_k=top_k)]

    def get_kp_explanation(self, db: Session, kp: "models.KnowledgePoint") -> str:
        """获取知识点的解析内容：优先读缓存列 content，否则用 AI 基于知识库生成结构化解析并缓存。"""
        cached = getattr(kp, "content", None)
        if cached:
            return cached
        prompt = (
            f"请基于知识库「{kp.subject}」的内容，为知识点「{kp.name}」写一份清晰易懂的学习解析。\n"
            "要求：\n"
            "1. 用中文，语言通俗，适合学生自学\n"
            "2. 结构清晰，使用以下 Markdown 格式：\n"
            "   - 用 ## 作为二级标题（如 ## 核心概念、## 关键公式、## 典型例题）\n"
            "   - 用 **加粗** 标记关键术语和重要结论\n"
            "   - 用 - 或 1. 2. 3. 列出要点\n"
            "   - 公式单独成行，用 $...$ 包裹行内公式\n"
            "3. 内容要准确，必须来自知识库，不要编造\n"
            "4. 长度适中，300-600 字，重点突出\n"
            "5. 直接输出解析内容，不要有开场白或结束语"
        )
        try:
            text = self.provider._sync_ws_call(
                capability="chat",
                content=prompt,
                knowledge_bases=[kp.subject],
                tools=["rag"],
                timeout=90,
            )
        except Exception:
            text = ""
        if not text or not text.strip():
            return f"（知识库「{kp.subject}」中暂未检索到「{kp.name}」的相关内容）"
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        # 写回缓存列
        try:
            from sqlalchemy import text as _sql_text
            db.execute(_sql_text(
                "UPDATE t_knowledge_point SET content = :c WHERE id = :id"
            ), {"c": text, "id": kp.id})
            db.commit()
        except Exception:
            pass
        return text

    def web_search(self, query: str) -> dict:
        """联网搜索 —— 由 DeepTutor web_search 工具给出带来源的答案。"""
        return {"answer": self.provider.web_search(query)}

    def save_web_result(
        self, kb_name: str, query: str, content: str, title: str | None = None,
        db=None, user_id: int | None = None,
    ) -> dict:
        """把联网搜索结果生成 PDF 文档，写入知识库并建立索引，同时登记本地文档记录。"""
        now = dt.datetime.now()
        heading = title or f"联网搜索：{query or '未命名'}"
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", (query or "web"))[:40].strip("_") or "web"
        filename = f"web_{safe}_{now:%Y%m%d_%H%M%S}.pdf"
        pdf_bytes = _build_pdf(heading, query, content, now)
        self.provider.upload_documents(kb_name, [(filename, pdf_bytes)])
        if db is not None and user_id is not None:
            self.add_document_records(db, user_id, kb_name, [(filename, len(pdf_bytes))])
        return {"filename": filename, "title": heading}

    def pull_chunks_for_matching(
        self, kb_name: str, query: str = "", top_k: int = 20
    ):
        """从 DeepTutor 拉 chunk —— 交给适配层 pipeline 做四级匹配。

        kb_name 预期是 Smart 知识点 subject（如"高数"），不一定和 DeepTutor 里的
        KB 名称一致。若精确匹配失败，自动 fallback 到 DeepTutor 里的第一个可用 KB。
        """
        if not query:
            query = "学习资料 知识点"
        # 先试精确
        chunks = self.provider.retrieve_chunks(kb_name, query, top_k=top_k)
        if chunks:
            return chunks
        # fallback：取 DeepTutor 第一个可用 KB
        kbs = self.provider.list_knowledge_bases()
        for kb in kbs:
            if kb.status == "ready" and kb.name != kb_name:
                chunks = self.provider.retrieve_chunks(kb.name, query, top_k=top_k)
                if chunks:
                    return chunks
        return []

    # ===== 统计 =====

    def mastery_summary(self, db: Session, user_id: int, subject: str) -> dict:
        points = self.list_knowledge_points(db, user_id, subject)
        if not points:
            return {
                "total": 0,
                "avg_mastery": 0.0,
                "by_level": {name: 0 for name in MASTERY_LEVELS},
            }
        avg = sum(p.mastery for p in points) / len(points)
        levels = {name: 0 for name in MASTERY_LEVELS}
        for p in points:
            levels[mastery_level(p.mastery)] += 1
        return {"total": len(points), "avg_mastery": round(avg, 1), "by_level": levels}
