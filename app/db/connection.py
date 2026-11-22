import os
import sqlite3
import threading

# Dual-mode engine: connects to PostgreSQL if DB_TYPE == 'postgres', or SQLite with WAL mode for zero-setup execution.
DB_TYPE = os.getenv("DB_TYPE", "sqlite")
SQLITE_PATH = os.getenv("SQLITE_PATH", "wallet_ledger.db")

_lock = threading.RLock()

def get_connection():
    if DB_TYPE == "postgres":
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/wallet_db")
        )
        return conn
    else:
        conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

def init_db(conn=None):
    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        phone TEXT NOT NULL UNIQUE,
        kyc_status TEXT NOT NULL DEFAULT 'PENDING',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        account_type TEXT NOT NULL CHECK (account_type IN ('USER_WALLET', 'CASH_IN', 'CASH_OUT', 'FEE_REVENUE')),
        currency TEXT NOT NULL DEFAULT 'INR',
        balance_paise INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        CHECK (account_type != 'USER_WALLET' OR balance_paise >= 0)
    );

    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT,
        transfer_type TEXT NOT NULL CHECK (transfer_type IN ('TOPUP', 'PEER_TRANSFER', 'WITHDRAWAL', 'FEE', 'REVERSAL')),
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'HELD_FOR_REVIEW', 'COMPLETED', 'FAILED', 'REVERSED')),
        amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
        description TEXT DEFAULT '',
        original_transfer_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (original_transfer_id) REFERENCES transfers(id)
    );

    CREATE TABLE IF NOT EXISTS ledger_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transfer_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
        amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (transfer_id) REFERENCES transfers(id),
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    );

    CREATE TABLE IF NOT EXISTS idempotency_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED')),
        response_code INTEGER,
        response_body TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, idempotency_key)
    );

    CREATE TABLE IF NOT EXISTS risk_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_name TEXT NOT NULL UNIQUE,
        threshold_paise INTEGER,
        description TEXT,
        action TEXT NOT NULL DEFAULT 'HOLD',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS risk_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transfer_id INTEGER NOT NULL,
        rule_id INTEGER,
        decision TEXT NOT NULL,
        llm_explanation TEXT,
        llm_label TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (transfer_id) REFERENCES transfers(id),
        FOREIGN KEY (rule_id) REFERENCES risk_rules(id)
    );

    CREATE TABLE IF NOT EXISTS job_locks (
        job_name TEXT PRIMARY KEY,
        locked_at DATETIME,
        locked_by TEXT
    );

    CREATE TABLE IF NOT EXISTS reconciliation_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
        end_time DATETIME,
        status TEXT NOT NULL,
        debits_sum INTEGER DEFAULT 0,
        credits_sum INTEGER DEFAULT 0,
        total_transfers_checked INTEGER DEFAULT 0,
        total_accounts_checked INTEGER DEFAULT 0,
        problems_count INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS reconciliation_problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        check_name TEXT NOT NULL,
        entity_id INTEGER,
        expected_value TEXT,
        actual_value TEXT,
        details TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id)
    );

    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_name TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        previous_status TEXT,
        new_status TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        change_reason TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id);
    CREATE INDEX IF NOT EXISTS idx_ledger_account_created ON ledger_entries(account_id, created_at, id);
    CREATE INDEX IF NOT EXISTS idx_ledger_transfer_id ON ledger_entries(transfer_id);
    CREATE INDEX IF NOT EXISTS idx_transfers_created_at ON transfers(created_at);
    CREATE INDEX IF NOT EXISTS idx_idempotency_lookup ON idempotency_keys(user_id, idempotency_key);
    """)

    # Seed system accounts if not present
    cur.execute("INSERT OR IGNORE INTO accounts (id, account_type, currency, balance_paise) VALUES (1, 'CASH_IN', 'INR', 0)")
    cur.execute("INSERT OR IGNORE INTO accounts (id, account_type, currency, balance_paise) VALUES (2, 'CASH_OUT', 'INR', 0)")
    cur.execute("INSERT OR IGNORE INTO accounts (id, account_type, currency, balance_paise) VALUES (3, 'FEE_REVENUE', 'INR', 0)")

    # Seed default risk rule
    cur.execute("""
    INSERT OR IGNORE INTO risk_rules (id, rule_name, threshold_paise, description, action)
    VALUES (1, 'SINGLE_TRANSACTION_LIMIT', 5000000, 'Holds transfers exceeding INR 50,000 (5,000,000 paise)', 'HOLD')
    """)

    cur.execute("INSERT OR IGNORE INTO job_locks (job_name) VALUES ('nightly_reconciliation')")

    conn.commit()
    if close_after:
        conn.close()
