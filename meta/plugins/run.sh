#!/usr/bin/env bash

set -e

if [ -z "$CUTEKIT_PYTHON" ]; then
    if command -v python3.11 &> /dev/null; then
        export CUTEKIT_PYTHON="python3.11"
    else
        export CUTEKIT_PYTHON="python3"
    fi
fi

if [ ! -d "/cutekit-bootstrap/venv" ]; then
    echo "Creating virtual environment..."

    $CUTEKIT_PYTHON -m venv /cutekit-bootstrap/venv
    source /cutekit-bootstrap/venv/bin/activate
    $CUTEKIT_PYTHON -m ensurepip
    $CUTEKIT_PYTHON -m pip install git+https://github.com/cute-engineering/cutekit

    echo "Virtual environment created."
else
    source /cutekit-bootstrap/venv/bin/activate
fi

cd /cutekit-bootstrap
export PYTHONPATH=/cutekit-bootstrap
$CUTEKIT_PYTHON -m cutekit $@
