#!/usr/bin/env bash
# Inside the test container: start PostgreSQL, create the GenApp schema from
# base/cntl/db2cre.jcl, build the programs, run pytest.
set -euo pipefail

export PGOPTIONS="-c client_min_messages=warning"
export PYTHONDONTWRITEBYTECODE=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RESULTS="$ROOT/test/results"
mkdir -p "$RESULTS"

echo "== Starting PostgreSQL"
su postgres -c "/opt/pgbin/pg_ctl -D $PGDATA -l /tmp/postgres.log -o '-k /tmp -c listen_addresses=' -w start" >/dev/null

echo "== Creating Db2 schema from base/cntl/db2cre.jcl"
python3 test/tools/db2ddl.py > /tmp/schema.sql
psql -q -d postgres -c "DROP DATABASE IF EXISTS genapp" -c "CREATE DATABASE genapp" >/dev/null
psql -q -v ON_ERROR_STOP=1 -f /tmp/schema.sql >/dev/null

echo "== Building COBOL programs"
bash test/build.sh

if [[ "${1:-}" == "--setup-only" ]]; then
    exit 0
fi

echo "== Running tests"
exec python3 -m pytest test/cases -p no:cacheprovider \
    --junitxml="$RESULTS/junit.xml" -o junit_family=xunit1 "$@"
