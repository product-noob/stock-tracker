-- Positive marker for a dedicated disposable stock-tracker integration-test database.
--
-- Apply only after explicitly creating a separate test database and loading the frozen
-- baseline schema there.  Never apply this file to the application database.  The test
-- guard checks the connected database name/OID as well as this identity/version row.

BEGIN;

CREATE SCHEMA IF NOT EXISTS stocktracker_test_guard;

CREATE TABLE IF NOT EXISTS stocktracker_test_guard.environment (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    marker_name text NOT NULL
        CHECK (marker_name = 'stocktracker-disposable-test-environment'),
    contract_version integer NOT NULL CHECK (contract_version = 1),
    database_name name NOT NULL,
    database_oid oid NOT NULL,
    marked_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO stocktracker_test_guard.environment
    (singleton, marker_name, contract_version, database_name, database_oid)
SELECT true,
       'stocktracker-disposable-test-environment',
       1,
       current_database(),
       oid
  FROM pg_database
 WHERE datname = current_database()
ON CONFLICT (singleton) DO UPDATE
SET marker_name = EXCLUDED.marker_name,
    contract_version = EXCLUDED.contract_version,
    database_name = EXCLUDED.database_name,
    database_oid = EXCLUDED.database_oid,
    marked_at = now();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON stocktracker_test_guard.environment FROM PUBLIC;

COMMIT;
