#!/usr/bin/env bash
# Instalación y actualización de Especia en el VPS (Ubuntu 24.04). Guía: docs/despliegue/servidor.md
#
#   sudo bash deploy/instalar.sh preparar           paquetes, usuario «especia» y carpetas
#   sudo bash deploy/instalar.sh codigo             descarga el código (o lo actualiza), entorno y pruebas
#   sudo bash deploy/instalar.sh env /tmp/x.env     instala el .env (copiado antes con scp) y borra la copia
#   sudo bash deploy/instalar.sh servicio           servicio systemd, nginx y tareas programadas
#   sudo bash deploy/instalar.sh https              certificado de Let's Encrypt para el dominio
#   sudo bash deploy/instalar.sh actualizar         baja la última versión de main, corre las pruebas y reinicia
#
# No toca otros sitios de nginx: agrega solo sites-available/especia.conf y comprueba con «nginx -t» antes de recargar.
set -euo pipefail

DOMINIO="${DOMINIO:-especia.mariomora.tech}"
REPO="${REPO:-https://github.com/saboryarte/saboryartewhatsapp.git}"
DIR=/opt/especia
USUARIO=especia

como_especia() { sudo -u "$USUARIO" -H "$@"; }

preparar() {
  apt-get update -qq
  apt-get install -y -qq python3.12-venv rclone
  if ! id "$USUARIO" >/dev/null 2>&1; then
    useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$USUARIO"
  fi
  install -d -o "$USUARIO" -g "$USUARIO" -m 755 "$DIR"
  echo "Listo: usuario $USUARIO y carpeta $DIR."
}

codigo() {
  if [ -d "$DIR/.git" ]; then
    como_especia git -C "$DIR" pull --ff-only
  else
    como_especia git clone -q "$REPO" "$DIR"   # la carpeta existe y está vacía (la crea «preparar»)
  fi
  install -d -o "$USUARIO" -g "$USUARIO" -m 700 "$DIR/data" "$DIR/data/logs"
  [ -d "$DIR/.venv" ] || como_especia python3 -m venv "$DIR/.venv"
  como_especia "$DIR/.venv/bin/pip" install -q --upgrade pip
  como_especia "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
  (cd "$DIR" && como_especia .venv/bin/python -m pytest -q -p no:cacheprovider) | tail -3
  echo "Código en $(como_especia git -C "$DIR" log --oneline -1)."
}

instalar_env() {
  local origen="${1:?Falta la ruta del .env copiado, p. ej. /tmp/especia.env}"
  install -o "$USUARIO" -g "$USUARIO" -m 600 "$origen" "$DIR/.env"
  shred -u "$origen" 2>/dev/null || rm -f "$origen"
  # La dirección pública es la del servidor (en el computador era la de ngrok).
  if grep -q '^PUBLIC_BASE_URL=' "$DIR/.env"; then
    sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMINIO|" "$DIR/.env"
  else
    echo "PUBLIC_BASE_URL=https://$DOMINIO" >> "$DIR/.env"
  fi
  echo ".env instalado en $DIR/.env (solo lo lee $USUARIO). Valores sin mostrar:"
  grep -oE '^[A-Z_]+=' "$DIR/.env" | tr -d = | while read -r k; do
    v=$(grep -E "^$k=" "$DIR/.env" | head -1 | cut -d= -f2-)
    printf '  %-28s %s\n' "$k" "$([ -n "$v" ] && echo lleno || echo VACÍO)"
  done
}

servicio() {
  install -m 644 "$DIR/deploy/especia.service" /etc/systemd/system/especia.service
  systemctl daemon-reload
  systemctl enable --now especia
  sed "s/server_name .*;/server_name $DOMINIO;/" "$DIR/deploy/nginx-especia.conf" > /etc/nginx/sites-available/especia.conf.nuevo
  # Si certbot ya agregó https, no se pisa su versión.
  if [ -f /etc/nginx/sites-available/especia.conf ] && grep -q "managed by Certbot" /etc/nginx/sites-available/especia.conf; then
    rm /etc/nginx/sites-available/especia.conf.nuevo
  else
    mv /etc/nginx/sites-available/especia.conf.nuevo /etc/nginx/sites-available/especia.conf
  fi
  ln -sf /etc/nginx/sites-available/especia.conf /etc/nginx/sites-enabled/especia.conf
  nginx -t
  systemctl reload nginx
  como_especia crontab "$DIR/deploy/crontab"
  sleep 2
  systemctl --no-pager --lines=0 status especia | head -3 || true
}

https_() {
  certbot --nginx -d "$DOMINIO" --non-interactive --redirect
  nginx -t && systemctl reload nginx
}

actualizar() {
  codigo
  systemctl restart especia
  sleep 2
  systemctl --no-pager --lines=0 status especia | head -3 || true
}

case "${1:-}" in
  preparar) preparar ;;
  codigo) codigo ;;
  env) instalar_env "${2:-}" ;;
  servicio) servicio ;;
  https) https_ ;;
  actualizar) actualizar ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
