# Payments-Grade Wallet Ledger

A mission-critical, double-entry financial ledger backend built to the exacting standards of modern real-time payments networks (e.g., UPI, IMPS, FedNow). Money here can never appear from nowhere, disappear, or be moved twice.

---

## 🎯 Architecture & Invariants

1. **Storage Unit: Whole Paise (`BIGINT`)**:
   - Stored strictly as positive integers in paise ($1\text{ INR} = 100\text{ paise}$).
   - Floating-point types (`FLOAT`, `DOUBLE`, `REAL`) are prohibited to eliminate binary floating-point rounding errors.
2. **Double-Entry Principle**:
   - Every financial transfer writes two or more ledger entries.
   - For every transfer: $\sum \text{Debits} = \sum \text{Credits}$.
   - Ledger entries are strictly append-only and immutable. Corrections are made via explicit reversal transfers.
3. **Deadlock-Free Deterministic Row Locking**:
   - Account rows are locked via `SELECT ... FOR UPDATE` in strictly ascending `account_id` order.
   - Prevents circular lock waits under concurrent opposing transfers.
4. **Zero-Floor Invariant**:
   - User wallet balances can never drop below zero (`CHECK (account_type != 'USER_WALLET' OR balance_paise >= 0)`).
   - System accounts (`CASH_IN`, `CASH_OUT`, `FEE_REVENUE`) may carry negative balances.
5. **Exact-Once Execution (Idempotency)**:
   - Mandatory `Idempotency-Key` UUID header on every `POST`.
   - Database uniqueness constraint on `(user_id, idempotency_key)`.
   - Request body SHA-256 hash comparison; payload modifications on the same key are rejected with `HTTP 422`.
6. **Nightly Invariant Reconciliation**:
   - Automated audit job running in `REPEATABLE READ` snapshot isolation.
   - Runs 5 SQL invariant checks; detects discrepancies without mutating history.
7. **Rule-Based Fraud Gates & Natural Language Statement Q&A**:
   - In-transaction risk threshold evaluation holds suspicious transfers before funds move.
   - 4-step sanitized Q&A engine translates natural language questions into safe, parameterized SQL with built-in prompt-injection defense.

---

## 🏛️ System Design & Milestone Questions Answered

### 1. Balance Strategy: Store, Compute, or Both?
- **Decision: Both.**
- **Cost of Compute-Only**: $O(N)$ query overhead across historical ledger lines. For accounts with tens of thousands of transactions, running `SUM(...)` on every payment pre-flight creates severe database I/O bottlenecks and high lock times.
- **Cost of Store-Only**: Vulnerable to silent corruption; zero forensic auditability.
- **Our Balance**: We maintain a cached `balance_paise` column updated within the atomic transfer transaction, providing $O(1)$ reads for balance checks and transfer validations. The append-only `ledger_entries` table remains the immutable source of truth, audited nightly by reconciliation queries.

### 2. Why Store User Wallets and System Accounts in the Same Table?
- **Symmetric Double-Entry Processing**: In double-entry accounting, money movements (top-up, withdrawal, peer transfer, fee) are transfers between two account containers.
  - *Top-up*: `CASH_IN` $\to$ `USER_WALLET`
  - *Withdrawal*: `USER_WALLET` $\to$ `CASH_OUT`
  - *Peer Transfer*: `USER_WALLET_A` $\to$ `USER_WALLET_B`
- Housing all accounts in a single table enables uniform foreign keys, identical locking semantics (`SELECT ... FOR UPDATE`), and eliminates polymorphic queries or bifurcated transfer code.

### 3. Isolation Level: Why Read Committed + Row Locks?
- **Our Selection**: `READ COMMITTED` with deterministic ascending `SELECT ... FOR UPDATE`.
- **Why Not Serializable?**: Under Serializable Snapshot Isolation (SSI), concurrent writes to the same account trigger serialization failures (`SQLSTATE 40001: could not serialize access`). Under heavy concurrency (e.g. 100 requests hitting one account simultaneously), over 80% of transactions abort, requiring aggressive retry loops that degrade throughput and latency.
- **Why Row Locks are Superior**: By acquiring exclusive row locks in ascending account ID order, transactions queue cooperatively at the row level without deadlock, achieving 100% deterministic success without retry storms.

### 4. Reconciliation Job Performance & Query Plans (1M Ledger Entries)
- **Full Table Scans Permissible**: Check 1 ($\sum \text{Debits} = \sum \text{Credits}$) runs as a nightly batch job when traffic is lowest; a sequential scan of `ledger_entries` is efficient due to sequential page reads and parallelism.
- **Indexes Required**:
  - Check 2 (Transfer balancing): Requires B-tree index on `ledger_entries(transfer_id)`.
  - Check 3 (Stored vs Computed): Requires composite index on `ledger_entries(account_id, direction, amount_paise)`.

### 5. Pagination: OFFSET vs Cursor-Based
- **Offset-based (`OFFSET 100000`)**: The database engine must scan and discard 100,000 index entries before reading the requested page, producing high buffer reads and latency.
- **Cursor-based (`WHERE (created_at, id) < ($last_time, $last_id) ORDER BY created_at DESC, id DESC LIMIT 50`)**: Jumps directly to the target B-Tree leaf node in $O(\log N)$ time, yielding constant-time pagination regardless of ledger depth.

---

## 🚦 Acceptance Test Verification

All required acceptance tests pass reliably:

```bash
python -m unittest discover tests
```

- **Stampede Test (`test_stampede.py`)**: 100 concurrent ₹10 transfers from a ₹500 wallet. Exactly 50 transfers succeed, exactly 50 fail with `INSUFFICIENT_FUNDS`, ending balance is 0 paise, total debits equal total credits. Passes **20 consecutive runs in a row**.
- **Idempotency Test (`test_idempotency.py`)**: 5 sequential retries produce 1 transfer; 10 parallel requests produce 1 transfer; altered amount payload on same key returns `HTTP 422`.
- **Reconciliation Test (`test_reconciliation.py`)**: Data directly corrupted (amount modified, credit deleted, stored balance altered) — reconciliation job reports exactly those 3 problems.
- **Fraud & Prompt-Injection Defense (`test_fraud_and_qa.py`)**: Large transfer held without moving funds; statement Q&A handles 10 natural queries; malicious prompt injections are safely constrained.

---

## 🐳 Quick Start with Docker

```bash
docker compose up -d --build
```
- API is available at `http://localhost:8000`.
- Interactive Swagger docs at `http://localhost:8000/docs`.
