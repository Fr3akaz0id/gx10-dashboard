#!/usr/bin/env bash
# install.sh — deploy the GX10 dashboard to /opt/gx10-dashboard + systemd.
#
# Idempotent: safe to re-run. Never overwrites an existing config.json or
# metrics.db (backups and live data are preserved).
#
# Usage:
#   sudo ./install.sh              # full install + enable service
#   sudo ./install.sh --no-start   # install files, don't touch systemd
set -euo pipefail

APP_DIR="/opt/gx10-dashboard"
SERVICE_NAME="gx10-dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_START=0
[[ "${1:-}" == "--no-start" ]] && NO_START=1

[[ $EUID -eq 0 ]] || { echo "ERROR: run with sudo (needs /opt and systemd writes)"; exit 1; }
[[ -f "${SRC_DIR}/dashboard.py" ]] || { echo "ERROR: run from a checkout containing dashboard.py"; exit 1; }

command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

echo "==> Installing to ${APP_DIR}"
mkdir -p "${APP_DIR}/logs"

# Runtime files: copy only if missing — never clobber live config/DB/backups.
RUNTIME_FILES=(config.json metrics.db)
for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "${APP_DIR}/${f}" ]]; then
        echo "    keeping existing ${f}"
    elif [[ -f "${SRC_DIR}/${f}" ]]; then
        install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
        echo "    installed ${f}"
    else
        echo "    ${f} not in source, skipping (created on first run)"
    fi
done

# Code + UI: always refreshed from the checkout.
CODE_FILES=(dashboard.py metadb.py engines.py engines_write.py catalog.py \
            promparse.py metrics.html engines.html settings.html setup.html \
            favicon.ico favicon.png)
for f in "${CODE_FILES[@]}"; do
    [[ -f "${SRC_DIR}/${f}" ]] && install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
done

# Optional dirs.
[[ -d "${SRC_DIR}/recipes" ]] && cp -rn "${SRC_DIR}/recipes" "${APP_DIR}/" 2>/dev/null || true

chown -R dashboard:dashboard "${APP_DIR}"

echo "==> Syntax check"
sudo -u dashboard python3 -m py_compile "${APP_DIR}/dashboard.py" \
    "${APP_DIR}/metadb.py" "${APP_DIR}/engines.py" \
    "${APP_DIR}/engines_write.py" "${APP_DIR}/catalog.py" "${APP_DIR}/promparse.py"

if [[ $NO_START -eq 1 ]]; then
    echo "==> --no-start: skipping systemd setup"
    echo "Done. Files in ${APP_DIR}. Start manually:"
    echo "    sudo -u dashboard python3 ${APP_DIR}/dashboard.py"
    exit 0
fi

echo "==> Installing systemd unit"
cat > "${SERVICE_FILE}" <<'EOF'
[Unit]
Description=GX10 metrics dashboard (:9000)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/gx10-dashboard/dashboard.py
Restart=on-failure
RestartSec=3
User=dashboard

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

echo "==> Health check (:9000)"
ok=0
for _ in $(seq 1 15); do
    sleep 2
    if curl -fsS localhost:9000/api/metrics >/dev/null 2>&1; then ok=1; break; fi
done
if [[ $ok -eq 1 ]]; then
    echo "OK — dashboard is live on http://$(hostname):9000"
else
    echo "WARNING: API did not answer within 30s. Check:"
    echo "    journalctl -u ${SERVICE_NAME} -n 50"
fi
