-- Runs BEFORE apps/api/sql/init.sql (mounted as 10-init.sql), because
-- docker-entrypoint-initdb.d scripts execute in lexical order.
--
-- init.sql declares `embedding vector(1536)` columns, so the extensions have
-- to exist by the time it runs. The pgvector/pgvector image ships the
-- extension files; this only enables them inside the `nesqbot` database.
--
-- On Azure, `azure.extensions` must allow-list VECTOR at the server level
-- first - see infra/azure/main.bicep - and then this same file applies.
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- Query-plan debugging, nice to have and never worth failing a boot over.
-- CREATE EXTENSION is a utility statement, so PL/pgSQL has to EXECUTE it.
DO $$
BEGIN
  EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pg_stat_statements unavailable (%), continuing', SQLERRM;
END
$$;
