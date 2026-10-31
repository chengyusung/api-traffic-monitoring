#!/bin/sh
set -eu

alembic upgrade head

bootstrap_admin_and_seed() {
    echo "bootstrap admin and demo seed: not implemented in this milestone, skipping"
}
bootstrap_admin_and_seed

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
