"""Knowledge 路由 — 知识点树 CRUD + DeepTutor 同步。"""
from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from smart.db import get_db
from smart.api.deps import get_current_user
from smart import models
from smart.db_utils import to_dict, to_dict_list
from smart.knowledge.service import KnowledgeService
from smart.adapter.container import get_ai_provider

router = APIRouter()


class KBCreate(BaseModel):
    name: str


class KBSearch(BaseModel):
    query: str
    top_k: int = 5


class WebSearchRequest(BaseModel):
    query: str


class WebNoteSave(BaseModel):
    query: str = ""
    content: str
    title: str | None = None


class KPCreate(BaseModel):
    subject: str
    chapter: str
    name: str
    parent_id: int | None = None


@router.get("/points")
def list_kps(
    subject: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    svc = KnowledgeService(get_ai_provider())
    kps = svc.list_knowledge_points(db, user.id, subject)
    return to_dict_list(kps)


@router.get("/points/tree")
def kp_tree(
    subject: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """按「知识库 → 章节 → 知识点」返回三层结构，供知识图谱渲染。"""
    svc = KnowledgeService(get_ai_provider())
    kps = svc.list_knowledge_points(db, user.id, subject)
    # 占位知识点（名称即知识库名，章节为「知识库」或与库同名）不是真实知识点，不进图谱
    real = [
        k for k in kps
        if not (k.name == subject and k.chapter in (subject, "知识库"))
    ]

    chapters: dict[str, list[dict]] = {}
    for kp in real:
        chapters.setdefault(kp.chapter or "未分类", []).append({
            "id": kp.id,
            "name": kp.name,
            "knowledge_type": kp.knowledge_type,
            "mastery": round(float(kp.mastery or 0), 1),
        })
    return {
        "subject": subject,
        "total": len(real),
        "chapters": [{"chapter": name, "points": pts} for name, pts in chapters.items()],
    }


@router.post("/points")
def create_kp(
    req: KPCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = KnowledgeService(get_ai_provider())
    kp = svc.create_knowledge_point(db, user.id, req.subject, req.chapter, req.name, req.parent_id)
    return to_dict(kp)


@router.get("/deeptutor/kbs")
def list_deeptutor_kbs(
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    svc = KnowledgeService(get_ai_provider())
    return svc.sync_knowledge_bases()


@router.get("/deeptutor/kbs/{kb_name}/docs")
def list_deeptutor_docs(
    kb_name: str,
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    svc = KnowledgeService(get_ai_provider())
    return svc.sync_documents(kb_name)


@router.get("/deeptutor/kbs/{kb_name}/docs/{filename:path}/text")
def get_doc_text(
    kb_name: str,
    filename: str,
    user: models.User = Depends(get_current_user),
) -> dict:
    """获取文档提取的纯文本 + 原始文件 URL（PDF 可直接内嵌预览）。"""
    provider = get_ai_provider()
    text = provider.get_document_text(kb_name, filename)
    raw_url = provider.get_document_raw_url(kb_name, filename)
    return {"text": text, "raw_url": raw_url}


@router.delete("/deeptutor/kbs/{kb_name}/docs/{filename:path}")
def delete_doc(
    kb_name: str,
    filename: str,
    user: models.User = Depends(get_current_user),
) -> dict:
    """删除知识库中的文档（连同其索引一起移除）。"""
    provider = get_ai_provider()
    if not provider.delete_document(kb_name, filename):
        raise HTTPException(status_code=404, detail="文档不存在或已被删除")
    return {"deleted": True, "filename": filename}


@router.get("/deeptutor/kbs/{kb_name}/dependents")
def kb_dependents(
    kb_name: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """查询知识库关联的学习计划与题库，供删除前的选项弹窗使用。"""
    svc = KnowledgeService(get_ai_provider())
    return svc.kb_dependents(db, user.id, kb_name)


@router.delete("/deeptutor/kbs/{kb_name}")
def delete_kb(
    kb_name: str,
    delete_plan: bool = False,
    delete_bank: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """删除整个知识库（含其文档与索引），可选一并删除关联计划与题库。"""
    svc = KnowledgeService(get_ai_provider())
    try:
        return svc.delete_knowledge_base(
            db, user.id, kb_name,
            delete_plan=delete_plan, delete_bank=delete_bank,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/deeptutor/kbs")
def create_deeptutor_kb(
    req: KBCreate,
    user: models.User = Depends(get_current_user),
) -> dict:
    """在 DeepTutor 引擎里新建空知识库。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    svc = KnowledgeService(get_ai_provider())
    try:
        return svc.create_knowledge_base(name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DeepTutor 建库失败：{exc}") from exc


@router.post("/deeptutor/kbs/{kb_name}/upload")
def upload_deeptutor_docs(
    kb_name: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """上传文档到知识库，解析与索引由 DeepTutor 后台异步完成。"""
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")
    payload = [(f.filename or "unnamed", f.file.read()) for f in files]
    svc = KnowledgeService(get_ai_provider())
    try:
        result = svc.upload_documents(kb_name, payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"上传失败：{exc}") from exc
    svc.add_document_records(db, user.id, kb_name, [(n, len(d)) for n, d in payload])
    return {"uploaded": len(payload), "detail": result}


class KBLinkCreate(BaseModel):
    title: str = ""
    url: str


@router.get("/deeptutor/kbs/{kb_name}/resources")
def list_kb_resources(
    kb_name: str,
    kind: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """知识库附属资源列表（文档记录 / 链接）。"""
    rows = KnowledgeService(get_ai_provider()).list_resources(db, user.id, kb_name)
    if kind:
        rows = [r for r in rows if r.kind == kind]
    return to_dict_list(rows)


@router.post("/deeptutor/kbs/{kb_name}/links")
def add_kb_link(
    kb_name: str,
    req: KBLinkCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """为知识库添加网页链接。"""
    svc = KnowledgeService(get_ai_provider())
    try:
        row, indexed, error = svc.add_link(db, user.id, kb_name, req.title, req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**to_dict(row), "indexed": indexed, "index_error": error}


@router.delete("/deeptutor/kbs/{kb_name}/resources/{resource_id}")
def delete_kb_resource(
    kb_name: str,
    resource_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = KnowledgeService(get_ai_provider())
    if not svc.delete_resource(db, user.id, kb_name, resource_id):
        raise HTTPException(status_code=404, detail="资源不存在")
    return {"deleted": True}


@router.post("/deeptutor/kbs/{kb_name}/search")
def search_deeptutor_kb(
    kb_name: str,
    req: KBSearch,
    user: models.User = Depends(get_current_user),
) -> list[dict]:
    """知识库语义检索，返回命中的片段与来源。"""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="查询内容不能为空")
    svc = KnowledgeService(get_ai_provider())
    return svc.search(kb_name, req.query.strip(), top_k=req.top_k)


@router.post("/deeptutor/kbs/{kb_name}/analyze")
def analyze_kb_points(
    kb_name: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """AI 读取知识库内容，切分为知识点并落库（subject=知识库名）。"""
    svc = KnowledgeService(get_ai_provider())
    points, created = svc.extract_and_save_kps(db, user.id, kb_name)
    return {
        "created": created,
        "total": len(points),
        "points": to_dict_list(points),
    }


@router.post("/deeptutor/web-search")
def web_search(
    req: WebSearchRequest,
    user: models.User = Depends(get_current_user),
) -> dict:
    """联网搜索（DeepTutor web_search 工具）。"""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="查询内容不能为空")
    svc = KnowledgeService(get_ai_provider())
    return svc.web_search(req.query.strip())


@router.post("/deeptutor/kbs/{kb_name}/web-notes")
def save_web_note(
    kb_name: str,
    req: WebNoteSave,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """把联网搜索结果存入知识库（生成 Markdown 文档并建立索引）。"""
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="保存内容不能为空")
    svc = KnowledgeService(get_ai_provider())
    return svc.save_web_result(kb_name, req.query.strip(), req.content.strip(), req.title, db=db, user_id=user.id)


@router.get("/points/{kp_id}/explanation")
def get_kp_explanation(
    kp_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    """获取知识点的解析内容（从知识库 RAG 检索相关片段，首次查询后缓存）。"""
    kp = db.get(models.KnowledgePoint, kp_id)
    if not kp:
        raise HTTPException(status_code=404, detail="知识点不存在")
    svc = KnowledgeService(get_ai_provider())
    explanation = svc.get_kp_explanation(db, kp)
    return {"explanation": explanation}


@router.get("/summary")
def mastery_summary(
    subject: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict:
    svc = KnowledgeService(get_ai_provider())
    return svc.mastery_summary(db, user.id, subject)
