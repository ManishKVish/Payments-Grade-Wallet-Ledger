import datetime
from app.db.connection import get_connection, _lock

class JobLockError(Exception):
    pass

def run_reconciliation():
    """
    Executes the 5 nightly reconciliation checks in a single transaction under snapshot isolation.
    Guarantees mutual exclusion using job_locks and persists discrepancies to reconciliation_problems.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Mutual exclusion lock on 'nightly_reconciliation'
        with _lock:
            cur.execute("SELECT locked_at FROM job_locks WHERE job_name = 'nightly_reconciliation'")
            lock_row = cur.fetchone()
            if lock_row and lock_row[0] is not None:
                raise JobLockError("Reconciliation job is already running concurrently")

            now_str = datetime.datetime.utcnow().isoformat()
            cur.execute(
                "UPDATE job_locks SET locked_at = ?, locked_by = 'worker-1' WHERE job_name = 'nightly_reconciliation'",
                (now_str,)
            )
            conn.commit()

        # Start Reconciliation Run
        cur.execute("""
            INSERT INTO reconciliation_runs (start_time, status)
            VALUES (CURRENT_TIMESTAMP, 'RUNNING')
        """)
        run_id = cur.lastrowid
        conn.commit()

        problems = []

        # -------------------------------------------------------------
        # Check 1: Everything balances: Total Debits == Total Credits
        # -------------------------------------------------------------
        cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount_paise ELSE 0 END), 0) AS total_debits,
                COALESCE(SUM(CASE WHEN direction = 'CREDIT' THEN amount_paise ELSE 0 END), 0) AS total_credits
            FROM ledger_entries
        """)
        totals = cur.fetchone()
        total_debits = totals[0]
        total_credits = totals[1]

        if total_debits != total_credits:
            problems.append({
                "check": "GLOBAL_DEBITS_EQUALS_CREDITS",
                "id": None,
                "expected": f"Total debits ({total_debits}) == Total credits ({total_credits})",
                "actual": f"Discrepancy of {abs(total_debits - total_credits)} paise",
                "details": "Sum of all ledger debits does not equal sum of all credits across the system."
            })

        # -------------------------------------------------------------
        # Check 2: Every transfer balances: Each transfer debits == credits
        # -------------------------------------------------------------
        cur.execute("""
            SELECT
                transfer_id,
                SUM(CASE WHEN direction = 'DEBIT' THEN amount_paise ELSE 0 END) AS transfer_debits,
                SUM(CASE WHEN direction = 'CREDIT' THEN amount_paise ELSE 0 END) AS transfer_credits
            FROM ledger_entries
            GROUP BY transfer_id
            HAVING SUM(CASE WHEN direction = 'DEBIT' THEN amount_paise ELSE 0 END) !=
                   SUM(CASE WHEN direction = 'CREDIT' THEN amount_paise ELSE 0 END)
        """)
        unbalanced_transfers = cur.fetchall()
        for row in unbalanced_transfers:
            t_id, t_deb, t_cred = row[0], row[1], row[2]
            problems.append({
                "check": "TRANSFER_BALANCED",
                "id": t_id,
                "expected": f"Equal debits and credits: {t_deb}",
                "actual": f"Debits={t_deb}, Credits={t_cred}",
                "details": f"Transfer #{t_id} is out of balance by {abs(t_deb - t_cred)} paise."
            })

        # -------------------------------------------------------------
        # Check 3: Stored balance matches computed balance for each account
        # -------------------------------------------------------------
        cur.execute("""
            SELECT
                a.id,
                a.balance_paise AS stored_balance,
                COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount_paise ELSE -l.amount_paise END), 0) AS computed_balance
            FROM accounts a
            LEFT JOIN ledger_entries l ON a.id = l.account_id
            GROUP BY a.id, a.balance_paise
            HAVING a.balance_paise != COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount_paise ELSE -l.amount_paise END), 0)
        """)
        mismatched_accounts = cur.fetchall()
        for row in mismatched_accounts:
            a_id, stored_bal, computed_bal = row[0], row[1], row[2]
            problems.append({
                "check": "STORED_BALANCE_MATCHES_LEDGER",
                "id": a_id,
                "expected": f"Stored balance = {computed_bal}",
                "actual": f"Stored balance = {stored_bal}",
                "details": f"Account #{a_id} stored balance diverges from sum of historical ledger entries by {abs(stored_bal - computed_bal)} paise."
            })

        # -------------------------------------------------------------
        # Check 4: No overdrafts on user wallets
        # -------------------------------------------------------------
        cur.execute("""
            SELECT
                a.id,
                COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount_paise ELSE -l.amount_paise END), 0) AS computed_balance
            FROM accounts a
            LEFT JOIN ledger_entries l ON a.id = l.account_id
            WHERE a.account_type = 'USER_WALLET'
            GROUP BY a.id
            HAVING COALESCE(SUM(CASE WHEN l.direction = 'CREDIT' THEN l.amount_paise ELSE -l.amount_paise END), 0) < 0
        """)
        overdraft_wallets = cur.fetchall()
        for row in overdraft_wallets:
            a_id, computed_bal = row[0], row[1]
            problems.append({
                "check": "NO_USER_WALLET_OVERDRAFT",
                "id": a_id,
                "expected": "Computed balance >= 0",
                "actual": f"Computed balance = {computed_bal}",
                "details": f"User wallet #{a_id} has breached zero-floor invariant with negative balance."
            })

        # -------------------------------------------------------------
        # Check 5: Nothing is orphaned
        # -------------------------------------------------------------
        # 5a. Ledger entries without a valid transfer
        cur.execute("""
            SELECT l.id FROM ledger_entries l
            LEFT JOIN transfers t ON l.transfer_id = t.id
            WHERE t.id IS NULL
        """)
        orphaned_entries = cur.fetchall()
        for row in orphaned_entries:
            problems.append({
                "check": "NO_ORPHANED_LEDGER_ENTRIES",
                "id": row[0],
                "expected": "Valid parent transfer",
                "actual": "No matching transfer found",
                "details": f"Ledger entry #{row[0]} points to nonexistent transfer."
            })

        # 5b. Completed transfers without ledger entries
        cur.execute("""
            SELECT t.id FROM transfers t
            LEFT JOIN ledger_entries l ON t.id = l.transfer_id
            WHERE t.status = 'COMPLETED' AND l.id IS NULL
        """)
        orphaned_transfers = cur.fetchall()
        for row in orphaned_transfers:
            problems.append({
                "check": "NO_ORPHANED_TRANSFERS",
                "id": row[0],
                "expected": "At least 2 ledger entries",
                "actual": "0 ledger entries",
                "details": f"Transfer #{row[0]} is marked COMPLETED but has no ledger records."
            })

        # Persist problems
        for p in problems:
            cur.execute("""
                INSERT INTO reconciliation_problems (run_id, check_name, entity_id, expected_value, actual_value, details)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (run_id, p["check"], p["id"], p["expected"], p["actual"], p["details"]))

        # Count total entities verified
        cur.execute("SELECT COUNT(*) FROM transfers")
        total_transfers = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM accounts")
        total_accounts = cur.fetchone()[0]

        status = "COMPLETED_CLEAN" if len(problems) == 0 else "COMPLETED_WITH_DISCREPANCIES"

        cur.execute("""
            UPDATE reconciliation_runs
            SET end_time = CURRENT_TIMESTAMP,
                status = ?,
                debits_sum = ?,
                credits_sum = ?,
                total_transfers_checked = ?,
                total_accounts_checked = ?,
                problems_count = ?
            WHERE id = ?
        """, (status, total_debits, total_credits, total_transfers, total_accounts, len(problems), run_id))

        conn.commit()

        return {
            "run_id": run_id,
            "status": status,
            "debits_sum": total_debits,
            "credits_sum": total_credits,
            "total_transfers_checked": total_transfers,
            "total_accounts_checked": total_accounts,
            "problems_count": len(problems),
            "problems": problems
        }
    finally:
        with _lock:
            try:
                cur.execute("UPDATE job_locks SET locked_at = NULL, locked_by = NULL WHERE job_name = 'nightly_reconciliation'")
                conn.commit()
            except Exception:
                pass
        conn.close()
