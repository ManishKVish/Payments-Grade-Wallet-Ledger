import concurrent.futures
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.db.connection import init_db, get_connection

class IdempotencyAcceptanceTest(unittest.TestCase):
    def setUp(self):
        conn = get_connection()
        init_db(conn)
        cur = conn.cursor()
        cur.execute("DELETE FROM risk_flags")
        cur.execute("DELETE FROM ledger_entries")
        cur.execute("DELETE FROM transfers")
        cur.execute("DELETE FROM idempotency_keys")
        cur.execute("DELETE FROM accounts WHERE id > 3")
        cur.execute("DELETE FROM users")

        cur.execute("INSERT INTO users (name, email, phone) VALUES ('Sender', 'idemp_sender@test.com', '9988112233')")
        self.u1 = cur.lastrowid
        cur.execute("INSERT INTO users (name, email, phone) VALUES ('Receiver', 'idemp_rcv@test.com', '9988112244')")
        self.u2 = cur.lastrowid
        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 100000)", (self.u1,))
        self.acc1 = cur.lastrowid
        cur.execute("INSERT INTO accounts (user_id, account_type, balance_paise) VALUES (?, 'USER_WALLET', 0)", (self.u2,))
        self.acc2 = cur.lastrowid
        conn.commit()
        conn.close()

        self.client = TestClient(app)

    def test_replaying_same_request_5_times_sequential(self):
        """
        Brief requirement: 'The same request sent 5 times in a row creates 1 transfer and returns 5 identical responses.'
        """
        idempotency_key = "uuid-test-key-seq-001"
        payload = {
            "user_id": self.u1,
            "source_account_id": self.acc1,
            "destination_account_id": self.acc2,
            "amount_paise": 2500,
            "transfer_type": "PEER_TRANSFER",
            "description": "Lunch split"
        }

        responses = []
        for _ in range(5):
            res = self.client.post("/transfers", json=payload, headers={"Idempotency-Key": idempotency_key})
            self.assertEqual(res.status_code, 201)
            responses.append(res.json())

        first_transfer_id = responses[0]["data"]["transfer_id"]
        for r in responses:
            self.assertEqual(r["data"]["transfer_id"], first_transfer_id)
            self.assertEqual(r["data"]["status"], "COMPLETED")

        # Verify balance only deducted ONCE (2,500 paise, not 12,500 paise)
        bal_res = self.client.get(f"/accounts/{self.acc1}/balance")
        self.assertEqual(bal_res.json()["balance_paise"], 97500)

    def test_replaying_same_key_10_times_parallel(self):
        """
        Brief requirement: 'The same key sent 10 times in parallel still creates exactly 1 transfer.'
        """
        idempotency_key = "uuid-test-key-parallel-002"
        payload = {
            "user_id": self.u1,
            "source_account_id": self.acc1,
            "destination_account_id": self.acc2,
            "amount_paise": 1500,
            "transfer_type": "PEER_TRANSFER",
            "description": "Coffee"
        }

        def make_req(idx):
            return self.client.post("/transfers", json=payload, headers={"Idempotency-Key": idempotency_key})

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_req, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        success_statuses = [r.status_code for r in results if r.status_code in (200, 201)]
        self.assertEqual(len(success_statuses), 10)

        transfer_ids = set()
        for r in results:
            if r.status_code in (200, 201):
                data = r.json()
                if "data" in data and "transfer_id" in data["data"]:
                    transfer_ids.add(data["data"]["transfer_id"])

        self.assertEqual(len(transfer_ids), 1, "Must produce exactly 1 unique transfer ID")

    def test_same_key_different_amount_returns_422(self):
        """
        Brief requirement: 'The same key with a different amount returns 422, and no money moves.'
        """
        idempotency_key = "uuid-test-key-mismatch-003"
        payload1 = {
            "user_id": self.u1,
            "source_account_id": self.acc1,
            "destination_account_id": self.acc2,
            "amount_paise": 3000,
            "description": "Dinner"
        }
        res1 = self.client.post("/transfers", json=payload1, headers={"Idempotency-Key": idempotency_key})
        self.assertEqual(res1.status_code, 201)

        payload2 = dict(payload1)
        payload2["amount_paise"] = 7000 # Tampered amount

        res2 = self.client.post("/transfers", json=payload2, headers={"Idempotency-Key": idempotency_key})
        self.assertEqual(res2.status_code, 422)
        self.assertEqual(res2.json()["code"], "IDEMPOTENCY_PAYLOAD_MISMATCH")

if __name__ == "__main__":
    unittest.main()
