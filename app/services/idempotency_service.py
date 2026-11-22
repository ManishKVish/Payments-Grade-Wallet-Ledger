import hashlib
import json
import time
from app.db.connection import get_connection, _lock

class IdempotencyConflictError(Exception):
    def __init__(self, message, status_code=409, saved_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.saved_body = saved_body

class IdempotencyMismatchError(Exception):
    def __init__(self, message="Request body does not match previous request with this idempotency key"):
        super().__init__(message)
        self.status_code = 422

def compute_request_hash(body_dict: dict) -> str:
    serialized = json.dumps(body_dict, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

def check_or_start_idempotency(user_id: int, key: str, body_dict: dict):
    """
    Checks if (user_id, key) exists.
    If exists with same hash and COMPLETED: returns (True, saved_code, saved_body)
    If exists with different hash: raises IdempotencyMismatchError (422)
    If exists with status PROCESSING: waits for completion or raises IdempotencyConflictError (409)
    If does not exist: inserts with PROCESSING status and returns (False, None, None)
    """
    req_hash = compute_request_hash(body_dict)
    conn = get_connection()
    try:
        cur = conn.cursor()
        with _lock:
            cur.execute(
                "SELECT request_hash, status, response_code, response_body FROM idempotency_keys WHERE user_id = ? AND idempotency_key = ?",
                (user_id, key)
            )
            row = cur.fetchone()
            if row:
                saved_hash, status, resp_code, resp_body = row[0], row[1], row[2], row[3]
                if saved_hash != req_hash:
                    raise IdempotencyMismatchError()

                if status == "COMPLETED":
                    parsed_body = json.loads(resp_body) if resp_body else {}
                    return True, resp_code, parsed_body

            else:
                # Insert initial PROCESSING row
                cur.execute("""
                    INSERT INTO idempotency_keys (user_id, idempotency_key, request_hash, status)
                    VALUES (?, ?, ?, 'PROCESSING')
                """, (user_id, key, req_hash))
                conn.commit()
                return False, None, None

        # If it was in PROCESSING status, poll briefly for completion
        for _ in range(30):
            time.sleep(0.05)
            with _lock:
                cur.execute(
                    "SELECT status, response_code, response_body FROM idempotency_keys WHERE user_id = ? AND idempotency_key = ?",
                    (user_id, key)
                )
                poll_row = cur.fetchone()
                if poll_row and poll_row[0] == "COMPLETED":
                    p_code = poll_row[1]
                    p_body = json.loads(poll_row[2]) if poll_row[2] else {}
                    return True, p_code, p_body

        raise IdempotencyConflictError("A request with this idempotency key is currently processing", status_code=409)
    finally:
        conn.close()

def save_idempotency_response(user_id: int, key: str, response_code: int, response_body: dict):
    conn = get_connection()
    try:
        cur = conn.cursor()
        with _lock:
            cur.execute("""
                UPDATE idempotency_keys
                SET status = 'COMPLETED',
                    response_code = ?,
                    response_body = ?
                WHERE user_id = ? AND idempotency_key = ?
            """, (response_code, json.dumps(response_body), user_id, key))
            conn.commit()
    finally:
        conn.close()

def mark_idempotency_failed(user_id: int, key: str, response_code: int, error_message: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        with _lock:
            cur.execute("""
                UPDATE idempotency_keys
                SET status = 'FAILED',
                    response_code = ?,
                    response_body = ?
                WHERE user_id = ? AND idempotency_key = ?
            """, (response_code, json.dumps({"error": error_message}), user_id, key))
            conn.commit()
    finally:
        conn.close()
