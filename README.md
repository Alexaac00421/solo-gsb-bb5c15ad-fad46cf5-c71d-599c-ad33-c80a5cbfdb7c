# 双人见证资料封存服务

基于 Python + FastAPI + SQLite 的资料封存/开封服务：封存人提交资料正文并指定**三名不同见证人**；开封需封存人提交带**理由与截止时刻**的开封请求，在截止时刻前由**两名不同指定见证人**分别同意（同意绑定本次请求与原摘要）方可开封。

## 快速启动

### Docker（一键启动）

```bash
docker compose up --build
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 地址与端口

| 项目 | 值 |
|---|---|
| 服务地址 | `http://localhost:8000` |
| 交互式 API 文档 | `http://localhost:8000/docs` |
| 健康检查 | `GET http://localhost:8000/health` |

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SEAL_DB_PATH` | 容器内 `/data/seal.db`，本地 `./data/seal.db` | SQLite 数据库文件路径，启动时自动建库建表 |

Compose 通过命名卷 `seal-data` 持久化 `/data`，重启/重建容器后状态一致。

## 规则说明

- 注册账号后凭请求头 `X-API-Key` 操作；密钥仅在注册响应中返回一次，服务端只存哈希。
- 封存时保存正文与 SHA-256 摘要，**封存后内容不可改**（无任何修改接口）。
- **开封前**查询与错误响应均不含正文；**开封后**查询返回正文、摘要、理由及见证记录。
- 同一文档同时仅允许一个待处理开封请求；截止时刻必须晚于当前时间。
- 同一见证人对同一请求重试只计一次（幂等返回）；同意须与原摘要一致。
- 撤销或到期后，旧同意绑定在原请求上，**不带入后续请求**。
- 失效凭据(401)、非见证人(403)、无效时限(400)、重复开封(409)一律拒绝且状态不变。
- 并发的同意与撤销由全局锁 + SQLite `BEGIN IMMEDIATE` 事务串行化，**只产生一个已提交结果**。
- 全部操作按顺序写入 `operation_log` 表，随业务事务一起提交，重启后一致。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts` | 注册账号，返回 `api_key`（仅一次） |
| GET | `/accounts` | 列出用户名（用于指定见证人） |
| POST | `/documents` | 封存资料（正文 + 三名见证人） |
| GET | `/documents/{id}` | 查询（开封前无正文，开封后含正文与见证记录） |
| POST | `/documents/{id}/unseal-requests` | 提交开封请求（理由 + 截止时刻） |
| GET | `/unseal-requests/{id}` | 查询请求状态与已绑定同意 |
| POST | `/unseal-requests/{id}/approvals` | 见证人同意（提交原摘要） |
| POST | `/unseal-requests/{id}/revoke` | 封存人撤销请求 |
| GET | `/documents/{id}/log` | 按顺序查询操作记录 |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1. 注册：1 名封存人 + 3 名见证人（保存返回的 api_key）
curl -s -X POST $BASE/accounts -H 'Content-Type: application/json' -d '{"username":"alice"}'
curl -s -X POST $BASE/accounts -H 'Content-Type: application/json' -d '{"username":"bob"}'
curl -s -X POST $BASE/accounts -H 'Content-Type: application/json' -d '{"username":"carol"}'
curl -s -X POST $BASE/accounts -H 'Content-Type: application/json' -d '{"username":"dave"}'

# 2. 封存资料，指定三名不同见证人（返回 id 与 digest）
curl -s -X POST $BASE/documents -H "X-API-Key: $ALICE_KEY" -H 'Content-Type: application/json' -d '{
  "title": "遗嘱正本",
  "body": "本人名下全部资产由长女继承……",
  "witnesses": ["bob", "carol", "dave"]
}'

# 3. 开封前查询：响应不含正文
curl -s $BASE/documents/1 -H "X-API-Key: $BOB_KEY"

# 4. 封存人提交开封请求（理由 + 截止时刻，ISO 8601）
curl -s -X POST $BASE/documents/1/unseal-requests -H "X-API-Key: $ALICE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"立嘱人去世，需执行遗嘱","deadline":"2030-01-01T00:00:00Z"}'

# 5. 两名不同见证人分别同意（digest 须为封存时返回的摘要）
curl -s -X POST $BASE/unseal-requests/1/approvals -H "X-API-Key: $BOB_KEY" \
  -H 'Content-Type: application/json' -d "{\"digest\":\"$DIGEST\"}"
curl -s -X POST $BASE/unseal-requests/1/approvals -H "X-API-Key: $CAROL_KEY" \
  -H 'Content-Type: application/json' -d "{\"digest\":\"$DIGEST\"}"
# 第二人同意后 document_status 变为 opened

# 6. 开封后查询：返回正文、摘要、理由及见证记录
curl -s $BASE/documents/1 -H "X-API-Key: $DAVE_KEY"

# 7. 操作记录（按顺序持久保存）
curl -s $BASE/documents/1/log -H "X-API-Key: $ALICE_KEY"

# 撤销（仅待处理请求可撤销，撤销后旧同意失效）
curl -s -X POST $BASE/unseal-requests/2/revoke -H "X-API-Key: $ALICE_KEY"
```

也可以直接运行端到端示例脚本（仅依赖标准库）：

```bash
python3 examples/demo.py http://localhost:8000
```

## 测试

```bash
pip install httpx   # 测试客户端依赖
PYTHONPATH=. python3 tests/test_flow.py          # 43 项功能验收
PYTHONPATH=. python3 tests/test_concurrency.py   # 并发同意/撤销竞争（20 轮）
PYTHONPATH=. python3 tests/test_restart.py       # 杀进程重启后状态一致
```

## 项目结构

```
app/
  main.py        # FastAPI 应用与全部端点
  db.py          # SQLite 连接、事务、schema、操作日志
examples/
  demo.py        # 端到端调用示例（标准库实现）
tests/
  test_flow.py         # 全流程验收测试
  test_concurrency.py  # 并发竞争测试
  test_restart.py      # 重启持久化测试
Dockerfile
docker-compose.yml
requirements.txt
```
