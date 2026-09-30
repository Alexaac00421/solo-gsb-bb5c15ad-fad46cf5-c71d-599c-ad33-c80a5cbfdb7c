"""双人见证资料封存服务 — HTTP API。

规则概要：
- 本地注册账号，凭 X-API-Key 操作；
- 封存时提交正文并指定三名不同见证人，保存正文与 SHA-256 摘要，封存后不可改；
- 开封前任何查询/错误响应均不含正文；
- 封存人可提交带理由与截止时刻的开封请求（同一文档同时仅一个待处理请求）；
- 截止时刻前，两名不同指定见证人分别同意才开封，同一人重试只计一次；
- 同意绑定本次请求与原摘要（摘要不符拒绝）；
- 撤销或到期后，旧同意不带入后续请求（同意按 request_id 绑定）；
- 失效凭据 / 非见证人 / 无效时限 / 重复开封一律拒绝且状态不变；
- 并发同意与撤销由全局锁 + IMMEDIATE 事务串行化，只产生一个已提交结果。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .db import connect, init_db, iso, log_op, now_utc, parse_ts, tx

app = FastAPI(title="双人见证资料封存服务", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------- 模型

class AccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class DocumentCreate(BaseModel):
    body: str = Field(min_length=1, description="资料正文")
    witnesses: list[str] = Field(min_length=3, max_length=3,
                                 description="三名不同见证人的用户名")
    title: str = ""


class UnsealRequestCreate(BaseModel):
    reason: str = Field(min_length=1, description="开封理由")
    deadline: str = Field(description="截止时刻，ISO 8601，必须晚于当前时间")


class ApprovalCreate(BaseModel):
    digest: str = Field(min_length=1, description="原资料摘要，须与封存摘要一致")


# ---------------------------------------------------------------- 工具

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_account(x_api_key: str = Header(default="")) -> sqlite3.Row:
    """凭 API Key 认证；失效凭据一律 401。"""
    if not x_api_key:
        raise HTTPException(401, "missing API key")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE api_key_hash = ?",
            (sha256_hex(x_api_key),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(401, "invalid API key")
    return row


def sweep_expired() -> None:
    """把已过期但仍为 pending 的请求标记为 expired（独立事务提交）。"""
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, document_id, deadline FROM unseal_requests WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            if parse_ts(row["deadline"]) <= now_utc():
                conn.execute(
                    "UPDATE unseal_requests SET status = 'expired', closed_at = ? WHERE id = ?",
                    (iso(now_utc()), row["id"]),
                )
                log_op(conn, "system", "expire", row["document_id"], row["id"],
                       "unseal request expired")


def fetch_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise HTTPException(404, "document not found")
    return doc


def fetch_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row:
    req = conn.execute("SELECT * FROM unseal_requests WHERE id = ?", (request_id,)).fetchone()
    if req is None:
        raise HTTPException(404, "unseal request not found")
    return req


def witness_names(conn: sqlite3.Connection, document_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT a.username FROM document_witnesses w"
        " JOIN accounts a ON a.id = w.account_id WHERE w.document_id = ?"
        " ORDER BY a.username",
        (document_id,),
    ).fetchall()
    return [r["username"] for r in rows]


def approvals_of(conn: sqlite3.Connection, request_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT a.username, p.digest, p.created_at FROM approvals p"
        " JOIN accounts a ON a.id = p.witness_id WHERE p.request_id = ?"
        " ORDER BY p.id",
        (request_id,),
    ).fetchall()
    return [{"witness": r["username"], "digest": r["digest"], "approved_at": r["created_at"]}
            for r in rows]


def request_view(conn: sqlite3.Connection, req: sqlite3.Row) -> dict:
    return {
        "id": req["id"],
        "reason": req["reason"],
        "deadline": req["deadline"],
        "status": req["status"],
        "created_at": req["created_at"],
        "closed_at": req["closed_at"],
        "approvals": approvals_of(conn, req["id"]),
    }


# ---------------------------------------------------------------- 账号

@app.post("/accounts", status_code=201)
def create_account(payload: AccountCreate):
    """注册本地账号；API Key 仅在响应中返回一次。"""
    api_key = secrets.token_urlsafe(32)
    with tx() as conn:
        if conn.execute("SELECT 1 FROM accounts WHERE username = ?",
                        (payload.username,)).fetchone():
            raise HTTPException(409, "username already exists")
        cur = conn.execute(
            "INSERT INTO accounts (username, api_key_hash, created_at) VALUES (?, ?, ?)",
            (payload.username, sha256_hex(api_key), iso(now_utc())),
        )
        account_id = cur.lastrowid
    return {"id": account_id, "username": payload.username, "api_key": api_key}


@app.get("/accounts")
def list_accounts(_: sqlite3.Row = Depends(get_account)):
    """列出用户名（用于指定见证人），不含任何凭据。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT username, created_at FROM accounts ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"accounts": [{"username": r["username"], "created_at": r["created_at"]}
                         for r in rows]}


# ---------------------------------------------------------------- 封存

@app.post("/documents", status_code=201)
def seal_document(payload: DocumentCreate, account: sqlite3.Row = Depends(get_account)):
    """封存资料：保存正文与摘要，指定三名不同见证人。封存后内容不可改。"""
    if len(set(payload.witnesses)) != 3:
        raise HTTPException(422, "witnesses must be three distinct usernames")
    digest = sha256_hex(payload.body)
    with tx() as conn:
        id_to_name = {}
        for name in payload.witnesses:
            row = conn.execute("SELECT id FROM accounts WHERE username = ?",
                               (name,)).fetchone()
            if row is None:
                raise HTTPException(422, f"witness account not found: {name}")
            id_to_name[row["id"]] = name
        cur = conn.execute(
            "INSERT INTO documents (owner_id, title, body, digest, status, created_at)"
            " VALUES (?, ?, ?, ?, 'sealed', ?)",
            (account["id"], payload.title, payload.body, digest, iso(now_utc())),
        )
        doc_id = cur.lastrowid
        for wid in id_to_name:
            conn.execute(
                "INSERT INTO document_witnesses (document_id, account_id) VALUES (?, ?)",
                (doc_id, wid),
            )
        log_op(conn, account["username"], "seal", doc_id, None,
               f"sealed with digest {digest}")
    return {
        "id": doc_id,
        "status": "sealed",
        "digest": digest,
        "witnesses": sorted(id_to_name.values()),
    }


@app.get("/documents/{document_id}")
def get_document(document_id: int, account: sqlite3.Row = Depends(get_account)):
    """查询资料。开封前响应不含正文；开封后返回正文、摘要、理由及见证记录。"""
    sweep_expired()
    conn = connect()
    try:
        doc = fetch_document(conn, document_id)
        witnesses = witness_names(conn, document_id)
        result = {
            "id": doc["id"],
            "title": doc["title"],
            "owner": conn.execute("SELECT username FROM accounts WHERE id = ?",
                                  (doc["owner_id"],)).fetchone()["username"],
            "status": doc["status"],
            "digest": doc["digest"],
            "witnesses": witnesses,
            "created_at": doc["created_at"],
        }
        if doc["status"] == "opened":
            req = conn.execute(
                "SELECT * FROM unseal_requests WHERE document_id = ? AND status = 'opened'"
                " ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            result.update({
                "opened_at": doc["opened_at"],
                "body": doc["body"],
                "unseal": request_view(conn, req) if req else None,
            })
        else:
            # 开封前：只暴露元数据与请求进度，绝不包含正文
            req = conn.execute(
                "SELECT * FROM unseal_requests WHERE document_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            result["latest_request"] = request_view(conn, req) if req else None
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------- 开封请求

@app.post("/documents/{document_id}/unseal-requests", status_code=201)
def create_unseal_request(document_id: int, payload: UnsealRequestCreate,
                          account: sqlite3.Row = Depends(get_account)):
    """封存人提交开封请求（理由 + 截止时刻）。同一文档同时仅允许一个待处理请求。"""
    try:
        deadline = parse_ts(payload.deadline)
    except ValueError:
        raise HTTPException(400, "invalid deadline: not an ISO 8601 timestamp")
    if deadline <= now_utc():
        raise HTTPException(400, "invalid deadline: must be in the future")

    sweep_expired()
    with tx() as conn:
        doc = fetch_document(conn, document_id)
        if doc["owner_id"] != account["id"]:
            raise HTTPException(403, "only the owner can request unsealing")
        if doc["status"] == "opened":
            raise HTTPException(409, "document already opened")
        pending = conn.execute(
            "SELECT 1 FROM unseal_requests WHERE document_id = ? AND status = 'pending'",
            (document_id,),
        ).fetchone()
        if pending:
            raise HTTPException(409, "an unseal request is already pending")
        cur = conn.execute(
            "INSERT INTO unseal_requests (document_id, reason, deadline, status, created_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (document_id, payload.reason, iso(deadline), iso(now_utc())),
        )
        req_id = cur.lastrowid
        log_op(conn, account["username"], "unseal_request", document_id, req_id,
               f"reason={payload.reason!r} deadline={iso(deadline)}")
    return {"id": req_id, "document_id": document_id, "status": "pending",
            "reason": payload.reason, "deadline": iso(deadline)}


@app.get("/unseal-requests/{request_id}")
def get_unseal_request(request_id: int, account: sqlite3.Row = Depends(get_account)):
    """查询开封请求状态与已绑定同意（不含资料正文）。"""
    sweep_expired()
    conn = connect()
    try:
        req = fetch_request(conn, request_id)
        view = request_view(conn, req)
        view["document_id"] = req["document_id"]
        return view
    finally:
        conn.close()


# ---------------------------------------------------------------- 见证同意 / 撤销

@app.post("/unseal-requests/{request_id}/approvals")
def approve_unseal(request_id: int, payload: ApprovalCreate,
                   account: sqlite3.Row = Depends(get_account)):
    """指定见证人同意开封。同意绑定本次请求与原摘要；两人同意即开封。"""
    sweep_expired()
    with tx() as conn:
        req = fetch_request(conn, request_id)
        if req["status"] != "pending":
            raise HTTPException(409, f"unseal request is {req['status']}")
        doc = fetch_document(conn, req["document_id"])
        if doc["status"] == "opened":
            raise HTTPException(409, "document already opened")
        is_witness = conn.execute(
            "SELECT 1 FROM document_witnesses WHERE document_id = ? AND account_id = ?",
            (doc["id"], account["id"]),
        ).fetchone()
        if not is_witness:
            raise HTTPException(403, "not a designated witness of this document")
        if payload.digest != doc["digest"]:
            raise HTTPException(400, "digest mismatch with the sealed document")

        existing = conn.execute(
            "SELECT 1 FROM approvals WHERE request_id = ? AND witness_id = ?",
            (request_id, account["id"]),
        ).fetchone()
        if existing:
            # 同一人重试只计一次：幂等返回，不改变状态
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                (request_id,),
            ).fetchone()["c"]
            return {"request_id": request_id, "already_approved": True,
                    "approval_count": count, "document_status": doc["status"]}

        conn.execute(
            "INSERT INTO approvals (request_id, witness_id, digest, created_at)"
            " VALUES (?, ?, ?, ?)",
            (request_id, account["id"], payload.digest, iso(now_utc())),
        )
        log_op(conn, account["username"], "approve", doc["id"], request_id,
               f"digest={payload.digest}")
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
            (request_id,),
        ).fetchone()["c"]

        opened = False
        if count >= 2:
            now = iso(now_utc())
            conn.execute(
                "UPDATE unseal_requests SET status = 'opened', closed_at = ? WHERE id = ?",
                (now, request_id),
            )
            conn.execute(
                "UPDATE documents SET status = 'opened', opened_at = ? WHERE id = ?",
                (now, doc["id"]),
            )
            log_op(conn, "system", "open", doc["id"], request_id,
                   "two distinct witnesses approved; document opened")
            opened = True
        return {"request_id": request_id, "already_approved": False,
                "approval_count": count,
                "document_status": "opened" if opened else "sealed"}


@app.post("/unseal-requests/{request_id}/revoke")
def revoke_unseal(request_id: int, account: sqlite3.Row = Depends(get_account)):
    """封存人撤销开封请求；撤销后该请求的同意对后续请求无效。"""
    sweep_expired()
    with tx() as conn:
        req = fetch_request(conn, request_id)
        doc = fetch_document(conn, req["document_id"])
        if doc["owner_id"] != account["id"]:
            raise HTTPException(403, "only the owner can revoke the request")
        if req["status"] != "pending":
            raise HTTPException(409, f"unseal request is {req['status']}")
        conn.execute(
            "UPDATE unseal_requests SET status = 'revoked', closed_at = ? WHERE id = ?",
            (iso(now_utc()), request_id),
        )
        log_op(conn, account["username"], "revoke", doc["id"], request_id,
               "unseal request revoked")
    return {"request_id": request_id, "status": "revoked"}


# ---------------------------------------------------------------- 操作记录

@app.get("/documents/{document_id}/log")
def get_document_log(document_id: int, account: sqlite3.Row = Depends(get_account)):
    """按顺序返回该资料的全部操作记录（持久化，重启后保持一致）。"""
    conn = connect()
    try:
        fetch_document(conn, document_id)
        rows = conn.execute(
            "SELECT seq, ts, actor, action, request_id, detail FROM operation_log"
            " WHERE document_id = ? ORDER BY seq",
            (document_id,),
        ).fetchall()
        return {"document_id": document_id, "operations": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}
