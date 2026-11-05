# Historial y copias de seguridad de Especia (servidor de Hostinger)

## Qué se guarda y cuánto tiempo

| Qué | Dónde | Cuánto dura | Para qué |
|---|---|---|---|
| Conversación activa (paso del menú, carrito, últimos 20 mensajes para Claude) | `conversaciones` | 24 h sin actividad | Que Especia siga el hilo. Después de 24 h empieza de cero, para no arrastrar un pedido viejo. |
| Archivo de chats (todo lo que escriben el cliente, Especia y el equipo) | `chat` | **60 días** | Revisar conversaciones y atender reclamos. |
| Memoria del cliente (nombre, últimos datos de envío, último pedido) | `clientes` | 60 días sin escribir, o **1 año** desde su último pago | Ofrecerle «¿a los mismos datos de la vez pasada?» y que Claude sepa que ya compró. |
| Chat completo + ficha de cada pedido **pagado** | `chats_pedidos` y `data/pedidos/*.txt` | **1 año** | Reclamos, cambios y contabilidad. |
| Pedidos (productos, total, dirección, pago) | `pedidos` | Sin vencer | Registro de ventas (ver «Pendiente» abajo). |
| Comprobantes de transferencia (foto o PDF) | `data/pedidos/Comprobantes/` | **1 año** | Soporte del pago. También van adjuntos en el correo al equipo. |
| Exporte diario de pedidos (Excel) | `data/pedidos/Exportes/` | **1 año** | Lo envía `app/exporte.py` por correo a las 7:00 p. m. |

El borrado de lo vencido lo hace el bot solo, una vez al día (`app/archivo.py`).

## Las copias de seguridad

Hay dos niveles, para no depender de un solo proveedor:

1. **Copia diaria a Google Drive** (`app/respaldo.py`, a las 3:00 a. m.):
   - La base de datos completa, **cifrada**, en `Saboryarte/Respaldos`. Se guardan **60 días**. En el servidor
     quedan solo las de los últimos 7.
   - Un `.txt` legible por cada pedido pagado (ficha + chat), los comprobantes de transferencia y el Excel
     diario, en `Saboryarte/Pedidos`, compartida solo con el equipo. Se guarda **1 año**. Se lee desde el celular con la app de Drive.
   - Antes de subirla, cada copia se abre y se verifica. Si algo falla, llega un correo al equipo.
2. **Copia semanal de Hostinger** de todo el servidor: en hPanel → VPS → **Copias de seguridad**, confirma que
   estén activas. Sirven si se daña el servidor completo.

## Configuración en el servidor (una sola vez)

### 1. Instalar rclone

```bash
sudo -v ; curl https://rclone.org/install.sh | sudo bash
```

### 2. Conectar Google Drive

Usa la cuenta de Google de Saboryarte (no una personal). El servidor no tiene navegador, así que la autorización se
hace desde tu computador: rclone te indica el comando exacto.

```bash
rclone config
```

- `n` (nuevo remoto) → nombre: `drive` → tipo: `drive` → `client_id` y `client_secret` en blanco →
  `scope`: `1` (acceso completo) → `root_folder_id` en blanco → `service_account_file` en blanco →
  «Edit advanced config?» `n` → «Use web browser?» **`n`** → sigue las instrucciones para autorizar desde tu
  computador → «Configure this as a Shared Drive?» `n` → `y` para guardar.

### 3. Crear el remoto cifrado

```bash
rclone config
```

- `n` → nombre: `drive-cifrado` → tipo: `crypt` → `remote`: `drive:Saboryarte/Respaldos` →
  `filename_encryption`: `standard` → `directory_name_encryption`: `true` →
  contraseña: `g` (generar) → **guarda la contraseña y la «salt» en el gestor de contraseñas del equipo** →
  `y` para guardar.

> ⚠️ Sin esa contraseña las copias **no se pueden abrir**. Si se pierde el servidor, es lo único que permite
> recuperar los datos: guárdala fuera del servidor.

### 4. Variables en el `.env` del servidor

```
RESPALDO_REMOTO=drive-cifrado:
PEDIDOS_REMOTO=drive:Saboryarte/Pedidos
```

Comparte la carpeta `Saboryarte/Pedidos` de Drive **solo** con las personas del equipo que atienden pedidos.

### 5. Programar la copia diaria

```bash
crontab -e
```

Agrega esta línea (ajusta la ruta a donde quedó el proyecto):

```
0 3 * * * cd /opt/especia && .venv/bin/python -m app.respaldo diario >> data/respaldo.log 2>&1
```

Y esta otra para el exporte diario de pedidos (Excel por correo y en Drive):

```
0 19 * * * cd /opt/chatsabor && .venv/bin/python -m app.exporte diario >> data/exporte.log 2>&1
```

> La hora del cron es la del servidor. Si el VPS no está en hora de Colombia, cámbiala con
> `sudo timedatectl set-timezone America/Bogota`. Las fechas del Excel siempre se calculan en hora de Colombia.

### 6. Probar

```bash
.venv/bin/python -m app.respaldo diario
rclone ls drive-cifrado:          # debe aparecer la copia de hoy
rclone ls drive:Saboryarte/Pedidos
```

## Restaurar una copia

1. Detén el bot.
2. Baja la copia que necesitas:
   `rclone copy drive-cifrado:saboryarte_2026-11-04_0300.db data/restaurar/`
3. Verifícala: `.venv/bin/python -m app.respaldo probar data/restaurar/saboryarte_2026-11-04_0300.db`
4. Guarda la base actual por si acaso (`mv data/conversaciones.db data/conversaciones.antes.db`) y pon la copia en su
   lugar (`mv data/restaurar/saboryarte_2026-11-04_0300.db data/conversaciones.db`).
5. Arranca el bot.

**Haz una restauración de prueba una vez al mes** (en otra carpeta, sin reemplazar la base): una copia que nunca se
probó no es una copia.

## Datos personales (Ley 1581 de 2012)

Los chats y las fichas tienen nombres, direcciones y teléfonos de clientes. Además de los plazos de arriba:

- Saboryarte debe tener una **política de tratamiento de datos** publicada y avisarle al cliente que la
  conversación se guarda. Lo usual es un enlace en el primer mensaje del bot o en el perfil de WhatsApp Business.
- Si un cliente pide que se borren sus datos, hay que borrarlo de la base y de la carpeta `Pedidos` de Drive.

## Pendiente

- **Plazo de los pedidos:** la tabla `pedidos` no vence porque es el registro de ventas. Confírmalo con la contadora
  (en Colombia los soportes contables suelen guardarse varios años) para fijar el plazo.
