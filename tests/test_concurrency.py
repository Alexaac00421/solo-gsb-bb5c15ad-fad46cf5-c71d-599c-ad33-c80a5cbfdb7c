#!/usr/bin/env python3
"""并发验收：真实 HTTP 服务下，并发"同意 vs 撤销"与"同意 vs 同意"只产生一个已提交结果。"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8123"
DB = "/tmp/seal_conc.db"
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

env = dict(os.environ, SEAL_DB_PATH=DB, PYTHONPATH="/workspace")
srv = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8123"],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def wait_up():
    for _ in range(50):
        try:
            s, _ = call("GET", "/health")
            if s == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not start")


def race(trial):
    keys = {}
    for n in ["o", "w1", "w2", "w3"]:
        s, r = call("POST", "/accounts", {"username": f"t{trial}-{n}"})
        keys[n] = r["api_key"]
    s, doc = call("POST", "/documents", {"body": f"race-{trial}",
                                         "witnesses": [f"t{trial}-w1", f"t{trial}-w2", f"t{trial}-w3"]},
                  keys["o"])
    doc_id, digest = doc["id"], doc["digest"]
    s, req = call("POST", f"/documents/{doc_id}/unseal-requests",
                  {"reason": "race", "deadline": "2030-01-01T00:00:00Z"}, keys["o"])
    req_id = req["id"]
    # 先一人同意，制造"第二个同意"与"撤销"的竞争
    call("POST", f"/unseal-requests/{req_id}/approvals", {"digest": digest}, keys["w1"])

    results = {}

    def approve():
        results["approve"] = call("POST", f"/unseal-requests/{req_id}/approvals",
                                  {"digest": digest}, keys["w2"])

    def revoke():
        results["revoke"] = call("POST", f"/unseal-requests/{req_id}/revoke", None, keys["o"])

    t1, t2 = threading.Thread(target=approve), threading.Thread(target=revoke)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted([results["approve"][0], results["revoke"][0]])
    assert codes == [200, 409], f"trial {trial}: unexpected {codes} {results}"
    s, doc = call("GET", f"/documents/{doc_id}", key=keys["o"])
    s2, rq = call("GET", f"/unseal-requests/{req_id}", key=keys["o"])
    if results["approve"][0] == 200:
        assert doc["status"] == "opened" and rq["status"] == "opened", (doc["status"], rq["status"])
        assert doc["body"] == f"race-{trial}"
        outcome = "opened"
    else:
        assert doc["status"] == "sealed" and rq["status"] == "revoked", (doc["status"], rq["status"])
        assert "body" not in doc
        outcome = "revoked"
    # 终态下两个操作都必须被拒绝
    a = call("POST", f"/unseal-requests/{req_id}/approvals", {"digest": digest}, keys["w3"])
    v = call("POST", f"/unseal-requests/{req_id}/revoke", None, keys["o"])
    assert a[0] == 409 and v[0] == 409, (a, v)
    return outcome


try:
    wait_up()
    outcomes = {}
    for i in range(20):
        o = race(i)
        outcomes[o] = outcomes.get(o, 0) + 1
    print(f"20 轮并发竞争全部只有一个已提交结果: {outcomes}")
finally:
    srv.terminate()
