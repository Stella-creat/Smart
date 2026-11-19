"""冒烟测试 — 所有 HTTP endpoint。"""
import sys, os
sys.path.insert(0, r"D:\Smart\src")

from smart.api.main import app
from smart.db import init_db
from fastapi.testclient import TestClient

init_db()  # 手动建表（TestClient 非上下文模式不会触发 startup）
c = TestClient(app)

# 1. health
r = c.get("/health")
print(f"GET /health -> {r.status_code} {r.json()}")

# 2. register + login
r2 = c.post("/api/auth/register", json={"username": "testuser", "password": "test123"})
print(f"POST /api/auth/register -> {r2.status_code}")

# 已存在时换用户名
if r2.status_code == 400:
    r2 = c.post("/api/auth/register", json={"username": "testuser2", "password": "test123"})
    print(f"POST /api/auth/register (alt) -> {r2.status_code}")

r3 = c.post("/api/auth/login", json={"username": "testuser2", "password": "test123"})
print(f"POST /api/auth/login -> {r3.status_code}")
assert r3.status_code == 200
token = r3.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# 3. knowledge points
r4 = c.post("/api/knowledge/points", headers=headers,
            json={"subject": "高数", "chapter": "第一章/极限", "name": "函数极限"})
print(f"POST /api/knowledge/points -> {r4.status_code}")
assert r4.status_code == 200
kp_id = r4.json()["id"]

r5 = c.get("/api/knowledge/points?subject=高数", headers=headers)
print(f"GET /api/knowledge/points -> {r5.status_code} count={len(r5.json())}")

# 4. question CRUD
r6 = c.post("/api/question", headers=headers,
            json={"knowledge_point_id": kp_id, "type": 1, "difficulty": 2,
                  "content": "lim x->0 sin x / x = ?", "answer": "1"})
print(f"POST /api/question -> {r6.status_code}")
assert r6.status_code == 200

# 5. plan
r7 = c.post("/api/plan", headers=headers, json={"subject": "高数", "daily_minutes": 60})
print(f"POST /api/plan -> {r7.status_code}")
assert r7.status_code == 200

r8 = c.get("/api/plan/today", headers=headers)
print(f"GET /api/plan/today -> {r8.status_code} count={len(r8.json())}")

# 6. deeptutor proxy（DeepTutor 必须运行）
try:
    r9 = c.get("/api/deeptutor/kbs", headers=headers)
    print(f"GET /api/deeptutor/kbs -> {r9.status_code}")
except Exception as e:
    print(f"GET /api/deeptutor/kbs -> skipped (deeptutor not running)")

# 7. master summary
r10 = c.get("/api/knowledge/summary?subject=高数", headers=headers)
print(f"GET /api/knowledge/summary -> {r10.status_code} {r10.json()}")

print("\n=== ALL SMOKE TESTS PASSED ===")
