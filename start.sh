#!/bin/sh
echo "=== AXIOM STARTING ==="
echo "PORT=$PORT"
echo "Python: $(python --version)"
echo "======================"
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
