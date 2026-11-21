#!/bin/sh
set -eu
export PGPASSWORD="$(cat /run/secrets/admin)"
export SMA_APP_PASSWORD="$(cat /run/secrets/app-db)"
export SMA_REC_PASSWORD="$(cat /run/secrets/recorder-db)"
export SMA_DOMAINS_PASSWORD="$(cat /run/secrets/domains-db)"
exec psql -X -v ON_ERROR_STOP=1 <<'SQL'
\getenv app_password SMA_APP_PASSWORD
\getenv rec_password SMA_REC_PASSWORD
\getenv domains_password SMA_DOMAINS_PASSWORD
BEGIN;
SELECT format('CREATE ROLE sma_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sma_app') \gexec
SELECT format('CREATE ROLE sma_recorder LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'rec_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sma_recorder') \gexec
SELECT format('CREATE ROLE sma_domains LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'domains_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sma_domains') \gexec
ALTER SCHEMA public OWNER TO sma_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE sma TO sma_app, sma_recorder, sma_domains;
COMMIT;
SQL
