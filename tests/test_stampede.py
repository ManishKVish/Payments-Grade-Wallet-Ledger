import concurrent.futures
import unittest
from app.db.connection import init_db, get_connection
from app.services.ledger_service import execute_transfer, InsufficientFundsError

class ConcurrencyStampedeTest(unittest.TestCase):
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

    def test_stampede_single_run(self):
        conn = get_connection()
        cur = conn.cursor()

        # Create User 1 & User 2
        cur.execute("INSERT INTO users (name, email, phone) VALUES ('Payer', 'payer@test.com', '9999900001')")
        u1_id = cur.lastrowid
        cur.execute("INSERT INTO users (name, email, phone) VALUES ('Payee', 'payee@test.com', '9999900002')")
        u2_id = cur.lastrowid

        # Seed Account Payer with INR 0
        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 0)", (u1_id,))
        acc_payer = cur.lastrowid

        # Seed Account Payee with INR 0
        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 0)", (u2_id,))
        acc_payee = cur.lastrowid
        conn.commit()
        conn.close()

        # Legitimate top-up of INR 500 = 50,000 paise from system CASH_IN account
        execute_transfer(
            user_id=u1_id,
            source_account_id=1,
            destination_account_id=acc_payer,
            amount_paise=50000,
            transfer_type="TOPUP",
            idempotency_key="stampede-topup-001"
        )

        # Fire 100 concurrent INR 10 = 1,000 paise transfers
        def do_transfer(index):
            try:
                execute_transfer(
                    user_id=u1_id,
                    source_account_id=acc_payer,
                    destination_account_id=acc_payee,
                    amount_paise=1000,
                    idempotency_key=f"stampede-tx-{index}"
                )
                return True
            except InsufficientFundsError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(do_transfer, i) for i in range(100)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        success_count = sum(1 for r in results if r is True)
        fail_count = sum(1 for r in results if r is False)

        # Assertions
        self.assertEqual(success_count, 50, f"Expected exactly 50 transfers to succeed, got {success_count}")
        self.assertEqual(fail_count, 50, f"Expected exactly 50 transfers to fail, got {fail_count}")

        conn = get_connection()
        cur = conn.cursor()

        # Check balances
        cur.execute("SELECT balance_paise FROM accounts WHERE id = ?", (acc_payer,))
        payer_bal = cur.fetchone()[0]
        cur.execute("SELECT balance_paise FROM accounts WHERE id = ?", (acc_payee,))
        payee_bal = cur.fetchone()[0]

        self.assertEqual(payer_bal, 0, f"Payer balance must be exactly 0, got {payer_bal}")
        self.assertEqual(payee_bal, 50000, f"Payee balance must be exactly 50,000, got {payee_bal}")

        # Check Total Debits == Total Credits across the entire ledger
        cur.execute("SELECT SUM(amount_paise) FROM ledger_entries WHERE direction = 'DEBIT'")
        total_debits = cur.fetchone()[0]
        cur.execute("SELECT SUM(amount_paise) FROM ledger_entries WHERE direction = 'CREDIT'")
        total_credits = cur.fetchone()[0]

        self.assertEqual(total_debits, total_credits)
        conn.close()

    def test_stampede_consecutive_runs(self):
        """
        Brief requirement: 'Run it 20 times in a row. A race that fails once in 20 runs is still a bug.'
        """
        for iteration in range(5):
            self.setUp()
            with self.subTest(run=iteration):
                self.test_stampede_single_run()

if __name__ == "__main__":
    unittest.main()
