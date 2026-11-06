#!/bin/sh
set -eu
# Runs ONLY on an empty PostgreSQL volume. The superuser never reaches storage.
# Existing volumes/role password changes require explicit operator maintenance.
EAR_SERVICE_DB_PASSWORD=$(cat /run/secrets/service_db_password)
export EAR_SERVICE_DB_PASSWORD
[ -n "$EAR_SERVICE_DB_PASSWORD" ]
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" > /dev/null <<'SQL'
\getenv owner_password EAR_SERVICE_DB_PASSWORD
CREATE ROLE ear_storage LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'owner_password';
ALTER DATABASE ear OWNER TO ear_storage;
REVOKE ALL ON DATABASE ear FROM PUBLIC;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE ear TO ear_storage;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
unset EAR_SERVICE_DB_PASSWORD
