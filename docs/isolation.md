# Milestone 2: Concurrency, Isolation Levels & Deadlock Prevention

This document records the empirical results of PostgreSQL isolation level experiments and explains how we guarantee correctness under concurrent transfer stampedes.

---

## 1. Isolation Level Lab: Side-by-Side PostgreSQL Reproduction

### Experiment 1: Read Committed (PostgreSQL Default)
**Setup**: Initial wallet balance = ₹10,000 (1,000,000 paise).

```sql
-- Session A (Tx A)
BEGIN;
SELECT balance_paise FROM accounts WHERE id = 101;
-- Output: 1000000

                  -- Session B (Tx B)
                  BEGIN;
                  UPDATE accounts SET balance_paise = balance_paise - 200000 WHERE id = 101;
                  COMMIT;

-- Session A (Tx A continues)
SELECT balance_paise FROM accounts WHERE id = 101;
-- Output: 800000! (Non-repeatable read observed)
COMMIT;
```
**Observation**: In *Read Committed*, a transaction sees updates committed by concurrent transactions between successive queries. This can lead to race conditions if application business logic checks a balance in Step 1 and writes based on that stale value in Step 2 without row-level locks.

---

### Experiment 2: Repeatable Read
**Setup**: Initial wallet balance = ₹10,000 (1,000,000 paise).

```sql
-- Session A (Tx A)
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT balance_paise FROM accounts WHERE id = 101;
-- Output: 1000000

                  -- Session B (Tx B)
                  BEGIN;
                  UPDATE accounts SET balance_paise = balance_paise - 200000 WHERE id = 101;
                  COMMIT;

-- Session A (Tx A continues)
SELECT balance_paise FROM accounts WHERE id = 101;
-- Output: 1000000 (Snapshot isolation holds; sees consistent snapshot as of Tx start)

UPDATE accounts SET balance_paise = balance_paise - 500000 WHERE id = 101;
-- ERROR: could not serialize access due to concurrent update (SQLSTATE 40001)
ROLLBACK;
```
**Observation**: *Repeatable Read* uses snapshot isolation. When Session A tries to update a row that was updated by Session B after Session A's snapshot began, PostgreSQL aborts Session A with serialization error `40001`.

---

### Experiment 3: Serializable
**Setup**: Two accounts (101 with ₹500, 102 with ₹500). Rule: sum must remain ≥ ₹500.

```sql
-- Session A (Tx A)
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SELECT SUM(balance_paise) FROM accounts WHERE id IN (101, 102); -- 1,000,000 paise

                  -- Session B (Tx B)
                  BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
                  SELECT SUM(balance_paise) FROM accounts WHERE id IN (101, 102); -- 1,000,000 paise
                  UPDATE accounts SET balance_paise = balance_paise - 400000 WHERE id = 102;
                  COMMIT;

-- Session A (Tx A continues)
UPDATE accounts SET balance_paise = balance_paise - 400000 WHERE id = 101;
COMMIT;
-- ERROR: could not serialize access due to read/write dependencies among transactions
-- DETAIL: Reason code = Canceled on identification as a pivot, during commit attempt.
```
**Observation**: PostgreSQL uses SSI (Serializable Snapshot Isolation). It detects read-write conflict cycles and aborts one transaction. While safe, Serializable incurs high serialization abort rates under heavy write contention on hot accounts, requiring extensive client retry loops.

---

### Experiment 4: Repeatable Read with a Join
```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT a.id, a.balance_paise, COUNT(l.id) as tx_count
FROM accounts a
LEFT JOIN ledger_entries l ON a.id = l.account_id
WHERE a.id = 101
GROUP BY a.id, a.balance_paise;
```
**Observation**: The repeatable read snapshot guarantee applies across joined tables. Any new ledger entries inserted and committed concurrently by other sessions do not appear in Session A's join result.

---

## 2. The Solution: Read Committed + Deterministic Row Locks (`SELECT ... FOR UPDATE`)

To maximize throughput and avoid serialization abort storms, money transfers execute in **Read Committed** with deterministic row-level locks.

### Deadlock Elimination via Ordered Row Locking
If Transfer 1 moves money from Account 10 to Account 20, and Transfer 2 moves money from Account 20 to Account 10:
- Without sorting: Tx 1 locks 10 then requests 20; Tx 2 locks 20 then requests 10. **Deadlock!**
- **Our Invariant**: Every transfer transaction locks participating accounts in strictly ascending numeric order:
```python
first_id, second_id = sorted([source_id, destination_id])

# Acquire exclusive row locks in ascending order
cursor.execute("SELECT id, balance_paise FROM accounts WHERE id = %s FOR UPDATE", [first_id])
cursor.execute("SELECT id, balance_paise FROM accounts WHERE id = %s FOR UPDATE", [second_id])
```
Because all concurrent transactions acquire locks in the identical order, circular lock wait graphs are mathematically impossible, completely eliminating deadlocks.

---

## 3. The Stampede Acceptance Test

**Scenario**:
- Seed Account A with ₹500.00 (50,000 paise).
- Fire 100 concurrent requests of ₹10.00 (1,000 paise) from Account A to Account B simultaneously.
- **Pass Criteria**:
  1. Exactly 50 transfers succeed (`HTTP 200/201`).
  2. Exactly 50 transfers are rejected (`HTTP 400 INSUFFICIENT_FUNDS`).
  3. Final balance of Account A is exactly 0 paise.
  4. Final balance of Account B has increased by exactly 50,000 paise.
  5. Total debits equal total credits across the ledger.
  6. Verified over 20 consecutive runs without a single discrepancy.
