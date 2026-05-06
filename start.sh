#!/bin/sh
set -e

echo "=== AXIOM STARTING ==="
echo "PORT=$PORT"
echo "Python: $(python --version)"

echo "→ Running database migrations..."

# Bootstrap detection: prod DB schema was originally created via SQLAlchemy
# create_all, so alembic_version may not exist. If it's missing, stamp head
# (existing tables are kept; alembic just records "we are at head") so the
# subsequent upgrade is a no-op. On future deploys this branch is skipped and
# new migrations apply normally.
ALEMBIC_HAS_VERSION_TABLE=$(python -c "
import os, sys
url = os.environ.get('DATABASE_URL', '')
if not url:
    print('no_url'); sys.exit(0)
url = url.replace('postgres://', 'postgresql://').replace('+asyncpg', '+psycopg2')
try:
    import sqlalchemy
    eng = sqlalchemy.create_engine(url)
    with eng.connect() as c:
        r = c.execute(sqlalchemy.text(\"SELECT 1 FROM information_schema.tables WHERE table_name='alembic_version'\")).scalar()
    print('yes' if r else 'no')
except Exception as e:
    print(f'error:{e}')
" 2>&1)

case "$ALEMBIC_HAS_VERSION_TABLE" in
    no)
        echo "  alembic_version table missing → stamping head (schema came from create_all)"
        alembic stamp head
        ;;
    yes)
        echo "  alembic_version table present"
        ;;
    *)
        echo "  WARN: alembic_version detection inconclusive: $ALEMBIC_HAS_VERSION_TABLE"
        ;;
esac

alembic upgrade head

echo "→ Starting uvicorn..."
echo "======================"
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
