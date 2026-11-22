-- Payments-Grade Wallet Ledger: Initial Database Schema (PostgreSQL 15+)
-- Storage Unit: Whole Paise (BIGINT). 1 INR = 100 Paise. No floating point.

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    phone VARCHAR(30) NOT NULL UNIQUE,
    kyc_status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (kyc_status IN ('PENDING', 'VERIFIED', 'REJECTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE RESTRICT,
    account_type VARCHAR(30) NOT NULL CHECK (account_type IN ('USER_WALLET', 'CASH_IN', 'CASH_OUT', 'FEE_REVENUE')),
    currency VARCHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance_paise BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_user_wallet_non_negative CHECK (
        account_type != 'USER_WALLET' OR balance_paise >= 0
    )
);

CREATE TABLE IF NOT EXISTS transfers (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(100),
    transfer_type VARCHAR(30) NOT NULL CHECK (transfer_type IN ('TOPUP', 'PEER_TRANSFER', 'WITHDRAWAL', 'FEE', 'REVERSAL')),
    status VARCHAR(30) NOT NULL CHECK (status IN ('PENDING', 'HELD_FOR_REVIEW', 'COMPLETED', 'FAILED', 'REVERSED')),
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    description TEXT DEFAULT '',
    original_transfer_id BIGINT REFERENCES transfers(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id) ON DELETE RESTRICT,
    account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    direction VARCHAR(6) NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    idempotency_key VARCHAR(100) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED')),
    response_code INT,
    response_body TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS risk_rules (
    id BIGSERIAL PRIMARY KEY,
    rule_name VARCHAR(100) NOT NULL UNIQUE,
    threshold_paise BIGINT,
    description TEXT,
    action VARCHAR(20) NOT NULL DEFAULT 'HOLD' CHECK (action IN ('HOLD', 'FLAG_ALLOW')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_flags (
    id BIGSERIAL PRIMARY KEY,
    transfer_id BIGINT NOT NULL REFERENCES transfers(id) ON DELETE RESTRICT,
    rule_id BIGINT REFERENCES risk_rules(id),
    decision VARCHAR(30) NOT NULL,
    llm_explanation TEXT,
    llm_label VARCHAR(30) CHECK (llm_label IN ('likely_ok', 'review', 'likely_fraud')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_locks (
    job_name VARCHAR(50) PRIMARY KEY,
    locked_at TIMESTAMPTZ,
    locked_by VARCHAR(50)
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id BIGSERIAL PRIMARY KEY,
    start_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED_CLEAN', 'COMPLETED_WITH_DISCREPANCIES', 'FAILED')),
    debits_sum BIGINT DEFAULT 0,
    credits_sum BIGINT DEFAULT 0,
    total_transfers_checked BIGINT DEFAULT 0,
    total_accounts_checked BIGINT DEFAULT 0,
    problems_count INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reconciliation_problems (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
    check_name VARCHAR(100) NOT NULL,
    entity_id BIGINT,
    expected_value TEXT,
    actual_value TEXT,
    details TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    entity_name VARCHAR(50) NOT NULL,
    entity_id BIGINT NOT NULL,
    previous_status VARCHAR(50),
    new_status VARCHAR(50) NOT NULL,
    changed_by VARCHAR(100) NOT NULL,
    change_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_account_created ON ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_transfer_id ON ledger_entries(transfer_id);
CREATE INDEX IF NOT EXISTS idx_transfers_created_at ON transfers(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_idempotency_lookup ON idempotency_keys(user_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_risk_flags_transfer_id ON risk_flags(transfer_id);

-- System Accounts Initial Seed
INSERT INTO accounts (id, account_type, currency, balance_paise) VALUES
(1, 'CASH_IN', 'INR', 0),
(2, 'CASH_OUT', 'INR', 0),
(3, 'FEE_REVENUE', 'INR', 0)
ON CONFLICT (id) DO NOTHING;

-- Default Risk Rules
INSERT INTO risk_rules (rule_name, threshold_paise, description, action) VALUES
('SINGLE_TRANSACTION_LIMIT', 5000000, 'Holds transfers exceeding INR 50,000 (5,000,000 paise)', 'HOLD'),
('VELOCITY_RAPID_FIRE', 5, 'Flags more than 5 transfers within 1 minute', 'HOLD'),
('NEW_ACCOUNT_HIGH_VALUE', 1000000, 'Holds transfers > INR 10,000 from accounts created < 24h ago', 'HOLD')
ON CONFLICT (rule_name) DO NOTHING;

-- Job Locks Seed
INSERT INTO job_locks (job_name) VALUES ('nightly_reconciliation')
ON CONFLICT (job_name) DO NOTHING;
