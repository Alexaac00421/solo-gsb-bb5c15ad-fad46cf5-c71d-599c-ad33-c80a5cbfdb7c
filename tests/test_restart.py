#!/usr/bin/env python3
"""真实进程重启验收：杀进程 → 同库重启 → 状态与操作记录完全一致。"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8201"
DB = "/tmp/seal_restart.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

env = dict(os.environ, SEAL_DB_PATH=DB, PYTHONPATH="/workspace")


def start():
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                          "--host", "127.0.0.1", "--port", "8201"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1)
            return p
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def call(method, path, body=None, key=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data) as resp:
        return json.loads(resp.read())


srv = start()
try:
    keys = {}
    for n in ["r-alice", "r-bob", "r-carol", "r-dave"]:
        keys[n] = call("POST", "/accounts", {"username": n})["api_key"]
    doc = call("POST", "/documents", {"body": "重启持久化验证正文",
                                      "witnesses": ["r-bob", "r-carol", "r-dave"]}, keys["r-alice"])
    req = call("POST", f"/documents/{doc['id']}/unseal-requests",
               {"reason": "验证重启", "deadline": "2030-01-01T00:00:00Z"}, keys["r-alice"])
    call("POST", f"/unseal-requests/{req['id']}/approvals", {"digest": doc["digest"]}, keys["r-bob"])
    call("POST", f"/unseal-requests/{req['id']}/approvals", {"digest": doc["digest"]}, keys["r-carol"])
    before_doc = call("GET", f"/documents/{doc['id']}", key=keys["r-alice"])
    before_log = call("GET", f"/documents/{doc['id']}/log", key=keys["r-alice"])
    assert before_doc["status"] == "opened" and before_doc["body"] == "重启持久化验证正文"
finally:
    srv.send_signal(signal.SIGKILL)  # 模拟崩溃/重启
    srv.wait()

srv = start()
try:
    after_doc = call("GET", f"/documents/{doc['id']}", key=keys["r-alice"])
    after_log = call("GET", f"/documents/{doc['id']}/log", key=keys["r-alice"])
    assert after_doc == before_doc, "document state changed across restart"
    assert after_log == before_log, "operation log changed across restart"
    print("重启后文档状态一致:", after_doc["status"], "| 见证记录:",
          [a["witness"] for a in after_doc["unseal"]["approvals"]])
    print("重启后操作记录一致:", [o["action"] for o in after_log["operations"]])
finally:
    srv.terminate()
