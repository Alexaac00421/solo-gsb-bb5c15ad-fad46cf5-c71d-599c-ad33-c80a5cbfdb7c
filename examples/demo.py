#!/usr/bin/env python3
"""端到端调用示例：注册 → 封存 → 开封请求 → 双人见证同意 → 开封查询。

仅使用 Python 标准库。用法：
    python3 examples/demo.py [base_url]      # 默认 http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method, path, body=None, key=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def show(title, status, payload):
    print(f"\n=== {title} -> HTTP {status}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


# 1. 注册：1 名封存人 + 3 名见证人
keys = {}
for name in ["alice", "bob", "carol", "dave"]:
    s, r = call("POST", "/accounts", {"username": name})
    if s == 409:  # 已存在则无法取回旧 key，演示请使用全新数据库
        raise SystemExit(f"用户 {name} 已存在，请换名或清空数据库后重试")
    keys[name] = show(f"注册 {name}", s, r)["api_key"]

# 2. 封存资料，指定三名不同见证人
s, doc = call("POST", "/documents", {
    "title": "遗嘱正本",
    "body": "本人名下全部资产由长女继承……",
    "witnesses": ["bob", "carol", "dave"],
}, keys["alice"])
doc = show("封存资料", s, doc)
doc_id, digest = doc["id"], doc["digest"]

# 3. 开封前查询：响应不含正文
s, r = call("GET", f"/documents/{doc_id}", key=keys["bob"])
r = show("开封前查询（无正文）", s, r)
assert "body" not in r

# 4. 无效请求示例：坏凭据 / 非见证人同意 / 无效时限
show("失效凭据", *call("GET", f"/documents/{doc_id}", key="bad-key"))
show("无效时限", *call("POST", f"/documents/{doc_id}/unseal-requests",
                       {"reason": "x", "deadline": "2000-01-01T00:00:00Z"}, keys["alice"]))

# 5. 提交开封请求（理由 + 截止时刻）
s, req = call("POST", f"/documents/{doc_id}/unseal-requests",
              {"reason": "立嘱人去世，需执行遗嘱", "deadline": "2030-01-01T00:00:00Z"},
              keys["alice"])
req = show("提交开封请求", s, req)
req_id = req["id"]

# 6. 见证：非见证人拒绝 → 错误摘要拒绝 → 两人同意开封 → 重复只计一次
show("非见证人同意被拒", *call("POST", f"/unseal-requests/{req_id}/approvals",
                               {"digest": digest}, keys["alice"]))
show("摘要不符被拒", *call("POST", f"/unseal-requests/{req_id}/approvals",
                           {"digest": "0" * 64}, keys["bob"]))
show("bob 同意", *call("POST", f"/unseal-requests/{req_id}/approvals",
                       {"digest": digest}, keys["bob"]))
show("bob 重试（只计一次）", *call("POST", f"/unseal-requests/{req_id}/approvals",
                                   {"digest": digest}, keys["bob"]))
show("carol 同意（达成两人，开封）", *call("POST", f"/unseal-requests/{req_id}/approvals",
                                           {"digest": digest}, keys["carol"]))

# 7. 开封后查询：返回正文、摘要、理由及见证记录
s, r = call("GET", f"/documents/{doc_id}", key=keys["dave"])
show("开封后查询（含正文与见证记录）", s, r)

# 8. 重复开封被拒
show("重复开封被拒", *call("POST", f"/documents/{doc_id}/unseal-requests",
                           {"reason": "again", "deadline": "2031-01-01T00:00:00Z"},
                           keys["alice"]))

# 9. 操作记录（按顺序持久保存）
show("操作记录", *call("GET", f"/documents/{doc_id}/log", key=keys["alice"]))
