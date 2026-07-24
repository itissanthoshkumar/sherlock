-- Resolve a loan account ID into ALL its bank accounts (one row per account).
-- {{loan_id}} is bound as a parameter (never string-concatenated).
--
-- Columns are mapped in checker._map_row(). AA-enabled is computed in app code:
-- source in AA_SOURCE_VALUES (DIGITAL) AND created_date > cutoff (2026-04-14).
-- The Digitap parent transaction id is read from the column named by
-- LOOKUP_TXN_COLUMN (see .env) -- add that column to this SELECT once known.

SELECT
    a.account_id                AS loan_id,
    a.application_no            AS los_application_no,
    a.uid                       AS context_uid,
    a.repayment_bankaccount_uid AS repayment_bankaccount_uid,
    ba.uid                      AS bank_account_uid,
    ba.bank_name                AS bank_name,
    ba.branch_name              AS branch_name,
    ba.account_type             AS account_type,
    ba.account_number           AS account_number,
    ba.masked_account_number    AS masked_account_number,
    ba.source                   AS source,
    ba.account_holder_name      AS account_holder_name,
    ba.ifsc                     AS ifsc,
    ba.created_date             AS fetched_at,
    CASE WHEN ba.uid = a.repayment_bankaccount_uid THEN 'YES' ELSE 'NO' END AS is_repayment_account
    -- EMI is read from the LMS (loan-level; same value on every row). Map the
    -- real column/join here, e.g.:  , a.emi_amount AS emi_amount
    -- TODO: add the Digitap parent transaction id column here, e.g.:
    --   , ba.main_txn_id        AS main_txn_id
    -- TODO: add consent id + expiry columns, e.g.:
    --   , ba.consent_id AS consent_id , ba.consent_expiry AS consent_expiry
FROM engrowdb_v2.application a
LEFT JOIN engrowdb_v2.bank_account ba
    ON ba.context_uid = a.uid
WHERE a.account_id = {{loan_id}}
ORDER BY is_repayment_account DESC, ba.uid;
