#!/usr/bin/env bash
# Instalare AI Translator pe dalvalem.imcu.ro (Ubuntu 22.04, Apache).
# Rulează-l ca root pe server:    sudo bash install_dalvalem.sh
# (după ce ai dezarhivat ai-translator-deploy.tar.gz în /tmp)
set -euo pipefail

APP_DIR=/opt/ai-translator
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 1. Pachete sistem"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip apache2

echo "==> 2. Module Apache"
a2enmod proxy proxy_http auth_basic authn_file authz_user headers

echo "==> 3. Cod aplicație → $APP_DIR"
mkdir -p "$APP_DIR"
cp "$SRC_DIR/translatev5.py" "$APP_DIR/translatev5.py"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chown -R www-data:www-data "$APP_DIR"

echo "==> 4. Virtualenv + dependențe"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u www-data python3 -m venv "$APP_DIR/venv"
fi
sudo -u www-data "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u www-data "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> 5. Systemd unit"
cp "$SRC_DIR/deploy/ai-translator.service" /etc/systemd/system/ai-translator.service
systemctl daemon-reload
systemctl enable --now ai-translator.service
sleep 2
systemctl status ai-translator.service --no-pager | head -15

echo "==> 6. Verifică Apache config pentru /translator"
if grep -rq "Location /translator" /etc/apache2/sites-available/ 2>/dev/null; then
    echo "    -> deja configurat în sites-available, nu suprascriu."
else
    echo "    -> nu există config /translator. Conținutul de pus în VirtualHost-ul 443:"
    echo "       ------ START ------"
    cat "$SRC_DIR/deploy/apache_translator.conf"
    echo "       ------ END ------"
    echo "    EDITEAZĂ manual /etc/apache2/sites-available/<site>.conf și adaugă <Location /translator> de mai sus."
fi

echo "==> 7. .htpasswd"
if [ -f /etc/apache2/.htpasswd_translator ]; then
    echo "    -> .htpasswd_translator există deja, nu îl ating."
else
    echo "    -> NU există /etc/apache2/.htpasswd_translator."
    echo "       Crează-l cu:  sudo htpasswd -c /etc/apache2/.htpasswd_translator <user>"
fi

echo "==> 8. Reload Apache (după ce ai editat config-ul):"
echo "       sudo apache2ctl configtest && sudo systemctl reload apache2"

echo "==> Probă locală:"
curl -s -o /dev/null -w "    http://127.0.0.1:8002/  -> HTTP %{http_code}\n" http://127.0.0.1:8002/ || true

echo "==> DONE. Verifică:  https://dalvalem.imcu.ro/translator/"
