"""SQLAlchemy ORM → 纯 dict 工具（过滤掉 _sa_instance_state 等内部字段）。"""
from __future__ import annotations


def to_dict(obj) -> dict:
    """把 SQLAlchemy ORM 对象转成可 JSON 序列化的 dict。

    只保留 __table__.columns 里的列，过滤掉 _sa_instance_state 等内部属性。
    datetime 转 ISO 字符串，JSON 列原样保留。
    """
    if obj is None:
        return {}
    result = {}
    for col in obj.__table__.columns:
        key = col.name
        val = getattr(obj, key)
        if val is None:
            result[key] = None
        elif hasattr(val, "isoformat") and not isinstance(val, str):
            result[key] = val.isoformat()
        else:
            result[key] = val
    return result


def to_dict_list(objs) -> list[dict]:
    return [to_dict(o) for o in objs]
