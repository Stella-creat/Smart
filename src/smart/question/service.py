"""题库模块 — 三种创建路径 + CRUD + 查重 + AI 判分 + 答题提交。

DeepTutor 有 question_bank.py 但只做已答题目归类，不支持结构化题库管理，
本模块完全自建，但出题/判分能力复用 AIProvider.generate_questions / judge_answer。
"""
from __future__ import annotations

import io
import re
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import delete, select, func, update
from sqlalchemy.orm import Session

from smart.adapter.provider.ai_provider import AIProvider, Chunk, JudgeResult
from smart import models

# 题号：1. / 1、/ 1．/ （1）/ (1)
_NUM_HEAD = re.compile(r"^\s*(?:\d+\s*[.、．]|[（(]\s*\d+\s*[)）])\s*")
# 答案：答案：A / 答案:A / 参考答案：A / 正确答案：A
_ANSWER_RE = re.compile(r"(?:参考|正确)?答案\s*[:：]\s*(.+)")
# 解析：解析：xxx
_EXPLAIN_RE = re.compile(r"解析\s*[:：]\s*(.+)")
# 选项起始：A. / A、/ A．
_OPT_START = re.compile(r"^[A-D]\s*[.、．]\s*")
# 同一行多个选项：A．甲 B．乙 C．丙 D．丁
_INLINE_OPT = re.compile(r"\s+(?=[A-D]\s*[.、．])")


# 知识点重要度缓存：{(知识库, 知识点名): {"importance", "reason"}}
_IMPORTANCE_CACHE: dict[tuple[str, str], dict] = {}


class QuestionService:
    """业务层题库服务。"""

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ===== 题库（按科目划分）=====

    def list_banks(self, db: Session, user_id: int) -> list[models.QuestionBank]:
        """返回用户的题库列表；首次调用时把历史无归属题目按科目幂等回填。"""
        self._backfill_banks(db, user_id)
        stmt = (
            select(models.QuestionBank)
            .where(models.QuestionBank.user_id == user_id)
            .order_by(models.QuestionBank.id)
        )
        return list(db.execute(stmt).scalars().all())

    def _backfill_banks(self, db: Session, user_id: int) -> None:
        """把 bank_id 为空的历史题目按 KnowledgePoint.subject 归入同名题库。"""
        subjects = list(
            db.execute(
                select(models.KnowledgePoint.subject)
                .join(
                    models.Question,
                    models.Question.knowledge_point_id == models.KnowledgePoint.id,
                )
                .where(
                    models.Question.user_id == user_id,
                    models.Question.bank_id.is_(None),
                )
                .distinct()
            ).scalars().all()
        )
        if not subjects:
            return
        existing = {
            bank.kb_name: bank
            for bank in db.execute(
                select(models.QuestionBank).where(models.QuestionBank.user_id == user_id)
            ).scalars().all()
        }
        for subject in subjects:
            if not subject:
                continue
            bank = existing.get(subject)
            if bank is None:
                bank = models.QuestionBank(user_id=user_id, name=subject, kb_name=subject)
                db.add(bank)
                db.flush()
                existing[subject] = bank
            # 该科目下仍未归属题库的题目一次性回填
            db.execute(
                update(models.Question)
                .where(
                    models.Question.user_id == user_id,
                    models.Question.bank_id.is_(None),
                    models.Question.knowledge_point_id.in_(
                        select(models.KnowledgePoint.id).where(
                            models.KnowledgePoint.subject == subject
                        )
                    ),
                )
                .values(bank_id=bank.id)
            )
        db.commit()

    def create_bank(
        self, db: Session, user_id: int, name: str, kb_name: str
    ) -> models.QuestionBank:
        """新建题库；同一用户下题库名不可重复。"""
        name = (name or "").strip()
        kb_name = (kb_name or "").strip()
        if not name:
            raise ValueError("题库名称不能为空")
        if not kb_name:
            raise ValueError("请选择关联知识库")
        dup = db.execute(
            select(models.QuestionBank.id).where(
                models.QuestionBank.user_id == user_id,
                models.QuestionBank.name == name,
            )
        ).first()
        if dup:
            raise ValueError(f"题库「{name}」已存在，请换一个名称")
        bank = models.QuestionBank(user_id=user_id, name=name, kb_name=kb_name)
        db.add(bank)
        db.commit()
        db.refresh(bank)
        return bank

    def delete_bank(self, db: Session, user_id: int, bank_id: int) -> bool:
        """删除题库及其下的题目；知识点 / 答题记录 / 计划任务不动。"""
        bank = db.get(models.QuestionBank, bank_id)
        if bank is None or bank.user_id != user_id:
            return False
        db.execute(delete(models.Question).where(models.Question.bank_id == bank_id))
        db.delete(bank)
        db.commit()
        return True

    # ===== CRUD =====

    def list_questions(
        self,
        db: Session,
        user_id: int,
        kp_id: int | None = None,
        qtype: int | None = None,
        difficulty: int | None = None,
        kb_name: str | None = None,
        bank_id: int | None = None,
    ) -> list[models.Question]:
        stmt = select(models.Question).where(
            (models.Question.user_id == user_id) | (models.Question.user_id == 0)
        )
        if bank_id is not None:
            stmt = stmt.where(models.Question.bank_id == bank_id)
        if kb_name:
            # 知识库 → 科目：该科目下所有知识点（含历史知识点）的题目都算这个库的
            stmt = stmt.join(
                models.KnowledgePoint,
                models.Question.knowledge_point_id == models.KnowledgePoint.id,
            ).where(models.KnowledgePoint.subject == kb_name)
        if kp_id is not None:
            stmt = stmt.where(models.Question.knowledge_point_id == kp_id)
        if qtype is not None:
            stmt = stmt.where(models.Question.type == qtype)
        if difficulty is not None:
            stmt = stmt.where(models.Question.difficulty == difficulty)
        return list(db.execute(stmt).scalars().all())

    def create_question(
        self,
        db: Session,
        user_id: int,
        kp_id: int,
        qtype: int,
        difficulty: int,
        content: str,
        answer: str,
        options: list | None = None,
        explanation: str | None = None,
        source: str = "MANUAL",
        bank_id: int | None = None,
    ) -> models.Question:
        question = models.Question(
            user_id=user_id, knowledge_point_id=kp_id, type=qtype,
            difficulty=difficulty, content=content, answer=answer,
            options=options, explanation=explanation, source=source,
            bank_id=bank_id,
        )
        db.add(question)
        db.commit()
        db.refresh(question)
        return question

    # ===== 路径 1：批量导入（IMPORT）=====

    def bulk_import(
        self,
        db: Session,
        user_id: int,
        kp_id: int,
        raw_text: str,
        qtype: int = 1,
        difficulty: int = 2,
        batch_size: int = 100,
        bank_id: int | None = None,
    ) -> int:
        """从纯文本/Word 提取的题库内容中批量导入。

        简单策略：按 "数字. 题干" 正则切分，再找 "A./B./C." 切选项。
        生产环境应接入 DeepTutor 的文档解析能力做更精准的题型识别。
        """
        pattern = re.compile(r"\d+[.、]\s*(.+?)(?=\n\s*\d+[.、]|\Z)", re.DOTALL)
        count = 0
        batch: list[models.Question] = []
        for match in pattern.finditer(raw_text):
            block = match.group(1).strip()
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue
            q_content = lines[0]
            options, answer = self._parse_options_and_answer(lines[1:])
            if self._is_duplicate(db, q_content):
                continue
            batch.append(models.Question(
                user_id=user_id, knowledge_point_id=kp_id, type=qtype,
                difficulty=difficulty, content=q_content, answer=answer,
                options=options, source="IMPORT", bank_id=bank_id,
            ))
            count += 1
            if len(batch) >= batch_size:
                db.bulk_save_objects(batch)
                db.commit()
                batch.clear()
        if batch:
            db.bulk_save_objects(batch)
            db.commit()
        return count

    def _parse_options_and_answer(self, lines: list[str]) -> tuple[list | None, str]:
        """从 "A. xxx / B. xxx / 答案：A" 格式行里抽选项和答案。"""
        opts: list[str] = []
        answer = ""
        answer_pattern = re.compile(r"(?:答案|参考答案)[:：]\s*(.+)")
        for line in lines:
            m = answer_pattern.search(line)
            if m:
                answer = m.group(1).strip()
                continue
            opt_match = re.match(r"^([A-D])[.、]\s*(.+)", line)
            if opt_match:
                opts.append(opt_match.group(2).strip())
        return (opts if opts else None), answer

    # ===== 路径 1.5：上传题库文档 =====

    def extract_text(self, filename: str, data: bytes) -> str:
        """解析上传的题库文档：txt / md / docx / pdf。"""
        if not data:
            raise ValueError("文件内容为空")
        name = (filename or "").strip()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("txt", "md"):
            return self._decode_text(data)
        if ext == "docx":
            return self._extract_docx_text(data)
        if ext == "pdf":
            return self._extract_pdf_text(data)
        raise ValueError("仅支持 txt/md/docx/pdf 格式")

    @staticmethod
    def _decode_text(data: bytes) -> str:
        """纯文本解码：utf-8 优先，gbk 兜底。"""
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _extract_docx_text(data: bytes) -> str:
        """docx 逐段落 + 表格单元格取文本。"""
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读提示
            raise ValueError("服务端缺少 python-docx，暂无法解析 docx") from exc
        try:
            doc = Document(io.BytesIO(data))
        except Exception as exc:
            raise ValueError("docx 文件解析失败，请确认文件未损坏") from exc
        lines = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    lines.append(" ".join(cells))
        text = "\n".join(lines).strip()
        if not text:
            raise ValueError("文件内容为空")
        return text

    @staticmethod
    def _extract_pdf_text(data: bytes) -> str:
        """pdf 逐页 extract_text。"""
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ValueError("服务端缺少 pypdf，暂无法解析 pdf") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:
            raise ValueError("pdf 文件解析失败，请确认文件未损坏") from exc
        text = "\n".join(pages).strip()
        if not text:
            raise ValueError("文件内容为空")
        return text

    def import_from_text(
        self,
        db: Session,
        user_id: int,
        kp_id: int,
        raw_text: str,
        qtype: int = 1,
        difficulty: int = 2,
        batch_size: int = 100,
        bank_id: int | None = None,
    ) -> tuple[int, int]:
        """容错批量导入：中英文编号、行内选项、多种答案写法都能切分。

        返回 (成功导入数, 解析失败或去重跳过数)。
        """
        imported = 0
        skipped = 0
        seen: set[str] = set()
        batch: list[models.Question] = []
        for block in self._split_question_blocks(raw_text):
            parsed = self._parse_question_block(block)
            if parsed is None:
                skipped += 1
                continue
            content, options, answer, explanation = parsed
            key = re.sub(r"\s+", "", content)[:30]
            if not key or key in seen or self._is_duplicate(db, content):
                skipped += 1
                continue
            seen.add(key)
            batch.append(models.Question(
                user_id=user_id, knowledge_point_id=kp_id, type=qtype,
                difficulty=difficulty, content=content, answer=answer,
                options=options, explanation=explanation, source="IMPORT",
                bank_id=bank_id,
            ))
            imported += 1
            if len(batch) >= batch_size:
                db.bulk_save_objects(batch)
                db.commit()
                batch.clear()
        if batch:
            db.bulk_save_objects(batch)
            db.commit()
        return imported, skipped

    @staticmethod
    def _split_question_blocks(raw_text: str) -> list[str]:
        """按题号把全文切成题目块；首个题号之前的内容忽略。"""
        text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
        blocks: list[str] = []
        current: list[str] = []
        for line in text.split("\n"):
            if _NUM_HEAD.match(line):
                if current:
                    blocks.append("\n".join(current))
                current = [_NUM_HEAD.sub("", line, count=1)]
            elif current:
                current.append(line)
        if current:
            blocks.append("\n".join(current))
        return blocks

    @staticmethod
    def _parse_question_block(block: str) -> tuple[str, list | None, str, str | None] | None:
        """解析单题：题干、选项、答案、解析；失败返回 None。"""
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            return None
        answer = ""
        explanation = ""
        body: list[str] = []
        for line in lines:
            m = _ANSWER_RE.search(line)
            if m and not answer:
                answer = m.group(1).strip()
                line = (line[:m.start()] + " " + line[m.end():]).strip()
            m2 = _EXPLAIN_RE.search(line)
            if m2 and not explanation:
                explanation = m2.group(1).strip()
                line = (line[:m2.start()] + " " + line[m2.end():]).strip()
            if line:
                body.append(line)
        if not body:
            return None
        # 题干与选项同行：题干取首个选项标记前的文本
        first = body[0]
        m = re.search(r"[A-D]\s*[.、．]", first)
        if m and m.start() > 0:
            content = first[:m.start()].strip()
            rest = [first[m.start():]] + body[1:]
        else:
            content = first.strip()
            rest = body[1:]
        if not content:
            return None
        options: list[str] = []
        for line in rest:
            for part in _INLINE_OPT.split(line):
                om = _OPT_START.match(part.strip())
                if om:
                    options.append(part.strip()[om.end():].strip())
        return content, (options if options else None), answer, (explanation or None)

    # ===== 路径 2：AI 出题（AI）=====

    def ai_generate(
        self,
        db: Session,
        user_id: int,
        kp_id: int,
        context_chunks: list[Chunk],
        types: list[int],
        difficulty: int,
        count: int = 3,
        bank_id: int | None = None,
    ) -> list[models.Question]:
        results = self.provider.generate_questions(context_chunks, types, difficulty, count)
        saved: list[models.Question] = []
        for r in results:
            if self._is_duplicate(db, r.content):
                continue
            q = self.create_question(
                db, user_id=user_id, kp_id=kp_id, qtype=r.type, difficulty=r.difficulty,
                content=r.content, answer=r.answer, options=r.options,
                explanation=r.explanation, source="AI", bank_id=bank_id,
            )
            saved.append(q)
        return saved

    def ai_generate_for_kps(
        self,
        db: Session,
        user_id: int,
        bank_id: int,
        kb_name: str,
        kp_names: list[str],
        types: list[int],
        difficulty: int,
        count_per_kp: int = 2,
        weighted: bool = True,
    ) -> tuple[list[models.Question], dict[str, dict]]:
        """按知识点逐个出题：知识点名作检索 query，题目挂到该知识点真实 id。

        weighted=True 时先让模型评估各知识点重要程度（1~5），
        再按权重把「总题量 = 每知识点题数 × 知识点数」重新分配：
        每个知识点至少 1 道，权重高的多分，单个知识点最多 count_per_kp*2 道。
        返回 (题目列表, {知识点名: {"count", "importance", "reason"}})。
        """
        from smart.knowledge.service import KnowledgeService

        kb = (kb_name or "").strip()
        names = [(n or "").strip() for n in kp_names if (n or "").strip()]

        # ① 重要度评估（带进程内缓存，同一知识库同一知识点只问一次）
        ranking: dict[str, dict] = {n: {"importance": 3, "reason": ""} for n in names}
        if weighted and names:
            todo = [
                n for n in names
                if (kb, n) not in _IMPORTANCE_CACHE
            ]
            if todo:
                try:
                    got = self.provider.rank_kp_importance(
                        kb, [{"name": n} for n in todo]
                    ) or {}
                except Exception:
                    got = {}
                for n in todo:
                    _IMPORTANCE_CACHE[(kb, n)] = got.get(
                        n, {"importance": 3, "reason": ""}
                    )
            for n in names:
                ranking[n] = _IMPORTANCE_CACHE.get(
                    (kb, n), {"importance": 3, "reason": ""}
                )

        # ② 按重要度分档分配题量：5★多出、4★基准、3★少出、≤2★不出
        allocation = {n: count_per_kp for n in names}
        if weighted and names:
            allocation = {}
            for n in names:
                w = max(1, int(ranking[n]["importance"]))
                if w <= 2:
                    allocation[n] = 0
                else:
                    allocation[n] = max(
                        1, min(count_per_kp + (w - 4), count_per_kp * 2)
                    )
            # 全部知识点都是次要项时，至少给最重要的一个出题
            if not any(allocation.values()) and names:
                top = max(names, key=lambda n: int(ranking[n]["importance"]))
                allocation[top] = count_per_kp

        saved: list[models.Question] = []
        per_kp: dict[str, dict] = {}
        for name in names:
            kp = db.execute(
                select(models.KnowledgePoint).where(
                    (models.KnowledgePoint.user_id == user_id)
                    | (models.KnowledgePoint.user_id == 0),
                    models.KnowledgePoint.subject == kb,
                    models.KnowledgePoint.name == name,
                )
            ).scalars().first()
            if kp is None:
                # 知识点被删或名字对不上时，退化为该知识库的占位知识点
                kp = KnowledgeService(self.provider).ensure_kp_for_kb(db, user_id, kb)
            want = allocation.get(name, count_per_kp)
            if want <= 0:
                # 次要知识点（重要度 ≤2）本轮不出题
                per_kp[name] = {
                    "count": 0,
                    "planned": 0,
                    "importance": int(ranking[name]["importance"]),
                    "reason": ranking[name].get("reason") or "",
                }
                continue
            query = f"{kb} {name}".strip() if kb else name
            chunks = self.provider.retrieve_chunks(kb, query, top_k=6)
            results = self.provider.generate_questions(chunks, types, difficulty, want)
            added = 0
            for r in results:
                if self._is_duplicate(db, r.content):
                    continue
                saved.append(self.create_question(
                    db, user_id=user_id, kp_id=kp.id, qtype=r.type,
                    difficulty=r.difficulty, content=r.content, answer=r.answer,
                    options=r.options, explanation=r.explanation, source="AI",
                    bank_id=bank_id,
                ))
                added += 1
            per_kp[name] = {
                "count": added,
                "planned": want,
                "importance": int(ranking[name]["importance"]),
                "reason": ranking[name].get("reason") or "",
            }
        return saved, per_kp

    # ===== 查重 =====

    def _is_duplicate(self, db: Session, content: str, threshold: float = 0.85) -> bool:
        """简单查重：题干精确匹配（生产环境应用 embedding 相似度）。"""
        stmt = select(models.Question.content).where(
            func.lower(models.Question.content).like(f"%{content[:30].lower()}%")
        ).limit(1)
        return db.execute(stmt).first() is not None

    # ===== 配题（供 plan 模块调用）=====

    def pick_for_kp(
        self,
        db: Session,
        kp_id: int,
        mastery: float,
        count: int = 3,
    ) -> list[models.Question]:
        """按掌握度反配题：mastery 低 → 中等偏难；mastery 高 → 简单少练。"""
        target_diff = 2 if mastery < 55 else (1 if mastery >= 85 else 2)
        stmt = select(models.Question).where(
            (models.Question.knowledge_point_id == kp_id)
            & (models.Question.difficulty == target_diff)
        ).limit(count)
        picked = list(db.execute(stmt).scalars().all())
        if len(picked) < count:
            # 不够就放宽难度
            stmt2 = select(models.Question).where(
                models.Question.knowledge_point_id == kp_id
            ).limit(count - len(picked))
            picked += list(db.execute(stmt2).scalars().all())
        return picked

    # ===== AI 判分 =====

    def judge(self, question_id: int, user_answer: str) -> JudgeResult:
        """纯 AI 判分（不写库，供前端单独调用）。"""
        # 找题 —— 这里只是判分，不涉及 user_id
        # 调用方应该已经拿到题目了，直接传 q.content + q.answer 也行
        # 但路由层会从 DB 查好传进来，这里接收已解析的题目字段
        raise NotImplementedError("请用 judge_with_fields")

    def judge_with_fields(
        self, question_content: str, reference_answer: str, user_answer: str
    ) -> JudgeResult:
        """AI 判分 —— provider 可能走 DeepTutor WS 或 DeepSeek 直连。"""
        return self.provider.judge_answer(question_content, reference_answer, user_answer)

    # ===== 答题提交（完整流程） =====

    def submit_and_judge(
        self,
        db: Session,
        user_id: int,
        question_id: int,
        user_answer: str,
        task_id: int | None = None,
        duration_seconds: int | None = None,
    ) -> tuple[JudgeResult, models.AttemptRecord | None]:
        """完整答题流程：AI 判分 → 写 AttemptRecord → 返回判分结果。

        AttemptRecord 是 mastery 算法的原始数据源，练习任务完成后回流掌握度。
        """
        q = db.get(models.Question, question_id)
        if q is None:
            raise ValueError(f"题目 {question_id} 不存在")

        # 客观题（单选/多选/判断）走规则判分，主观题走 AI
        judge_result = self._auto_or_ai_judge(q, user_answer)

        # 写 AttemptRecord（如果是计划里的练习任务则关联 task_id）
        record: models.AttemptRecord | None = None
        if judge_result.is_correct or task_id is not None:
            record = models.AttemptRecord(
                user_id=user_id,
                knowledge_point_id=q.knowledge_point_id,
                task_id=task_id,
                is_correct=judge_result.is_correct,
                duration_seconds=duration_seconds,
            )
            db.add(record)
            db.commit()
            db.refresh(record)

        return judge_result, record

    # 各题型分组默认分值：选择 / 判断 / 主观
    DEFAULT_POINTS = {"choice": 2.0, "judge": 2.0, "subjective": 5.0}

    @classmethod
    def _normalize_points(cls, points: dict | None) -> dict:
        """把前端传来的分值配置补全/纠错；非法或小于等于 0 时回落到默认值。"""
        result = dict(cls.DEFAULT_POINTS)
        if isinstance(points, dict):
            for key in result:
                try:
                    val = float(points.get(key))
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    result[key] = val
        return result

    @staticmethod
    def _points_for_type(qtype: int | None, points: dict) -> float:
        """按题型取每题分值：1/2 选择、3 判断、4/5 主观。"""
        if qtype == 3:
            return points["judge"]
        if qtype in (4, 5):
            return points["subjective"]
        return points["choice"]

    def practice_batch(
        self,
        db: Session,
        user_id: int,
        bank_id: int,
        answers: list[dict],
        duration_seconds: int | None = None,
        points: dict | None = None,
    ) -> dict:
        """整卷练习提交：逐题复用 submit_and_judge 判分后汇总。

        answers 每项 ``{question_id, user_answer}``。只接受该用户该题库内的
        题目，非法题目跳过并在 items 里标记 skipped=True（计入 total，但不计入
        answered/correct，也不计入 full_score）。空答案视为未作答，按错误计入
        items 且不触发 AI 判分。

        points 为 ``{choice, judge, subjective}`` 每题分值，默认 2/2/5；
        full_score 为本卷作答题目分值之和，earned_score 为答对题目分值之和。
        """
        pts = self._normalize_points(points)
        items: list[dict] = []
        answered = 0
        correct = 0
        full_score = 0.0
        earned_score = 0.0
        for raw in answers or []:
            try:
                qid = int(raw.get("question_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            user_answer = str(raw.get("user_answer") or "").strip()
            q = db.get(models.Question, qid)
            if q is None or q.bank_id != bank_id or q.user_id not in (user_id, 0):
                items.append({
                    "question_id": qid,
                    "user_answer": user_answer,
                    "answer": None,
                    "is_correct": False,
                    "score": 0.0,
                    "feedback": "题目不存在或不属于该题库，已跳过",
                    "content": None,
                    "options": None,
                    "type": None,
                    "explanation": None,
                    "points": 0.0,
                    "skipped": True,
                })
                continue
            if user_answer:
                judge_result, _record = self.submit_and_judge(
                    db, user_id, qid, user_answer,
                    duration_seconds=duration_seconds,
                )
                answered += 1
            else:
                judge_result = JudgeResult(
                    score=0.0,
                    is_correct=False,
                    feedback=f"未作答，参考答案 {q.answer}",
                )
            if judge_result.is_correct:
                correct += 1
            item_points = self._points_for_type(q.type, pts)
            full_score += item_points
            if judge_result.is_correct:
                earned_score += item_points
            items.append({
                "question_id": qid,
                "user_answer": user_answer,
                "answer": q.answer,
                "is_correct": judge_result.is_correct,
                "score": judge_result.score,
                "feedback": judge_result.feedback,
                "content": q.content,
                "options": q.options,
                "type": q.type,
                "explanation": q.explanation,
                "points": item_points,
                "skipped": False,
            })
        total = len(items)
        full_score = round(full_score, 2)
        earned_score = round(earned_score, 2)
        score = round(earned_score / full_score * 100, 1) if full_score else 0
        return {
            "total": total,
            "answered": answered,
            "correct": correct,
            "score": score,
            "full_score": full_score,
            "earned_score": earned_score,
            "duration_seconds": duration_seconds,
            "items": items,
        }

    def _auto_or_ai_judge(self, q: models.Question, user_answer: str) -> JudgeResult:
        """客观题规则判分 + 主观题 AI 判分。"""
        qtype = q.type  # 1=单选 2=多选 3=判断 4=填空 5=简答
        ref = self._normalize_choice((q.answer or "").strip().lower(), q.options)
        ua = self._normalize_choice((user_answer or "").strip().lower(), q.options)
        wrong_msg = f"规则判分：参考答案 {ref or q.answer}"

        if qtype in (1, 3):  # 单选/判断 — 去掉空白后严格匹配
            correct = self._same_text(ua, ref)
            return JudgeResult(
                score=100.0 if correct else 0.0,
                is_correct=correct,
                feedback="规则判分：答案正确" if correct else wrong_msg,
            )
        if qtype == 4:  # 填空 — 子串/近似匹配
            correct = ua in ref or ref in ua or (ua and abs(len(ua) - len(ref)) <= 2)
            return JudgeResult(
                score=100.0 if correct else 0.0,
                is_correct=correct,
                feedback="规则判分：答案正确" if correct else wrong_msg,
            )
        if qtype == 2:  # 多选 — 集合等价
            ref_set = set(filter(None, re.split(r"[,\s，、/]+", ref)))
            ua_set = set(filter(None, re.split(r"[,\s，、/]+", ua)))
            correct = ref_set == ua_set and bool(ref_set)
            return JudgeResult(
                score=100.0 if correct else 0.0,
                is_correct=correct,
                feedback="规则判分：答案正确" if correct else wrong_msg,
            )
        # 简答（type=5）— 走 AI
        return self.provider.judge_answer(q.content, q.answer, user_answer)

    @staticmethod
    def _same_text(a: str, b: str) -> bool:
        """忽略空白差异比较两段答案文本。"""
        return bool(a) and bool(b) and re.sub(r"\s+", "", a) == re.sub(r"\s+", "", b)

    @staticmethod
    def _normalize_choice(answer: str, options: list | None) -> str:
        """答案若写成 A/B/C/D，转成选项文本，兼容字母与文本两种录入习惯。

        注意：只有「整串都是选项字母」时才按字母映射；用户直接提交的选项原文
        不能按分隔符拆开，否则会丢掉原文里的逗号/空格，导致判分误判为错误。
        """
        if not answer or not options:
            return answer
        letters = "abcd"
        parts = [p for p in re.split(r"[,\s，、/]+", answer) if p]
        if not parts or not all(
            len(p) == 1 and letters.find(p) >= 0 and letters.find(p) < len(options)
            for p in parts
        ):
            return answer
        return " ".join(str(options[letters.find(p)]).strip().lower() for p in parts)
