-- ============================================================================
-- RETAIL AI AGENTS — SUPABASE / POSTGRESQL SCHEMA MIGRATION (EPIC A: M1)
-- Covers Tasks A1, A2, A3, A4, A8, A10 + Exact CSV Import Compatibility
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. CORE MASTER TABLES (Matches Your Generated CSVs + Task A1)
-- ----------------------------------------------------------------------------

-- Branches Table (Task A1)
CREATE TABLE IF NOT EXISTS public.branches (
    branch_id BIGINT PRIMARY KEY,
    branch_name TEXT NOT NULL UNIQUE,
    city TEXT DEFAULT 'Cairo',
    timezone TEXT NOT NULL DEFAULT 'Africa/Cairo',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the 3 branches from your CSVs immediately so Foreign Keys succeed on import
INSERT INTO public.branches (branch_id, branch_name, city, timezone)
VALUES
    (1, 'Maadi Branch', 'Cairo', 'Africa/Cairo'),
    (2, 'Nasr City Branch', 'Cairo', 'Africa/Cairo'),
    (3, '6th of October Branch', 'Giza', 'Africa/Cairo')
ON CONFLICT (branch_id) DO NOTHING;

-- Suppliers Table (Matches suppliers (1).csv + Epic B RetrieveSuppliersNode)
CREATE TABLE IF NOT EXISTS public.suppliers (
    supplier_id BIGINT PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    product_category TEXT NOT NULL,
    supplier_location TEXT,
    contact_name TEXT,
    contact_phone TEXT,
    shipping_duration_days INTEGER NOT NULL CHECK (shipping_duration_days > 0),
    minimum_order_quantity INTEGER NOT NULL DEFAULT 1 CHECK (minimum_order_quantity > 0),
    email TEXT DEFAULT 'orders@supplier-demo.eg',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_suppliers_category ON public.suppliers(product_category);

-- Products Table (Matches products_*.csv headers exactly + Task A1)
-- Note: (branch_id, product_id) is the Composite Primary Key because product_id (1..500)
-- is repeated across each branch with branch-specific stock_quantity & minimum_stock_level.
CREATE TABLE IF NOT EXISTS public.products (
    product_id BIGINT NOT NULL,
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT,
    brand TEXT,
    production_date DATE NOT NULL,
    expiration_date DATE NOT NULL,
    unit_price NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    cost_price NUMERIC(12, 2) NOT NULL CHECK (cost_price >= 0),
    supplier_id BIGINT NOT NULL REFERENCES public.suppliers(supplier_id),
    supplier_name TEXT,
    stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    minimum_stock_level INTEGER NOT NULL DEFAULT 0 CHECK (minimum_stock_level >= 0),
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    branch_name TEXT,
    demand_level TEXT NOT NULL DEFAULT 'Medium' CHECK (demand_level IN ('Low', 'Medium', 'High')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (branch_id, product_id),
    UNIQUE (branch_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_products_branch_expiry ON public.products(branch_id, expiration_date);
CREATE INDEX IF NOT EXISTS idx_products_branch_category ON public.products(branch_id, category);
CREATE INDEX IF NOT EXISTS idx_products_branch_stock ON public.products(branch_id, stock_quantity);

-- Product Batches with Expiry (Task A1 requirement for batch-level tracking)
CREATE TABLE IF NOT EXISTS public.product_batches (
    batch_id BIGSERIAL PRIMARY KEY,
    batch_number TEXT NOT NULL UNIQUE,
    branch_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    production_date DATE NOT NULL,
    expiration_date DATE NOT NULL,
    stock_quantity INTEGER NOT NULL CHECK (stock_quantity >= 0),
    cost_price NUMERIC(12, 2) NOT NULL CHECK (cost_price >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batches_branch_expiry ON public.product_batches(branch_id, expiration_date);

-- ----------------------------------------------------------------------------
-- 2. TRANSACTIONS, MOVEMENTS & RETURNS (Matches sales_transactions_*.csv + Task A2)
-- ----------------------------------------------------------------------------

-- Sales Transactions / Sales Lines (Matches sales_transactions_*.csv headers)
-- Note: transaction_id is a receipt ID that can have multiple rows, so sale_line_id is the PK.
CREATE TABLE IF NOT EXISTS public.sales_transactions (
    sale_line_id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL,
    date DATE NOT NULL,
    time TIME NOT NULL,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    product_id BIGINT NOT NULL,
    sku TEXT NOT NULL,
    product_name TEXT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash', 'card', 'wallet')),
    offer_id BIGINT,
    discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_branch_date ON public.sales_transactions(branch_id, date);
CREATE INDEX IF NOT EXISTS idx_sales_tx_id ON public.sales_transactions(branch_id, transaction_id);
CREATE INDEX IF NOT EXISTS idx_sales_branch_product ON public.sales_transactions(branch_id, product_id);

-- Alias view `sales_lines` so M3/M4 code referencing `sales_lines` (Task A2) works seamlessly
CREATE OR REPLACE VIEW public.sales_lines AS
SELECT * FROM public.sales_transactions;

-- Inventory Movements Table (Task A2)
CREATE TABLE IF NOT EXISTS public.inventory_movements (
    movement_id BIGSERIAL PRIMARY KEY,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    product_id BIGINT NOT NULL,
    batch_id BIGINT REFERENCES public.product_batches(batch_id),
    movement_type TEXT NOT NULL CHECK (movement_type IN ('SALE', 'RESTOCK', 'RETURN', 'EXPIRY_WRITE_OFF', 'ADJUSTMENT')),
    quantity_change INTEGER NOT NULL,
    reference_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_movements_branch_time ON public.inventory_movements(branch_id, occurred_at);

-- Returns Table (Task A2)
CREATE TABLE IF NOT EXISTS public.returns (
    return_id BIGSERIAL PRIMARY KEY,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    transaction_id BIGINT NOT NULL,
    product_id BIGINT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    refund_amount NUMERIC(12, 2) NOT NULL CHECK (refund_amount >= 0),
    reason TEXT,
    returned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_returns_branch_date ON public.returns(branch_id, returned_at);

-- ----------------------------------------------------------------------------
-- 3. AGENT WORKFLOW, APPROVAL & AUDIT TABLES (Tasks A3, A4, A8, A10, A11, A12)
-- ----------------------------------------------------------------------------

-- Offers Table with Versioning (Task A3 & Epic D D1)
CREATE TABLE IF NOT EXISTS public.offers (
    offer_id BIGSERIAL PRIMARY KEY,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    product_id BIGINT NOT NULL,
    offer_type TEXT NOT NULL CHECK (offer_type IN ('DISCOUNT', 'BUY_2_GET_1', 'BUNDLE')),
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'CANDIDATE' CHECK (status IN ('CANDIDATE', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'ACTIVE', 'EXPIRED', 'STOPPED')),
    discount_pct NUMERIC(5, 2),
    promo_price NUMERIC(12, 2),
    bundle_product_id BIGINT,
    quantity_cap INTEGER NOT NULL CHECK (quantity_cap > 0),
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    evidence_ref TEXT,
    forecast_ref TEXT,
    version_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id),
    UNIQUE (offer_id, version)
);

-- Restock Orders Table (Task A3 & Epic B B13)
CREATE TABLE IF NOT EXISTS public.restock_orders (
    restock_order_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    product_id BIGINT NOT NULL,
    supplier_id BIGINT NOT NULL REFERENCES public.suppliers(supplier_id),
    requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
    trigger_reason TEXT NOT NULL CHECK (trigger_reason IN ('NEAR_EXPIRY', 'HIGH_DEMAND_LOW_STOCK', 'MEDIUM_DEMAND_LOW_STOCK')),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'DISPATCHED', 'ESCALATED')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    user_feedback TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    version_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (branch_id, product_id) REFERENCES public.products(branch_id, product_id)
);

-- Report Runs Table (Task A3 & Epic C C11)
CREATE TABLE IF NOT EXISTS public.report_runs (
    report_run_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    report_type TEXT NOT NULL CHECK (report_type IN ('DAILY', 'MONTHLY', 'MANAGER_OFFER_IMPACT')),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    is_partial BOOLEAN NOT NULL DEFAULT FALSE,
    cutoff_time TIMESTAMPTZ,
    snapshot_ref TEXT,
    query_version TEXT,
    kpi_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    executive_summary TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (delivery_status IN ('PENDING', 'BLOCKED_VALIDATION', 'SENT', 'DELIVERY_FAILED')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Approvals Service Table (Task A3 & Task A12)
CREATE TABLE IF NOT EXISTS public.approvals (
    approval_id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('RESTOCK_ORDER', 'OFFER')),
    entity_id BIGINT NOT NULL,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('APPROVE', 'REJECT', 'REQUEST_CHANGES')),
    reason TEXT,
    reviewed_version INTEGER NOT NULL,
    reviewed_hash TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit Log Table (Task A3 & Task A11)
CREATE TABLE IF NOT EXISTS public.audit_log (
    audit_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    branch_id BIGINT REFERENCES public.branches(branch_id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    version INTEGER,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Config Service Table (Task A4)
CREATE TABLE IF NOT EXISTS public.config_settings (
    config_key TEXT NOT NULL,
    branch_id BIGINT NOT NULL DEFAULT 0, -- 0 = global default across all branches
    config_value JSONB NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (config_key, branch_id)
);

INSERT INTO public.config_settings (config_key, branch_id, config_value)
VALUES
    ('restock_thresholds', 0, '{
      "max_retries": 3,
      "default": {"expiry_days": 7, "high_demand_qty": 25, "medium_demand_qty": 12},
      "categories": {
        "Bakery":          {"expiry_days": 2,  "high_demand_qty": 60, "medium_demand_qty": 35, "shelf_life_days": 5},
        "Dairy":           {"expiry_days": 3,  "high_demand_qty": 50, "medium_demand_qty": 30, "shelf_life_days": 14},
        "Frozen Food":     {"expiry_days": 14, "high_demand_qty": 50, "medium_demand_qty": 30, "shelf_life_days": 270},
        "Beverages":       {"expiry_days": 14, "high_demand_qty": 80, "medium_demand_qty": 45, "shelf_life_days": 300},
        "Snacks":          {"expiry_days": 10, "high_demand_qty": 40, "medium_demand_qty": 25, "shelf_life_days": 180},
        "Staples":         {"expiry_days": 15, "high_demand_qty": 60, "medium_demand_qty": 35, "shelf_life_days": 540},
        "Cleaning":        {"expiry_days": 14, "high_demand_qty": 25, "medium_demand_qty": 15, "shelf_life_days": 720},
        "Home":            {"expiry_days": 14, "high_demand_qty": 25, "medium_demand_qty": 15, "shelf_life_days": 720},
        "Personal Care":   {"expiry_days": 15, "high_demand_qty": 20, "medium_demand_qty": 12, "shelf_life_days": 720},
        "School Supplies": {"expiry_days": 21, "high_demand_qty": 15, "medium_demand_qty": 8,  "shelf_life_days": 1095}
      },
      "subcategories": {
        "Frozen Chicken":  {"expiry_days": 10, "high_demand_qty": 50, "medium_demand_qty": 30, "unit": "kg_or_pack"},
        "Milk":            {"expiry_days": 3,  "high_demand_qty": 60, "medium_demand_qty": 35, "unit": "pack"},
        "Bread":           {"expiry_days": 2,  "high_demand_qty": 70, "medium_demand_qty": 40, "unit": "pack"}
      }
    }'::jsonb),
    ('schedules', 0, '{"restock_cron": "0 0 * * *", "daily_report_cron": "0 1 * * *", "timezone": "Africa/Cairo"}'::jsonb),
    ('offer_guardrails', 0, '{"max_discount_pct": 40.0, "min_margin_pct": 5.0}'::jsonb),
    ('authorized_recipients', 1, '{"restock_approver": "stock.maadi@supermarket.eg", "report_recipient": "manager.maadi@supermarket.eg"}'::jsonb),
    ('authorized_recipients', 2, '{"restock_approver": "stock.nasrcity@supermarket.eg", "report_recipient": "manager.nasrcity@supermarket.eg"}'::jsonb),
    ('authorized_recipients', 3, '{"restock_approver": "stock.october@supermarket.eg", "report_recipient": "manager.october@supermarket.eg"}'::jsonb)
ON CONFLICT (config_key, branch_id) DO NOTHING;

-- Idempotency Key Store & Transactional Outbox (Tasks A8 & A10)
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    branch_id BIGINT,
    response_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.outbox_messages (
    message_id BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    branch_id BIGINT NOT NULL REFERENCES public.branches(branch_id),
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'POS_WEBHOOK', 'EVENT_BUS')),
    recipient TEXT NOT NULL,
    subject TEXT,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'DISPATCHED', 'FAILED')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ
);

COMMIT;
