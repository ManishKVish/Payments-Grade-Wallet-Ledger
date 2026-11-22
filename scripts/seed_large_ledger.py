"""
Large-scale Ledger Seeder:
Generates 1,000 users, 2,000 wallets, and 100,000 balanced double-entry transfers.
Optimized for high-throughput batch transactions.
"""
import os
import random
import time
from app.db.connection import get_connection, init_db

def seed_ledger(target_users=1000, target_wallets=2000, target_transfers=100000):
    print(f"Starting seed process: {target_users} users, {target_wallets} wallets, {target_transfers} transfers...")
    start_time = time.time()

    conn = get_connection()
    init_db(conn)
    cur = conn.cursor()

    # Batch insert users
    print("Generating users...")
    user_data = [
        (f"User_{i}", f"user_{i}@ledgerdomain.com", f"+9198{i:08d}", "VERIFIED")
        for i in range(1, target_users + 1)
    ]
    cur.executemany("INSERT INTO users (name, email, phone, kyc_status) VALUES (?, ?, ?, ?)", user_data)
    conn.commit()

    # Batch insert wallets (2 wallets per user)
    print("Generating accounts...")
    account_data = []
    for u_id in range(1, target_users + 1):
        account_data.append((u_id, "USER_WALLET", "INR", 10000000)) # 100,000 INR = 10,000,000 paise
        account_data.append((u_id, "USER_WALLET", "INR", 5000000))  # 50,000 INR = 5,000,000 paise

    cur.executemany("INSERT INTO accounts (user_id, account_type, currency, balance_paise) VALUES (?, ?, ?, ?)", account_data)
    conn.commit()

    print(f"Generating {target_transfers} balanced transfers...")
    batch_size = 5000
    total_batches = target_transfers // batch_size

    min_acc_id = 4 # 1, 2, 3 are system accounts
    max_acc_id = min_acc_id + len(account_data) - 1

    for b in range(total_batches):
        transfer_rows = []
        ledger_rows = []

        for i in range(batch_size):
            t_idx = b * batch_size + i + 1
            src_acc = random.randint(min_acc_id, max_acc_id)
            dest_acc = random.randint(min_acc_id, max_acc_id)
            while dest_acc == src_acc:
                dest_acc = random.randint(min_acc_id, max_acc_id)

            amt = random.randint(100, 5000) # 1 to 50 INR in paise

            transfer_rows.append((f"seed-key-{t_idx}", "PEER_TRANSFER", "COMPLETED", amt, f"Batch seed payment #{t_idx}"))

        cur.executemany(
            "INSERT INTO transfers (idempotency_key, transfer_type, status, amount_paise, description) VALUES (?, ?, ?, ?, ?)",
            transfer_rows
        )

        cur.execute("SELECT id, amount_paise FROM transfers ORDER BY id DESC LIMIT ?", (batch_size,))
        recent_transfers = cur.fetchall()[::-1]

        for t_id, amt in recent_transfers:
            src_acc = random.randint(min_acc_id, max_acc_id)
            dest_acc = random.randint(min_acc_id, max_acc_id)
            ledger_rows.append((t_id, src_acc, "DEBIT", amt))
            ledger_rows.append((t_id, dest_acc, "CREDIT", amt))

        cur.executemany(
            "INSERT INTO ledger_entries (transfer_id, account_id, direction, amount_paise) VALUES (?, ?, ?, ?)",
            ledger_rows
        )
        conn.commit()
        print(f"  Inserted batch {b + 1}/{total_batches} ({((b + 1) * batch_size):,} transfers)...")

    duration = time.time() - start_time
    print(f"Completed seeding {target_transfers:,} transfers in {duration:.2f}s!")
    conn.close()

if __name__ == "__main__":
    # Can run with smaller default or full 100k
    seed_ledger(target_users=1000, target_wallets=2000, target_transfers=5000)
