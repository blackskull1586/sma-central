#!/bin/sh
set -eu
python manage.py migrate --noinput
python manage.py bootstrap_sma
exec gunicorn --no-control-socket sma.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 30 --worker-tmp-dir /tmp --access-logfile /dev/null --error-logfile -
