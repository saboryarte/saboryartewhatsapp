# Especia en el VPS de Hostinger

Especia corre en el VPS de Hostinger (Ubuntu 24.04), en `https://especia.mariomora.tech`, junto a otros sitios del
mismo servidor sin tocarlos.

| Qué | Dónde |
|---|---|
| Código (clon de `main`) | `/opt/especia`, del usuario de sistema `especia` |
| Llaves | `/opt/especia/.env` (permisos 600, solo lo lee `especia`) |
| Datos de clientes, pedidos, comprobantes, respaldos | `/opt/especia/data/` (permisos 700) |
| Servicio | `especia.service` (systemd): uvicorn en `127.0.0.1:8010`, un solo proceso |
| Sitio web | `/etc/nginx/sites-available/especia.conf`, https de Let's Encrypt (se renueva solo) |
| Tareas programadas | crontab del usuario `especia` (`deploy/crontab`): respaldo 3:00 a. m., Excel 7:00 p. m. |
| Registros | `journalctl -u especia` y `/opt/especia/data/logs/` |

Todo se instala con [`deploy/instalar.sh`](../../deploy/instalar.sh). El dominio se cambia con la variable
`DOMINIO` (por defecto `especia.mariomora.tech`).

## Instalar desde cero

Requisitos: un registro **A** del dominio apuntando a la IP del VPS, y un usuario con `sudo`.

```bash
# En el VPS
git clone https://github.com/saboryarte/saboryartewhatsapp.git /tmp/especia-instalador
sudo bash /tmp/especia-instalador/deploy/instalar.sh preparar
sudo bash /tmp/especia-instalador/deploy/instalar.sh codigo
```

Las llaves no pasan por ningún chat: desde el computador que tiene el `.env`,

```bash
scp .env <usuario>@<vps>:/tmp/especia.env
```

y en el VPS:

```bash
sudo bash /opt/especia/deploy/instalar.sh env /tmp/especia.env   # lo instala, borra la copia y fija PUBLIC_BASE_URL
sudo bash /opt/especia/deploy/instalar.sh servicio
sudo bash /opt/especia/deploy/instalar.sh https
```

Después, en los paneles:

1. **Meta** (developers.facebook.com → WhatsApp → Configuration → Webhook): `https://especia.mariomora.tech/webhook`
   con el mismo `VERIFY_TOKEN`. Facebook Login for Business → Settings: agregar el dominio en los permitidos.
2. **Bold**: el aviso de pagos a `https://especia.mariomora.tech/webhooks/bold`.
3. **Política de privacidad**: `https://especia.mariomora.tech/privacidad` (en la app de Meta → Configuración → Básica).

## Actualizar a la última versión

Después de cada `git push` a `main` (por ejemplo, un cambio de precios):

```bash
sudo bash /opt/especia/deploy/instalar.sh actualizar
```

Baja `main`, instala dependencias, **corre las pruebas** (si fallan, no reinicia) y reinicia el bot. Los clientes no
pierden mensajes: Meta reintenta los avisos que lleguen durante los segundos del reinicio.

## Comandos útiles

```bash
sudo systemctl status especia                    # ¿está corriendo?
sudo journalctl -u especia -f                    # registro en vivo
sudo systemctl restart especia                   # reiniciar
cd /opt/especia && sudo -u especia .venv/bin/python -m app.conexion estado   # número y llave en uso
sudo -u especia nano /opt/especia/.env && sudo systemctl restart especia     # cambiar una llave
```

Las tareas de la línea de comandos (`app.conexion`, `app.transferencias`, `app.exporte`, `app.respaldo`) se corren
siempre como `especia` y desde `/opt/especia`, para que usen su `.env` y su carpeta `data/`.
