# Deploy pe dalvalem.imcu.ro

## Ce instalează

- `/opt/ai-translator/` — cod + venv Python (rulează ca `www-data`)
- `ai-translator.service` — systemd unit, autostart la boot, port `127.0.0.1:8002`
- `BASE_PATH=/translator` setat ca env var → JS-ul UI prefixează corect toate fetch-urile
- Apache reverse proxy `/translator/` → `127.0.0.1:8002` cu Basic Auth păstrat

## Pași (de pe mașina ta)

```bash
# 1. Bundle local
cd ~/Documents/ai-translator
tar czf /tmp/ai-translator-deploy.tar.gz translatev5.py requirements.txt deploy/

# 2. Upload pe server
scp /tmp/ai-translator-deploy.tar.gz <user>@dalvalem.imcu.ro:/tmp/

# 3. SSH și rulează scriptul
ssh <user>@dalvalem.imcu.ro
sudo -i
cd /tmp && tar xzf ai-translator-deploy.tar.gz
bash deploy/install_dalvalem.sh
```

## Apache config (dacă nu există încă)

Scriptul afișează snippet-ul `deploy/apache_translator.conf`. Pune-l într-un
`<VirtualHost *:443>` (probabil în `/etc/apache2/sites-available/dalvalem.imcu.ro-le-ssl.conf`):

```apache
<Location /translator>
    AuthType Basic
    AuthName "AI Translator"
    AuthUserFile /etc/apache2/.htpasswd_translator
    Require valid-user
    ProxyPass        http://127.0.0.1:8002
    ProxyPassReverse http://127.0.0.1:8002
    SetEnv proxy-sendchunked 1
    SetEnv no-gzip 1
</Location>
```

Apoi:
```bash
sudo apache2ctl configtest
sudo systemctl reload apache2
```

## .htpasswd

Dacă nu există utilizatori încă:
```bash
sudo htpasswd -c /etc/apache2/.htpasswd_translator admin
# (te întreabă parola)
```

Pentru a adăuga utilizatori suplimentari (FĂRĂ `-c`, altfel rescrie fișierul):
```bash
sudo htpasswd /etc/apache2/.htpasswd_translator alt_user
```

## Verificare

```bash
# Local pe server:
curl -i http://127.0.0.1:8002/  # ar trebui HTTP 200 cu HTML

# Prin Apache (de oriunde):
curl -i -u admin:<parola> https://dalvalem.imcu.ro/translator/
```

## Ollama remote (RTX 5090)

Aplicația rulează pe dalvalem dar poate folosi Ollama de pe **mașina ta locală**
(RTX 5090) prin Tailscale. În UI, lasă IP-ul `100.67.104.36` (sau IP-ul tău
Tailscale) și apasă „Scanare Modele". Necesită ca `ollama serve` să asculte
pe `0.0.0.0` (`OLLAMA_HOST=0.0.0.0`) și să accepte origin-ul (`OLLAMA_ORIGINS=*`).
