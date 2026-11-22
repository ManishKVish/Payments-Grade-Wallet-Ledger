import unittest
from app.db.connection import init_db, get_connection
from app.services.ledger_service import execute_transfer
from app.services.reconciliation_service import run_reconciliation

class ReconciliationAcceptanceTest(unittest.TestCase):
    def setUp(self):
        conn = get_connection()
        init_db(conn)
        cur = conn.cursor()
        cur.execute("DELETE FROM risk_flags")
        cur.execute("DELETE FROM ledger_entries")
        cur.execute("DELETE FROM transfers")
        cur.execute("DELETE FROM idempotency_keys")
        cur.execute("DELETE FROM reconciliation_problems")
        cur.execute("DELETE FROM reconciliation_runs")
        cur.execute("DELETE FROM accounts WHERE id > 3")
        cur.execute("UPDATE accounts SET balance_paise = 0 WHERE id <= 3")
        cur.execute("DELETE FROM users")
        conn.commit()
        conn.close()

    def test_reconciliation_detects_exactly_three_corruptions(self):
        conn = get_connection()
        cur = conn.cursor()

        # Seed users and wallets with 0 balance
        cur.execute("INSERT INTO users (name, email, phone) VALUES ('RecPayer', 'recpayer@test.com', '9911002201')")
        u1 = cur.lastrowid
        cur.execute("INSERT INTO users (name, email, phone) VALUES ('RecPayee', 'recpayee@test.com', '9911002202')")
        u2 = cur.lastrowid

        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 0)", (u1,))
        acc1 = cur.lastrowid
        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 0)", (u2,))
        acc2 = cur.lastrowid
        conn.commit()
        conn.close()

        # Top up Payer wallet from system CASH_IN account (Account 1)
        execute_transfer(
            user_id=u1,
            source_account_id=1, # CASH_IN
            destination_account_id=acc1,
            amount_paise=2000000,
            transfer_type="TOPUP",
            idempotency_key="rec-topup-001"
        )

        # Seed valid peer transfers
        transfer_ids = []
        for i in range(20):
            res = execute_transfer(
                user_id=u1,
                source_account_id=acc1,
                destination_account_id=acc2,
                amount_paise=1000,
                idempotency_key=f"rec-seed-{i}"
            )
            transfer_ids.append(res["transfer_id"])

        # Run 1: Clean run must detect 0 discrepancies
        clean_report = run_reconciliation()
        self.assertEqual(clean_report["status"], "COMPLETED_CLEAN")
        self.assertEqual(clean_report["problems_count"], 0)

        # -----------------------------------------------------------------
        # INJECT CORRUPTIONS DIRECTLY IN DATABASE:
        # 1. Change one entry's amount
        # 2. Delete one credit
        # 3. Change one stored balance
        # -----------------------------------------------------------------
        conn = get_connection()
        cur = conn.cursor()

        # Corruption 1: Change entry amount on transfer 1 debit
        cur.execute("SELECT id FROM ledger_entries WHERE transfer_id = ? AND direction = 'DEBIT' LIMIT 1", (transfer_ids[0],))
        entry1_id = cur.fetchone()[0]
        cur.execute("UPDATE ledger_entries SET amount_paise = amount_paise + 500 WHERE id = ?", (entry1_id,))

        # Corruption 2: Delete one credit entry on transfer 2
        cur.execute("SELECT id FROM ledger_entries WHERE transfer_id = ? AND direction = 'CREDIT' LIMIT 1", (transfer_ids[1],))
        entry2_id = cur.fetchone()[0]
        cur.execute("DELETE FROM ledger_entries WHERE id = ?", (entry2_id,))

        # Corruption 3: Change one stored balance on Account 1
        cur.execute("UPDATE accounts SET balance_paise = balance_paise + 9999 WHERE id = ?", (acc1,))
        conn.commit()
        conn.close()

        # Run 2: Reconciliation job must detect exactly these problems!
        corrupt_report = run_reconciliation()
        self.assertEqual(corrupt_report["status"], "COMPLETED_WITH_DISCREPANCIES")

        problem_checks = [p["check"] for p in corrupt_report["problems"]]
        self.assertIn("TRANSFER_BALANCED", problem_checks) # Detects corruptions 1 & 2
        self.assertIn("STORED_BALANCE_MATCHES_LEDGER", problem_checks) # Detects corruption 3
        self.assertIn("GLOBAL_DEBITS_EQUALS_CREDITS", problem_checks)

        print("\nReconciliation Job verified corruptions with detailed discrepancy records:")
        for p in corrupt_report["problems"]:
            print(f"  • [{p['check']}] ID={p['id']}: Expected={p['expected']} | Actual={p['actual']}")

if __name__ == "__main__":
    unittest.main()
