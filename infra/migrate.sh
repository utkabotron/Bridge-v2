#!/usr/bin/env bash
#
# Apply pending SQL migrations, in order, each in its own transaction.
#
# The compose file mounts infra/migrations into /docker-entrypoint-initdb.d, but Postgres
# only runs that on an empty data directory — i.e. once, ever. Every migration since has
# been applied by hand, guided by a deploy step that says "skip this if there are no new
# ones". A forgotten migration does not fail loudly: the code writes to a column that is
# not there, db.py swallows the error, and message_events quietly stops recording.
#
# Usage (from the deploy directory, on the host):
#   ./infra/migrate.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

PSQL=(docker compose exec -T postgres psql -U bridge -d bridge -v ON_ERROR_STOP=1 --quiet)

# Ledger of what has run. Created on first use; existing databases are back-filled below.
"${PSQL[@]}" <<'SQL'
create table if not exists public.schema_migrations (
  filename    text primary key,
  applied_at  timestamptz not null default now()
);
SQL

# First run against a database that predates this script: everything already in the
# migrations directory is assumed applied, since the schema is live. Detected by the
# ledger being empty while the schema exists.
already_tracked=$("${PSQL[@]}" -t -A -c "select count(*) from public.schema_migrations")
schema_exists=$("${PSQL[@]}" -t -A -c "select count(*) from information_schema.tables where table_schema='public' and table_name='chat_pairs'")

if [ "$already_tracked" = "0" ] && [ "$schema_exists" = "1" ]; then
  echo "Back-filling the ledger for an existing database (nothing will be re-run)..."
  for f in infra/migrations/*.sql; do
    "${PSQL[@]}" -c "insert into public.schema_migrations (filename) values ('$(basename "$f")') on conflict do nothing"
  done
  echo "Ledger initialised with $(ls infra/migrations/*.sql | wc -l) migration(s)."
  exit 0
fi

applied=0
for f in infra/migrations/*.sql; do
  name=$(basename "$f")
  seen=$("${PSQL[@]}" -t -A -c "select count(*) from public.schema_migrations where filename='${name}'")
  if [ "$seen" != "0" ]; then
    continue
  fi

  echo "Applying ${name}..."
  # Each file runs in one transaction: a failure leaves the schema untouched rather
  # than half-migrated, and the ledger only records what actually committed.
  {
    echo "begin;"
    cat "$f"
    echo "insert into public.schema_migrations (filename) values ('${name}');"
    echo "commit;"
  } | "${PSQL[@]}"

  echo "  ${name} applied."
  applied=$((applied + 1))
done

if [ "$applied" = "0" ]; then
  echo "Schema is up to date."
else
  echo "Applied ${applied} migration(s)."
fi
