#!/usr/bin/env python3
"""全流程验收测试：覆盖任务书各项要求。运行前设置 SEAL_DB_PATH 指向临时文件。"""
import os
import sqlite3
import sys
import threading
import time

os.environ["SEAL_DB_PATH"] = sys.argv[1] if len(sys.argv) > 1 else "/tmp/seal_test.db"
DB = os.environ["SEAL_DB_PATH"]
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ok - {name}")


with TestClient(app) as c:
    # ---- 注册
    keys = {}
    for name in ["alice", "bob", "carol", "dave", "eve"]:
        r = c.post("/accounts", json={"username": name})
        assert r.status_code == 201, r.text
        keys[name] = r.json()["api_key"]
    check("注册账号并签发密钥", True)
    r = c.post("/accounts", json={"username": "alice"})
    check("重复用户名拒绝 409", r.status_code == 409)

    H = {k: {"X-API-Key": v} for k, v in keys.items()}

    # ---- 失效凭据
    r = c.get("/documents/1", headers={"X-API-Key": "nope"})
    check("失效凭据拒绝 401", r.status_code == 401)
    r = c.get("/documents/1")
    check("缺少凭据拒绝 401", r.status_code == 401)

    # ---- 封存参数校验
    r = c.post("/documents", json={"body": "x", "witnesses": ["bob", "bob", "carol"]}, headers=H["alice"])
    check("见证人重复拒绝 422", r.status_code == 422)
    r = c.post("/documents", json={"body": "x", "witnesses": ["bob", "carol"]}, headers=H["alice"])
    check("见证人不足三人拒绝 422", r.status_code == 422)
    r = c.post("/documents", json={"body": "x", "witnesses": ["bob", "carol", "ghost"]}, headers=H["alice"])
    check("见证人不存在拒绝 422", r.status_code == 422)

    # ---- 正常封存
    BODY = "机密正文：转让协议全文……"
    r = c.post("/documents", json={"title": "协议", "body": BODY,
                                   "witnesses": ["bob", "carol", "dave"]}, headers=H["alice"])
    assert r.status_code == 201, r.text
    doc = r.json()
    DOC, DIGEST = doc["id"], doc["digest"]
    check("封存返回摘要与状态", doc["status"] == "sealed" and len(DIGEST) == 64)

    # ---- 开封前查询不泄露正文（含错误响应）
    r = c.get(f"/documents/{DOC}", headers=H["eve"])
    check("开封前查询无正文字段", r.status_code == 200 and "body" not in r.json())
    r = c.get("/documents/99999", headers=H["alice"])
    check("404 错误响应不含正文", r.status_code == 404 and BODY not in r.text)
    r = c.post(f"/documents/{DOC}/unseal-requests", json={"reason": "r", "deadline": "bad"},
               headers=H["alice"])
    check("无效时限格式拒绝 400 且不含正文", r.status_code == 400 and BODY not in r.text)
    r = c.post(f"/documents/{DOC}/unseal-requests",
               json={"reason": "r", "deadline": "2000-01-01T00:00:00Z"}, headers=H["alice"])
    check("过去时限拒绝 400", r.status_code == 400)

    # ---- 非封存人提交开封请求
    r = c.post(f"/documents/{DOC}/unseal-requests",
               json={"reason": "r", "deadline": "2030-01-01T00:00:00Z"}, headers=H["bob"])
    check("非封存人提交开封请求拒绝 403", r.status_code == 403)

    # ---- 提交开封请求
    r = c.post(f"/documents/{DOC}/unseal-requests",
               json={"reason": "司法调取", "deadline": "2030-01-01T00:00:00Z"}, headers=H["alice"])
    assert r.status_code == 201, r.text
    REQ = r.json()["id"]
    check("提交开封请求成功", True)
    r = c.post(f"/documents/{DOC}/unseal-requests",
               json={"reason": "again", "deadline": "2030-01-01T00:00:00Z"}, headers=H["alice"])
    check("重复待处理请求拒绝 409", r.status_code == 409)

    # ---- 见证同意校验
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["eve"])
    check("非见证人同意拒绝 403", r.status_code == 403)
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": "0" * 64}, headers=H["bob"])
    check("摘要与封存摘要不符拒绝 400", r.status_code == 400)
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["alice"])
    check("封存人非见证人同意拒绝 403", r.status_code == 403)

    # 状态未变
    r = c.get(f"/unseal-requests/{REQ}", headers=H["alice"])
    check("拒绝后状态不变（0 同意）", r.json()["approvals"] == [])

    # ---- 一人同意，重试只计一次
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["bob"])
    check("第一人同意", r.status_code == 200 and r.json()["approval_count"] == 1)
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["bob"])
    j = r.json()
    check("同一人重试只计一次", r.status_code == 200 and j["already_approved"] and j["approval_count"] == 1)
    r = c.get(f"/documents/{DOC}", headers=H["alice"])
    check("一人同意仍未开封且无正文", r.json()["status"] == "sealed" and "body" not in r.json())

    # ---- 第二人同意 → 开封
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["carol"])
    j = r.json()
    check("第二人同意即开封", j["approval_count"] == 2 and j["document_status"] == "opened")

    # ---- 开封后查询返回正文、摘要、理由、见证记录
    r = c.get(f"/documents/{DOC}", headers=H["dave"])
    j = r.json()
    check("开封后返回正文", j["body"] == BODY)
    check("开封后返回摘要", j["digest"] == DIGEST)
    check("开封后返回理由", j["unseal"]["reason"] == "司法调取")
    check("开封后返回见证记录",
          [a["witness"] for a in j["unseal"]["approvals"]] == ["bob", "carol"]
          and all(a["digest"] == DIGEST for a in j["unseal"]["approvals"]))

    # ---- 重复开封拒绝
    r = c.post(f"/documents/{DOC}/unseal-requests",
               json={"reason": "x", "deadline": "2031-01-01T00:00:00Z"}, headers=H["alice"])
    check("重复开封拒绝 409", r.status_code == 409)
    r = c.post(f"/unseal-requests/{REQ}/approvals", json={"digest": DIGEST}, headers=H["dave"])
    check("已开封请求的同意拒绝 409", r.status_code == 409)
    r = c.post(f"/unseal-requests/{REQ}/revoke", headers=H["alice"])
    check("已开封请求撤销拒绝 409", r.status_code == 409)

    # ---- 撤销流程：旧同意不得带入后续请求
    r = c.post("/documents", json={"body": "第二份资料", "witnesses": ["bob", "carol", "dave"]},
               headers=H["alice"])
    DOC2, DIG2 = r.json()["id"], r.json()["digest"]
    r = c.post(f"/documents/{DOC2}/unseal-requests",
               json={"reason": "r1", "deadline": "2030-06-01T00:00:00Z"}, headers=H["alice"])
    REQ2 = r.json()["id"]
    c.post(f"/unseal-requests/{REQ2}/approvals", json={"digest": DIG2}, headers=H["bob"])
    r = c.post(f"/unseal-requests/{REQ2}/revoke", headers=H["alice"])
    check("封存人撤销成功", r.status_code == 200 and r.json()["status"] == "revoked")
    r = c.post(f"/unseal-requests/{REQ2}/approvals", json={"digest": DIG2}, headers=H["carol"])
    check("撤销后同意拒绝 409", r.status_code == 409)
    r = c.post(f"/documents/{DOC2}/unseal-requests",
               json={"reason": "r2", "deadline": "2030-06-01T00:00:00Z"}, headers=H["alice"])
    REQ3 = r.json()["id"]
    r = c.get(f"/unseal-requests/{REQ3}", headers=H["alice"])
    check("撤销后新请求不继承旧同意", r.json()["approvals"] == [])
    r = c.post(f"/unseal-requests/{REQ3}/approvals", json={"digest": DIG2}, headers=H["bob"])
    check("新请求中旧同意者需重新同意", r.json()["approval_count"] == 1)

    # ---- 到期流程
    r = c.post("/documents", json={"body": "第三份资料", "witnesses": ["bob", "carol", "dave"]},
               headers=H["alice"])
    DOC3, DIG3 = r.json()["id"], r.json()["digest"]
    soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1.2)) + "Z"
    r = c.post(f"/documents/{DOC3}/unseal-requests",
               json={"reason": "r", "deadline": soon}, headers=H["alice"])
    REQ4 = r.json()["id"]
    c.post(f"/unseal-requests/{REQ4}/approvals", json={"digest": DIG3}, headers=H["bob"])
    time.sleep(1.5)
    r = c.post(f"/unseal-requests/{REQ4}/approvals", json={"digest": DIG3}, headers=H["carol"])
    check("到期后同意拒绝 409", r.status_code == 409)
    r = c.get(f"/unseal-requests/{REQ4}", headers=H["alice"])
    check("到期请求状态为 expired", r.json()["status"] == "expired")
    r = c.post(f"/unseal-requests/{REQ4}/revoke", headers=H["alice"])
    check("到期请求撤销拒绝 409", r.status_code == 409)
    r = c.post(f"/documents/{DOC3}/unseal-requests",
               json={"reason": "r-new", "deadline": "2030-01-01T00:00:00Z"}, headers=H["alice"])
    REQ5 = r.json()["id"]
    r = c.get(f"/unseal-requests/{REQ5}", headers=H["alice"])
    check("到期后新请求不继承旧同意", r.json()["approvals"] == [])

    # ---- 操作记录按顺序保存
    r = c.get(f"/documents/{DOC}/log", headers=H["alice"])
    ops = [o["action"] for o in r.json()["operations"]]
    check("操作记录顺序正确", ops == ["seal", "unseal_request", "approve", "approve", "open"], str(ops))
    seqs = [o["seq"] for o in r.json()["operations"]]
    check("操作记录序号单调递增", seqs == sorted(seqs))

# ---- 重启后状态一致（新进程级 TestClient，同一数据库文件）
with TestClient(app) as c2:
    r = c2.get(f"/documents/{DOC}", headers=H["alice"])
    j = r.json()
    check("重启后开封状态与正文一致", j["status"] == "opened" and j["body"] == BODY)
    r = c2.get(f"/documents/{DOC}/log", headers=H["alice"])
    check("重启后操作记录完整", len(r.json()["operations"]) == 5)
    r = c2.get(f"/unseal-requests/{REQ4}", headers=H["alice"])
    check("重启后到期状态一致", r.json()["status"] == "expired")

print(f"\n全部 {len(PASS)} 项检查通过")
