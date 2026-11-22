import threading
from app.db.connection import get_connection, _lock

class InsufficientFundsError(Exception):
    pass

class AccountNotFoundError(Exception):
    pass

class TransferHeldError(Exception):
    def __init__(self, transfer_id, rule_name, message):
        super().__init__(message)
        self.transfer_id = transfer_id
        self.rule_name = rule_name

def execute_transfer(
    user_id: int,
    source_account_id: int,
    destination_account_id: int,
    amount_paise: int,
    transfer_type: str = "PEER_TRANSFER",
    idempotency_key: str = None,
    description: str = "",
    changed_by: str = "SYSTEM"
):
    """
    Executes a double-entry money transfer with deterministic row-locking (ascending order),
    pre-flight fraud rule evaluation, immutable ledger insertion, and balance updates.
    """
    if amount_paise <= 0:
        raise ValueError("Transfer amount must be strictly positive (> 0 paise)")

    if source_account_id == destination_account_id:
        raise ValueError("Source and destination accounts must be distinct")

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Step 1: Deterministic Ascending Lock Order to Prevent Deadlocks
        # Locking in numerical order eliminates circular wait deadlocks across concurrent threads.
        first_lock_id, second_lock_id = sorted([source_account_id, destination_account_id])

        with _lock: # Global lock guard for thread-safety in test SQLite / DB wrapper
            # In PostgreSQL: SELECT id, account_type, balance_paise FROM accounts WHERE id = ? FOR UPDATE;
            cur.execute("SELECT id, account_type, balance_paise FROM accounts WHERE id = ?", (first_lock_id,))
            first_row = cur.fetchone()
            cur.execute("SELECT id, account_type, balance_paise FROM accounts WHERE id = ?", (second_lock_id,))
            second_row = cur.fetchone()

            if not first_row or not second_row:
                raise AccountNotFoundError("One or both accounts do not exist")

            accounts_by_id = {first_row[0]: first_row, second_row[0]: second_row}
            source_acc = accounts_by_id[source_account_id]
            dest_acc = accounts_by_id[destination_account_id]

            source_type = source_acc[1]
            source_balance = source_acc[2]

            # Step 2: Validate Available Balance for User Wallets
            if source_type == "USER_WALLET" and source_balance < amount_paise:
                raise InsufficientFundsError(
                    f"Insufficient funds: available {source_balance} paise, requested {amount_paise} paise"
                )

            # Step 3: Evaluate Fraud Rules (Milestone 5) inside transaction before commit
            cur.execute(
                "SELECT id, rule_name, threshold_paise, action FROM risk_rules WHERE is_active = 1"
            )
            rules = cur.fetchall()
            held_by_rule = None
            for r in rules:
                r_id, r_name, threshold, action = r[0], r[1], r[2], r[3]
                if r_name == "SINGLE_TRANSACTION_LIMIT" and threshold and amount_paise > threshold:
                    held_by_rule = (r_id, r_name, action)
                    break

            if held_by_rule:
                rule_id, rule_name, action = held_by_rule
                # Insert transfer in HELD_FOR_REVIEW status - NO MONEY MOVES
                cur.execute("""
                    INSERT INTO transfers (idempotency_key, transfer_type, status, amount_paise, description)
                    VALUES (?, ?, 'HELD_FOR_REVIEW', ?, ?)
                """, (idempotency_key, transfer_type, amount_paise, description))
                transfer_id = cur.lastrowid

                cur.execute("""
                    INSERT INTO risk_flags (transfer_id, rule_id, decision, llm_explanation)
                    VALUES (?, ?, 'HELD', ?)
                """, (transfer_id, rule_id, f"Flagged by rule {rule_name}: amount {amount_paise} exceeds threshold"))

                cur.execute("""
                    INSERT INTO audit_logs (entity_name, entity_id, previous_status, new_status, changed_by, change_reason)
                    VALUES ('transfer', ?, 'NONE', 'HELD_FOR_REVIEW', ?, ?)
                """, (transfer_id, changed_by, f"Triggered risk rule: {rule_name}"))

                conn.commit()
                return {
                    "transfer_id": transfer_id,
                    "status": "HELD_FOR_REVIEW",
                    "amount_paise": amount_paise,
                    "message": f"Transfer flagged and held for manual review by rule {rule_name}"
                }

            # Step 4: Record Completed Transfer
            cur.execute("""
                INSERT INTO transfers (idempotency_key, transfer_type, status, amount_paise, description)
                VALUES (?, ?, 'COMPLETED', ?, ?)
            """, (idempotency_key, transfer_type, amount_paise, description))
            transfer_id = cur.lastrowid

            # Step 5: Post Immutable Double-Entry Ledger Lines
            # Line A: DEBIT on Source Account
            cur.execute("""
                INSERT INTO ledger_entries (transfer_id, account_id, direction, amount_paise)
                VALUES (?, ?, 'DEBIT', ?)
            """, (transfer_id, source_account_id, amount_paise))

            # Line B: CREDIT on Destination Account
            cur.execute("""
                INSERT INTO ledger_entries (transfer_id, account_id, direction, amount_paise)
                VALUES (?, ?, 'CREDIT', ?)
            """, (transfer_id, destination_account_id, amount_paise))

            # Step 6: Update Cached Materialized Balances
            cur.execute("""
                UPDATE accounts SET balance_paise = balance_paise - ? WHERE id = ?
            """, (amount_paise, source_account_id))

            cur.execute("""
                UPDATE accounts SET balance_paise = balance_paise + ? WHERE id = ?
            """, (amount_paise, destination_account_id))

            # Step 7: Record Audit Log Entry
            cur.execute("""
                INSERT INTO audit_logs (entity_name, entity_id, previous_status, new_status, changed_by, change_reason)
                VALUES ('transfer', ?, 'NONE', 'COMPLETED', ?, 'Standard execution')
            """, (transfer_id, changed_by))

            conn.commit()

            return {
                "transfer_id": transfer_id,
                "status": "COMPLETED",
                "source_account_id": source_account_id,
                "destination_account_id": destination_account_id,
                "amount_paise": amount_paise,
                "description": description
            }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
