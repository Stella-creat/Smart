# Smart — AI 驱动的个性化学习系统（给 Agent 的操作手册）

> 这份文件记录了在 Trae 新对话中接手 Smart 项目需要知道的**一切关键约定**，避免"失忆"后从零摸索。
> 更新时间：2026-11-12

---

## 一、基本信息

| 项 | 值 |
| --- | --- |
| 项目代号 | **Smart** |
| 根目录 | `D:\Smart` |
| 语言/框架 | Python 3.12 + FastAPI + SQLAlchemy（后端） / 原生 HTML + CSS + JS（前端，单文件） |
| AI 引擎 | DeepTutor（另起进程，WS 协议） |
| OS | Windows 10/11，PowerShell 5 |
| 用户测试账号 | `demo` / `demo123` |
| 会话对话 | 中文，直接了当 |

## 二、端口与启动

```
Smart 后端 API    : http://localhost:8004    (FastAPI / uvicorn)
DeepTutor 引擎    : ws://localhost:8001/ws   (另起进程，Smart 通过 WS 调用)
前端单页          : http://localhost:8004/   (后端直接 mount StaticFiles)
健康检查          : GET /health → {"status":"ok", ...}
```

### 启动命令（必须从 D:\Smart 目录）

```powershell
# Smart 后端
Set-Location 'D:\Smart'
$env:PYTHONPATH='D:\Smart\src'
& 'C:\Users\syf09\AppData\Local\Programs\Python\Python312\python.exe' `
  -m uvicorn smart.api.main:app --host 0.0.0.0 --port 8004 2>&1
```

> ⚠️ **必须 cwd 在 D:\Smart**（SQLite 相对路径依赖 cwd）。db_url 已改绝对路径 `sqlite:///D:/Smart/smart_dev.db`，但 init_db() 里其他 SQL 操作可能仍受影响。

### DeepTutor 引擎
单独进程，端口 8001，由用户手动启动。Smart 不会自动拉起它。
WS 协议必须带 `"type":"start_turn","protocol_version":"2.0"`。

## 三、目录结构（只列关键）

```
D:\Smart\
├── src\smart\
│   ├── api\
│   │   ├── main.py                    # FastAPI app + 路由注册
│   │   └── routers\
│   │       ├── auth.py                # 登录/注册
│   │       ├── plan.py                # 学习计划 + 每日任务
│   │       ├── practice.py            # 练习组卷/提交/结算
│   │       ├── visualize.py           # 🆕 动画讲解（带后端进程内缓存 3h）
│   │       ├── knowledge.py           # 知识点 CRUD
│   │       ├── question.py            # 题库 + AI 出题
│   │       └── feedback.py / mapping.py / deeptutor_proxy.py
│   ├── adapter\provider\
│   │   ├── ai_provider.py             # AIProvider Protocol 接口定义
│   │   └── deep_tutor_provider.py     # DeepTutor WS 实现（含 visualize_kp）
│   ├── plan\service.py                # 排期算法（LEARN/REVIEW 分配）
│   ├── practice\service.py            # 练习组卷/批改
│   ├── question\service.py            # 题库 + _IMPORTANCE_CACHE
│   ├── config.py                      # 所有配置（db_url, daily_rollover_enabled 等）
│   └── db.py                          # engine + SessionLocal + PRAGMA
├── web\index.html                     # 🆕 单文件前端（约 2200 行）
├── smart_dev.db                       # SQLite 开发库（绝对路径 D:/Smart/smart_dev.db）
└── pyproject.toml / requirements*.txt
```

## 四、后端已踩的坑（读 config.py / db.py 看注释）

### 🔴 必须记住
1. **`daily_rollover_enabled: False`** —— SQLAlchemy + SQLite 多线程（FastAPI handler vs daily_loop 协程）会锁竞争，daily_loop 已关掉
2. **SQLite PRAGMA** —— 每个 session 打开后立即 `journal_mode=DELETE` + `busy_timeout=30000`，不要用 WAL 模式
3. **db.py 不要用 `pool_pre_ping`** —— SQLite 不需要连接池 pre-ping，反而会搞坏连接
4. **DeepTutor visualize capability 不返回 JSON** —— 它就返回 markdown ```svg 代码块。Smart 的 visualize_kp 用 `re.search(r"<svg[\s\S]*?</svg>")` 提取，prompt 直接要纯 SVG 不要 JSON 包装
5. **`_sync_ws_call` 异常时返回空字符串 `""`** —— AIProvider 层所有 WS 调用失败都会静默返回空串，需要上层自己判断并走 fallback
6. **practice 路由里 UPDATE question_ids 报 `OperationalError: unable to open database file`** —— 根因是 daily_loop 污染连接池，关掉 daily_loop + 绝对路径 DB 后已修复

### 🟡 注意事项
- `question/service.py` 的 `_IMPORTANCE_CACHE` 是进程内 dict，重启丢失
- 前端知识点重要度评分：1-5 星，占位知识点（name == subject）硬给 1
- `plan/service.py` 排期边界：Day 1 无 REVIEW，最后一天无 LEARN，REVIEW 优先排（避免 LEARN 占满时间）
- SQL 写操作要 try/except + rollback，不要让坏掉的 session 留在连接池

## 五、前端已踩的坑

1. **index.html 渲染时序** —— `renderStudyLearnPage` 每次 `innerHTML` 覆盖，**绝对不能用 `addEventListener` 绑 handler**！统一用 `document.addEventListener('click', ...)` 全局委托
2. **概览页 vs 学习页渲染路径不同** —— 概览页用 `$('day-detail-body').innerHTML = kpGroups.map(...)`，学习页用 `$('kps-body').innerHTML = ...`
3. **浏览器缓存** —— main.py 里 index.html 响应头 `Cache-Control: no-store`，硬刷 Ctrl+Shift+R 绕过缓存
4. **deep_tutor_provider.py 被删过** —— 如果 Glob 找不到，Grep 一下实际文件名；系统消息里"文件被删除"可能是误报 pyc

## 六、API 速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 返回 JWT |
| GET | `/api/plan/plans/{plan_id}/days` | 返回每日任务（含 `importance: 1-5` + `importance_reason`） |
| GET | `/api/practice/tasks/{task_id}` | 返回试卷（含断点续练） |
| GET | `/api/visualize/kp/{kp_id}` | 🆕 返回讲解 + SVG 动画，**带 `cached: true/false` 标识**（后端 3h 缓存） |
| DELETE | `/api/visualize/cache` | 清所有动画缓存 |
| DELETE | `/api/visualize/kp/{kp_id}/cache` | 清单个知识点缓存 |

## 七、动画讲解功能架构（新对话接手直接看这里）

```
前端概览页/学习页
  ── 🤖 紫色按钮 ──▶ openAnimModal(kpId)
                           │
              ① 查 localStorage（24h TTL）
                  hit → 直接 innerHTML 渲染，弹窗秒开
                  miss ↓
              ② fetch /api/visualize/kp/{kp_id}
                           │
                    后端查 _CACHE（3h TTL）
                      hit → 返回 {"cached": true, ...} 20ms
                      miss ↓
                    DeepTutor WS:
                      capability="visualize" → 要纯 SVG/HTML
                      capability="chat" + tools=[] → 要讲解文字
                    （如果 visualize 返回 markdown ```svg 代码块
                     用 re 清理包裹 + 提取 <svg>...</svg>）
                           │
              前端收到响应 → 写 localStorage → 渲染弹窗
              弹窗标题栏显示：⚡ localStorage 秒开 / （后端缓存）
```

## 八、前端 localStorage 缓存键

```
anim_cache_{kp_id}  →  JSON: {ts: Date.now(), data: {kp_name, subject, render_type, render_content, explanation}}
TTL: 24 小时（ANIM_CACHE_TTL）
```

手动测试缓存：
```js
// 清前端缓存
for (let i=0; i<localStorage.length; i++) { const k=localStorage.key(i); if(k.startsWith('anim_cache_')) localStorage.removeItem(k); }
```

## 九、调试技巧

```powershell
# 测试后端 API
$env:PYTHONIOENCODING='utf-8'
$login = python -c "import json,urllib.request; r=urllib.request.Request('http://127.0.0.1:8004/api/auth/login', data=json.dumps({'username':'demo','password':'demo123'}).encode(), headers={'Content-Type':'application/json'}); print(json.loads(urllib.request.urlopen(r).read())['access_token'])"
$tok = $login
curl.exe -s -H "Authorization: Bearer $tok" http://127.0.0.1:8004/api/visualize/kp/36

# 测试 DeepTutor WS 连通性（直接测协议）
python -c "
import asyncio, websockets, json, uuid
async def t():
    async with websockets.connect('ws://localhost:8001/ws', open_timeout=10) as ws:
        await ws.send(json.dumps({'type':'start_turn','protocol_version':'2.0','content':'你好','capability':'chat','session_id':'test','config':{},'tools':[]}))
        while True:
            m = json.loads(await ws.recv())
            if m.get('type')=='result': print(m.get('metadata',{}).get('response','')[:200]); break
asyncio.run(t())
"
```

## 十、常见问题 → 快速诊断

| 症状 | 可能原因 | 快速验证 |
| --- | --- | --- |
| 500 `unable to open database file` | daily_loop 污染 SQLite 连接池 | `Get-NetTCPConnection -LocalPort 8004` 看有没有 python 进程；Config 里 daily_rollover_enabled 确认 False |
| 前端按钮不显示 | 浏览器缓存旧 JS | Ctrl+Shift+R 硬刷；main.py 已有 Cache-Control: no-store |
| 动画弹窗一直 spinner | ① 后端没起来 ② DeepTutor WS 连不上 ③ fallback SVG（812 字符） | curl `/health`；看响应 render_content 长度 > 1000 才是真 AI 动画 |
| practice 500 | 旧连接污染连接池 | 重启后端（会重置 SQLAlchemy 连接池） |
| 所有知识点堆第一天 | 旧排期算法 bug（已修：REVIEW 优先排 + 边界约束） | 看 plan/service.py generate_day_tasks |

---

## 最后提醒

**DeepTutorProvider._sync_ws_call() 的 `except: return ""` 是静默吞异常！** 任何 WS 失败、异常、超时都会返回空字符串，上层业务代码**必须**自己判断 `if not resp: ...` 并走 fallback（`_fallback_svg`、硬给默认分等）。不要假设 `_sync_ws_call` 会抛异常给上层。
