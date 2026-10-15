#!/usr/bin/env bash
# Install or upgrade healthbot. Safe to re-run: an existing config is kept.
set -euo pipefail
cd "$(dirname "$0")"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "healthbot needs Python 3.11 or newer" >&2; exit 1; }

missing=()
python3 -c 'import matplotlib' 2>/dev/null || missing+=(python3-matplotlib)
command -v smartctl >/dev/null || missing+=(smartmontools)
command -v sensors >/dev/null || missing+=(lm-sensors)
if ((${#missing[@]})); then
    if command -v apt-get >/dev/null; then
        echo "Installing: ${missing[*]}"
        apt-get install -y "${missing[@]}"
    else
        echo "Please install: ${missing[*]} (package names may differ on your distro)" >&2
        exit 1
    fi
fi

install -m 755 healthbot.py /usr/local/bin/healthbot
install -d -m 700 /etc/healthbot /var/lib/healthbot
if [[ ! -e /etc/healthbot/config.toml ]]; then
    install -m 600 config.example.toml /etc/healthbot/config.toml
    fresh=1
fi
install -m 644 systemd/healthbot.service systemd/healthbot.timer /etc/systemd/system/
systemctl daemon-reload

if [[ -n ${fresh:-} ]]; then
    cat <<EOF

Installed. Next:
  1. Put your webhook URL in /etc/healthbot/config.toml (and list your services).
  2. Preview without posting:   sudo healthbot --dry-run
  3. Start it:                  sudo systemctl enable --now healthbot.timer
EOF
else
    systemctl is-enabled --quiet healthbot.timer && systemctl restart healthbot.timer
    echo "Upgraded; existing config at /etc/healthbot/config.toml kept."
fi
