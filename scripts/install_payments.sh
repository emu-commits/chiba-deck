#!/usr/bin/env bash
# Build and install the mesh_cash_crypto Rust extension for the Python running chiba-deck.
# Run once after cloning mesh-cash, or after any changes to the crypto crate.
#
# Usage: bash scripts/install_payments.sh [path/to/mesh-cash]
#
# The mesh-cash path defaults to ../mesh-cash relative to this repo, or to
# the value of payments.mesh_cash_path in config.yaml if set.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Resolve mesh-cash path
if [[ $# -ge 1 ]]; then
    MC_PATH="$1"
elif [[ -f "$REPO_ROOT/config.yaml" ]]; then
    MC_PATH=$(python3 -c "
import yaml, sys
with open('$REPO_ROOT/config.yaml') as f:
    d = yaml.safe_load(f)
print(d.get('payments', {}).get('mesh_cash_path', ''))
" 2>/dev/null || true)
fi
MC_PATH="${MC_PATH:-$(realpath "$REPO_ROOT/../mesh-cash" 2>/dev/null || echo "")}"

if [[ -z "$MC_PATH" || ! -d "$MC_PATH" ]]; then
    echo "Error: mesh-cash repo not found."
    echo "Usage: bash scripts/install_payments.sh /path/to/mesh-cash"
    exit 1
fi

CRYPTO_PATH="$MC_PATH/crypto"
if [[ ! -d "$CRYPTO_PATH" ]]; then
    echo "Error: $CRYPTO_PATH not found — is this the right mesh-cash repo?"
    exit 1
fi

# Ensure cargo/rustup is available
if ! command -v cargo &>/dev/null; then
    if [[ -f "$HOME/.cargo/env" ]]; then
        source "$HOME/.cargo/env"
    else
        echo "Error: cargo not found. Install Rust via https://rustup.rs/"
        exit 1
    fi
fi

# Ensure maturin is available
if ! command -v maturin &>/dev/null; then
    echo "Installing maturin..."
    pip install --user maturin
fi

PYTHON="$(command -v python3)"
echo "Building mesh_cash_crypto for $PYTHON..."

WHEEL_DIR="$(mktemp -d)"
trap "rm -rf $WHEEL_DIR" EXIT

maturin build --features pyo3-bindings --out "$WHEEL_DIR" --manifest-path "$CRYPTO_PATH/Cargo.toml" 2>&1

WHEEL=$(ls "$WHEEL_DIR"/*.whl 2>/dev/null | head -1)
if [[ -z "$WHEEL" ]]; then
    echo "Error: no wheel produced by maturin build"
    exit 1
fi

echo "Installing $WHEEL..."
"$PYTHON" -m pip install --user --break-system-packages --force-reinstall "$WHEEL"

echo ""
echo "mesh_cash_crypto installed. Verify with:"
echo "  python3 -c \"import mesh_cash_crypto; print('OK')\""
