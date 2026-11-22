import uuid
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db.connection import init_db, get_connection
from app.services.ledger_service import (
    execute_transfer,
    InsufficientFundsError,
    AccountNotFoundError,
    TransferHeldError
)
from app.services.idempotency_service import (
    check_or_start_idempotency,
    save_idempotency_response,
    mark_idempotency_failed,
    IdempotencyConflictError,
    IdempotencyMismatchError
)
from app.services.reconciliation_service import run_reconciliation, JobLockError
from app.services.fraud_and_qa_service import answer_statement_question

app = FastAPI(
    title="Payments-Grade Wallet Ledger API",
    description="Double-entry, strictly consistent ledger backend with concurrency safety, idempotency, and nightly reconciliation.",
    version="1.0.0"
)

@app.on_event("startup")
def on_startup():
    init_db()

class TransferRequest(BaseModel):
    user_id: int
    source_account_id: int
    destination_account_id: int
    amount_paise: int = Field(gt=0, description="Amount in whole paise (1 INR = 100 paise)")
    transfer_type: str = "PEER_TRANSFER"
    description: Optional[str] = ""

class QuestionRequest(BaseModel):
    question: str

@app.get("/health")
def health():
    return {"status": "UP", "service": "Payments-Grade Wallet Ledger"}

@app.post("/transfers", status_code=status.HTTP_201_CREATED)
def create_transfer_endpoint(
    req: TransferRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    request_id = str(uuid.uuid4())

    # Milestone 3: Require Idempotency-Key header on all POST writes
    if not idempotency_key or not idempotency_key.strip():
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "code": "MISSING_IDEMPOTENCY_KEY",
                "message": "Header 'Idempotency-Key' is required for all state-mutating requests",
                "request_id": request_id
            }
        )

    body_dict = req.model_dump()

    # Step A: Evaluate Idempotency
    try:
        is_replay, saved_code, saved_body = check_or_start_idempotency(
            user_id=req.user_id,
            key=idempotency_key,
            body_dict=body_dict
        )
    except IdempotencyMismatchError as e:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                "message": str(e),
                "request_id": request_id
            }
        )
    except IdempotencyConflictError as e:
        return JSONResponse(
            status_code=e.status_code,
            content={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": str(e),
                "request_id": request_id
            }
        )

    if is_replay:
        # Exactly identical response returned on key replay
        return JSONResponse(status_code=saved_code, content=saved_body)

    # Step B: Execute the Transfer
    try:
        result = execute_transfer(
            user_id=req.user_id,
            source_account_id=req.source_account_id,
            destination_account_id=req.destination_account_id,
            amount_paise=req.amount_paise,
            transfer_type=req.transfer_type,
            idempotency_key=idempotency_key,
            description=req.description,
            changed_by=f"USER_{req.user_id}"
        )

        response_payload = {
            "success": True,
            "data": result,
            "request_id": request_id
        }
        resp_code = status.HTTP_201_CREATED if result.get("status") == "COMPLETED" else status.HTTP_200_OK

        save_idempotency_response(req.user_id, idempotency_key, resp_code, response_payload)
        return JSONResponse(status_code=resp_code, content=response_payload)

    except InsufficientFundsError as e:
        mark_idempotency_failed(req.user_id, idempotency_key, status.HTTP_400_BAD_REQUEST, str(e))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "code": "INSUFFICIENT_FUNDS",
                "message": str(e),
                "request_id": request_id
            }
        )
    except AccountNotFoundError as e:
        mark_idempotency_failed(req.user_id, idempotency_key, status.HTTP_404_NOT_FOUND, str(e))
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "code": "ACCOUNT_NOT_FOUND",
                "message": str(e),
                "request_id": request_id
            }
        )
    except Exception as e:
        mark_idempotency_failed(req.user_id, idempotency_key, status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "INTERNAL_SERVER_ERROR",
                "message": str(e),
                "request_id": request_id
            }
        )

@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, account_type, currency, balance_paise FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")

        return {
            "account_id": row[0],
            "user_id": row[1],
            "account_type": row[2],
            "currency": row[3],
            "balance_paise": row[4],
            "balance_inr": row[4] / 100.0
        }
    finally:
        conn.close()

@app.get("/accounts/{account_id}/statement")
def get_statement(account_id: int, limit: int = 50, offset: int = 0):
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Verify account exists
        cur.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Account not found")

        # Ordered newest first
        cur.execute("""
            SELECT l.id, l.transfer_id, l.direction, l.amount_paise, l.created_at, t.description, t.transfer_type
            FROM ledger_entries l
            JOIN transfers t ON l.transfer_id = t.id
            WHERE l.account_id = ?
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ? OFFSET ?
        """, (account_id, limit, offset))
        rows = cur.fetchall()

        entries = []
        for r in rows:
            entries.append({
                "entry_id": r[0],
                "transfer_id": r[1],
                "direction": r[2],
                "amount_paise": r[3],
                "amount_inr": r[3] / 100.0,
                "created_at": r[4],
                "description": r[5],
                "transfer_type": r[6]
            })

        return {
            "account_id": account_id,
            "count": len(entries),
            "limit": limit,
            "offset": offset,
            "statement": entries
        }
    finally:
        conn.close()

@app.post("/admin/reconciliation/run")
def trigger_reconciliation():
    try:
        report = run_reconciliation()
        return {"success": True, "reconciliation": report}
    except JobLockError as e:
        raise HTTPException(status_code=409, detail=str(e))

@app.post("/users/{user_id}/statement/ask")
def ask_statement_question(user_id: int, req: QuestionRequest):
    return answer_statement_question(user_id=user_id, question_text=req.question)
