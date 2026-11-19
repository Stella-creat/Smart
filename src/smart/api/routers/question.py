"""Question 路由 — 题库 CRUD + 三种创建路径。"""
from __future__ import annotations

import random

from pydantic import BaseModel
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart import models
from smart.db_utils import to_dict, to_dict_list
from smart.question.service import QuestionService
from smart.knowledge.service import KnowledgeService
from smart.adapter.container import get_ai_provider

router = APIRouter()


class QuestionCreate(BaseModel):
    kb_name: str | None = None
    knowledge_point_id: int | None = None
    bank_id: int | None = None
    type: int
    difficulty: int = 2
    content: str
    answer: str
    options: list | None = None
    explanation: str | None = None


class BulkImportReq(BaseModel):
    kb_name: str | None = None
    knowledge_point_id: int | None = None
    bank_id: int | None = None
    raw_text: str
    type: int = 1
    difficulty: int = 2


class AIGenerateReq(BaseModel):
    kb_name: str | None = None
    knowledge_point_id: int | None = None
    bank_id: int | None = None
    types: list[int] = [1]
    difficulty: int = 2
    count: int = 3


class BankCreate(BaseModel):
    name: str
    kb_name: str


class AIPaperReq(BaseModel):
    """按知识点出卷：kps 为空时默认取该知识库全部知识点。"""
    kps: list[str] = []
    types: list[int] = [1]
    difficulty: int = 2
    count: int = 2
    weighted: bool = True


def _get_bank(
    db: Session, user_id: int, bank_id: int | None
) -> models.QuestionBank | None:
    """取题库并校验归属；bank_id 为空时返回 None。"""
    if bank_id is None:
        return None
    bank = db.get(models.QuestionBank, bank_id)
    if bank is None or bank.user_id != user_id:
        raise HTTPException(status_code=404, detail="题库不存在")
    return bank


def _resolve_kp_id(
    db: Session, user_id: int, kb_name: str | None, kp_id: int | None
) -> int:
    """题库选「知识库」时自动映射到该库的占位知识点；兼容旧的知识点直传。"""
    if kb_name and kb_name.strip():
        return KnowledgeService(get_ai_provider()).ensure_kp_for_kb(
            db, user_id, kb_name.strip()
        ).id
    if kp_id:
        return kp_id
    raise HTTPException(status_code=400, detail="请先选择知识库")


class JudgeReq(BaseModel):
    """纯 AI 判分（不传 question_id，直接给题干/参考答案/用户答案）。"""
    question_content: str
    reference_answer: str
    user_answer: str


class SubmitReq(BaseModel):
    """完整答题提交（用 question_id 从 DB 拿题，自动判断规则/AI 判分）。"""
    question_id: int
    user_answer: str
    task_id: int | None = None
    duration_seconds: int | None = None


class PracticeAnswer(BaseModel):
    """练习答题的一项作答。"""
    question_id: int
    user_answer: str = ""


class PracticePoints(BaseModel):
    """整卷每题分值（按题型分组）。"""
    choice: float = 2
    judge: float = 2
    subjective: float = 5


class PracticeSubmitReq(BaseModel):
    """整卷练习交卷（限时答题用）。"""
    answers: list[PracticeAnswer] = []
    duration_seconds: int | None = None
    points: PracticePoints | None = None


@router.get("/banks")
def list_banks(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """题库列表 —— 首次调用会按科目回填历史无归属题目。"""
    svc = QuestionService(get_ai_provider())
    banks = svc.list_banks(db, user.id)
    counts = dict(
        db.execute(
            select(models.Question.bank_id, func.count(models.Question.id))
            .where(
                models.Question.bank_id.is_not(None),
                (models.Question.user_id == user.id) | (models.Question.user_id == 0),
            )
            .group_by(models.Question.bank_id)
        ).all()
    )
    return [
        {
            "id": b.id,
            "name": b.name,
            "kb_name": b.kb_name,
            "question_count": int(counts.get(b.id, 0)),
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in banks
    ]


@router.post("/banks")
def create_bank(
    req: BankCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """新建题库（同一用户下题库名不可重复）。"""
    svc = QuestionService(get_ai_provider())
    try:
        bank = svc.create_bank(db, user.id, req.name, req.kb_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "id": bank.id,
        "name": bank.name,
        "kb_name": bank.kb_name,
        "question_count": 0,
        "created_at": bank.created_at.isoformat() if bank.created_at else None,
    }


@router.delete("/banks/{bank_id}")
def delete_bank(
    bank_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """删除题库及其下所有题目。"""
    svc = QuestionService(get_ai_provider())
    if not svc.delete_bank(db, user.id, bank_id):
        raise HTTPException(status_code=404, detail="题库不存在")
    return {"deleted": True}


@router.post("/banks/{bank_id}/upload")
async def upload_bank_document(
    bank_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """上传题库文档（txt/md/docx/pdf）→ 解析文本 → 容错切题入库。"""
    bank = _get_bank(db, user.id, bank_id)
    svc = QuestionService(get_ai_provider())
    data = await file.read()
    try:
        text = svc.extract_text(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    kp_id = _resolve_kp_id(db, user.id, bank.kb_name, None)
    imported, skipped = svc.import_from_text(
        db, user.id, kp_id, text, bank_id=bank.id,
    )
    total = db.execute(
        select(func.count(models.Question.id)).where(models.Question.bank_id == bank.id)
    ).scalar() or 0
    return {
        "filename": file.filename,
        "imported": imported,
        "skipped": skipped,
        "question_count": int(total),
    }


@router.post("/banks/{bank_id}/ai-paper")
def ai_generate_paper(
    bank_id: int,
    req: AIPaperReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """按知识点 AI 出题（每个知识点单独检索上下文后出题）。"""
    bank = _get_bank(db, user.id, bank_id)
    provider = get_ai_provider()
    ks = KnowledgeService(provider)
    points = ks.list_knowledge_points(db, user.id, bank.kb_name)
    # 排除「知识库」占位知识点（chapter=知识库 且 name=知识库名）
    real_names = [
        p.name for p in points
        if not (p.chapter == "知识库" and p.name == bank.kb_name)
    ]
    names = [n.strip() for n in req.kps if n and n.strip()]
    if not names:
        names = real_names or ([bank.kb_name] if bank.kb_name else [])
    types = [t for t in req.types if t in (1, 2, 3, 4, 5)] or [1]
    count = min(max(int(req.count or 2), 1), 5)
    svc = QuestionService(provider)
    saved, per_kp = svc.ai_generate_for_kps(
        db, user.id, bank.id, bank.kb_name, names, types, req.difficulty, count,
        weighted=req.weighted,
    )
    plan = [
        {
            "kp": name,
            "count": info.get("count", 0),
            "planned": info.get("planned", 0),
            "importance": info.get("importance", 3),
            "reason": info.get("reason", ""),
        }
        for name, info in per_kp.items()
    ]
    plan.sort(key=lambda x: (-x["importance"], x["kp"]))
    return {
        "saved": len(saved),
        "weighted": req.weighted,
        "plan": plan,
        "questions": to_dict_list(saved),
    }


@router.get("/banks/{bank_id}/practice")
def get_practice_questions(
    bank_id: int,
    order: str = "seq",
    limit: int = 0,
    single: int = 0,
    judge: int = 0,
    subjective: int = 0,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """取该题库的练习题集。

    single/judge/subjective 分别限制选择题(1/2)、判断题(3)、主观题(4/5) 的
    抽题数量，0（或不传）表示该类全部取；order=seq 保持题库原顺序（按 id），
    order=random 服务端打乱；limit=0 表示不再额外截断。
    """
    _get_bank(db, user.id, bank_id)
    svc = QuestionService(get_ai_provider())
    questions = sorted(
        svc.list_questions(db, user.id, bank_id=bank_id),
        key=lambda q: q.id or 0,
    )
    shuffled = order == "random"

    def pick(types: tuple[int, ...], count: int) -> list[models.Question]:
        """按题型分组取题：count<=0 取全部，否则取前 count 道（random 时随机）。"""
        pool = [q for q in questions if q.type in types]
        if shuffled:
            random.shuffle(pool)
        return pool[:count] if count and count > 0 else pool

    # 三类都为 0 时等价于整库取题，保持原有默认行为
    if single > 0 or judge > 0 or subjective > 0:
        questions = (
            pick((1, 2), single)
            + pick((3,), judge)
            + pick((4, 5), subjective)
        )
        if shuffled:
            random.shuffle(questions)
        else:
            questions.sort(key=lambda q: q.id or 0)
    elif shuffled:
        random.shuffle(questions)
    if limit and limit > 0:
        questions = questions[:limit]
    return to_dict_list(questions)


@router.post("/banks/{bank_id}/practice/submit")
def submit_practice(
    bank_id: int,
    req: PracticeSubmitReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """整卷练习交卷：逐题判分并返回总分与逐题结果。"""
    _get_bank(db, user.id, bank_id)
    svc = QuestionService(get_ai_provider())
    answers = [
        {"question_id": a.question_id, "user_answer": a.user_answer}
        for a in req.answers
    ]
    return svc.practice_batch(
        db, user.id, bank_id, answers,
        duration_seconds=req.duration_seconds,
        points=req.points.model_dump() if req.points else None,
    )


@router.get("")
def list_questions(
    kp_id: int | None = None,
    qtype: int | None = None,
    difficulty: int | None = None,
    kb_name: str | None = None,
    bank_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    svc = QuestionService(get_ai_provider())
    qs = svc.list_questions(
        db, user.id, kp_id, qtype, difficulty, kb_name, bank_id=bank_id
    )
    return to_dict_list(qs)


@router.post("")
def create_question(
    req: QuestionCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = QuestionService(get_ai_provider())
    bank = _get_bank(db, user.id, req.bank_id)
    kb_name = req.kb_name or (bank.kb_name if bank else None)
    kp_id = _resolve_kp_id(db, user.id, kb_name, req.knowledge_point_id)
    q = svc.create_question(
        db, user_id=user.id,
        kp_id=kp_id, qtype=req.type, difficulty=req.difficulty,
        content=req.content, answer=req.answer, options=req.options,
        explanation=req.explanation, source="MANUAL", bank_id=req.bank_id,
    )
    return to_dict(q)


@router.post("/import")
def bulk_import(
    req: BulkImportReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = QuestionService(get_ai_provider())
    bank = _get_bank(db, user.id, req.bank_id)
    kb_name = req.kb_name or (bank.kb_name if bank else None)
    kp_id = _resolve_kp_id(db, user.id, kb_name, req.knowledge_point_id)
    count = svc.bulk_import(
        db, user.id, kp_id, req.raw_text, req.type, req.difficulty,
        bank_id=req.bank_id,
    )
    return {"imported": count}


@router.post("/ai-generate")
def ai_generate(
    req: AIGenerateReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """AI 出题 —— 用 DeepTutor 知识库 chunk 做上下文。"""
    provider = get_ai_provider()
    ks = KnowledgeService(provider)
    bank = _get_bank(db, user.id, req.bank_id)
    kb_name = req.kb_name or (bank.kb_name if bank else None)
    kp_id = _resolve_kp_id(db, user.id, kb_name, req.knowledge_point_id)
    kp = db.get(models.KnowledgePoint, kp_id)
    # 出题上下文：优先用所选知识库（或题库关联知识库）名，兼容旧知识点路径
    kb = (kb_name or "").strip() or (kp.subject if kp else "")
    query = kb if kb_name else (kp.name if kp else "")
    chunks = ks.pull_chunks_for_matching(kb, query, top_k=10)
    svc = QuestionService(provider)
    saved = svc.ai_generate(
        db, user.id, kp_id, chunks,
        req.types, req.difficulty, req.count, bank_id=req.bank_id,
    )
    return {"saved": len(saved), "questions": to_dict_list(saved)}


@router.post("/judge")
def judge_answer(
    req: JudgeReq,
    user: models.User = Depends(get_current_user),
) -> dict:
    """纯 AI 判分 —— 给定题干/参考答案/用户答案，走 DeepTutor → DeepSeek。"""
    provider = get_ai_provider()
    svc = QuestionService(provider)
    result = svc.judge_with_fields(req.question_content, req.reference_answer, req.user_answer)
    return {
        "score": result.score,
        "is_correct": result.is_correct,
        "feedback": result.feedback,
    }


@router.post("/submit")
def submit_answer(
    req: SubmitReq,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """完整答题提交 —— 自动规则/AI 判分 + 写 AttemptRecord。"""
    svc = QuestionService(get_ai_provider())
    judge_result, record = svc.submit_and_judge(
        db, user.id, req.question_id, req.user_answer,
        task_id=req.task_id, duration_seconds=req.duration_seconds,
    )
    return {
        "score": judge_result.score,
        "is_correct": judge_result.is_correct,
        "feedback": judge_result.feedback,
        "attempt_record": to_dict(record) if record else None,
    }
