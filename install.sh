#!/usr/bin/env bash
# install.sh — deploy the GX10 dashboard to /opt/gx10-dashboard + systemd.
#
# Idempotent: safe to re-run. Never overwrites an existing config.json or
# metrics.db (backups and live data are preserved).
#
# Usage:
#   sudo ./install.sh              # full install + enable service
#   sudo ./install.sh --no-start   # install files, don't touch systemd
#
# Requirements: Linux, python3 (>= 3.10, stdlib only), systemd (full
# install mode only). No pip packages, no node, no docker needed.
set -euo pipefail

APP_DIR="/opt/gx10-dashboard"
SERVICE_NAME="gx10-dashboard"
SERVICE_USER="dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_START=0
[[ "${1:-}" == "--no-start" ]] && NO_START=1

[[ $# -le 1 ]] || { echo "ERROR: unknown argument: ${2:-}"; exit 1; }
[[ $EUID -eq 0 ]] || { echo "ERROR: run with sudo (needs /opt and systemd writes)"; exit 1; }
[[ -f "${SRC_DIR}/dashboard.py" ]] || { echo "ERROR: run from a checkout containing dashboard.py"; exit 1; }

command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || { echo "ERROR: python3 >= 3.10 required (found ${PYV})"; exit 1; }

# Dedicated system user. Created here (not shipped by anyone else).
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "==> Creating system user '${SERVICE_USER}'"
    useradd --system --home-dir "${APP_DIR}" --no-create-home \
        --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Installing to ${APP_DIR}"
mkdir -p "${APP_DIR}/logs"

# Runtime files: copy only if missing — never clobber live config/DB/backups.
for f in config.json metrics.db; do
    if [[ -f "${APP_DIR}/${f}" ]]; then
        echo "    keeping existing ${f}"
    elif [[ -f "${SRC_DIR}/${f}" ]]; then
        install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
        echo "    installed ${f}"
    fi
done

# Code + UI: always refreshed from the checkout.
CODE_FILES=(dashboard.py metadb.py engines.py engines_write.py catalog.py
            promparse.py metrics.html engines.html settings.html setup.html
            favicon.ico favicon.png)
for f in "${CODE_FILES[@]}"; do
    if [[ -f "${SRC_DIR}/${f}" ]]; then
        install -m 0644 "${SRC_DIR}/${f}" "${APP_DIR}/${f}"
    else
        echo "    WARNING: ${f} missing from checkout, skipping"
    fi
done

[[ -d "${SRC_DIR}/examples" ]] && cp -rn "${SRC_DIR}/examples" "${APP_DIR}/" 2>/dev/null || true
[[ -d "${SRC_DIR}/recipes"  ]] && cp -rn "${SRC_DIR}/recipes"  "${APP_DIR}/" 2>/dev/null || true

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

echo "==> Syntax check (as ${SERVICE_USER})"
su -s /bin/sh "${SERVICE_USER}" -c \
    "python3 -m py_compile ${APP_DIR}/dashboard.py ${APP_DIR}/metadb.py ${APP_DIR}/engines.py ${APP_DIR}/engines_write.py ${APP_DIR}/catalog.py ${APP_DIR}/promparse.py"

if [[ $NO_START -eq 1 ]]; then
    echo "==> --no-start: skipping systemd setup"
    echo "Done. Files in ${APP_DIR}. Start manually:"
    echo "    sudo -u ${SERVICE_USER} python3 ${APP_DIR}/dashboard.py"
    echo "Then open http://$(hostname):9000 — the onboarding wizard will"
    echo "scan your model dirs and inference engines and write config.json."
    exit 0
fi

command -v systemctl >/dev/null || { echo "ERROR: systemd not available (use --no-start)"; exit 1; }

echo "==> Installing systemd unit"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=GX10 metrics dashboard (:9000)
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) ${APP_DIR}/dashboard.py
Restart=on-failure
RestartSec=3
User=${SERVICE_USER}

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
    echo "No config.json yet? The onboarding wizard opens on first visit."
else
    echo "WARNING: API did not answer within 30s. Check:"
    echo "    journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi
