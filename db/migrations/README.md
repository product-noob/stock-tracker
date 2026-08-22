# Database migrations

`db/schema.sql` v1.2 is the fresh-install baseline for the pre-integration project.

After the first shared/integration database is created:

1. Never edit DDL that has already been applied.
2. Add forward migrations named `NNNN_short_description.sql` (for example, `0001_add_source_health.sql`).
3. Start every migration with `BEGIN;` and end with `COMMIT;` unless PostgreSQL requires a non-transactional operation.
4. Run with `psql -v ON_ERROR_STOP=1` and test against a restored production-shaped backup.
5. Insert the migration version and description into `platform.schema_migrations` in the same transaction.
6. Include data backfills and new constraints in safe stages when existing rows may violate them.
7. Treat behavior-bearing seed/reference changes as migrations.
8. Record application compatibility and any required worker deployment order in the migration header.
9. Roll back operationally by restoring/testing backups or by a separate forward corrective migration; do not rely on untested down scripts.

The repository intentionally does not include a v1.1→v1.2 migration. v1.2 changed identity, content, event and market semantics before integration, and silently guessing how to transform a live v1.1 database would be unsafe. Inventory and export any such database before adopting this baseline.
