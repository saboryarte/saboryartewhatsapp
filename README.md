# WhatsApp Claude Agent

Agente de WhatsApp construido con **Python + FastAPI** que recibe mensajes via [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api), consulta a **Claude** (Anthropic) y responde automáticamente al remitente.

## Características

- `GET /webhook` — verificación del webhook por parte de Meta
- `POST /webhook` — recibe mensajes de WhatsApp, consulta a Claude y envía la respuesta
- Integración con Claude via Anthropic SDK. **Sonnet 5.5 es el modelo más alto que usa el bot**: responde el texto libre (esfuerzo bajo, system prompt cacheado) y **Haiku 5.5** extrae los datos de pedido y envío. La elección está medida en `evals/` (ver abajo)
- Variables de entorno con validación via **pydantic-settings**
- Pruebas con **pytest**
- Menú principal como lista de WhatsApp: los saludos puros ("hola", "buenas tardes") y la palabra "menú" reciben el menú G1 con 7 opciones. Con el saludo, el texto es una bienvenida aleatoria personalizada con el nombre del perfil (`app/greetings.py`)
- Estado de conversación por remitente (SQLite, caduca a las 24 h): Claude recibe el historial reciente y se recuerda la opción elegida del menú
- Deduplicación por `message.id`: un reintento de Meta no genera una segunda respuesta
- Ruta de compra navegable con listas y botones: catálogo, ficha de cada mezcla, las más queridas, kits, recomendación por uso y dudas frecuentes
- Pedido sin escribir nombres: lista de productos con precio, cantidad con botones y pedido parcial («Agregar otra mezcla» / «Continuar»); los kits armados se arman eligiendo las mezclas de una lista. Escribir el pedido también funciona
- Toma de pedidos (P6 → P7 → P8): Claude extrae del texto las mezclas, cantidades y datos de envío; el código valida contra el catálogo y calcula subtotal, envío por ciudad y total. El pedido aún no se registra ni se pasa al equipo
- Traspaso a una persona en modo coexistencia: "asesor", "Hablar con alguien" o "Confirmar pedido" silencian el bot y envían la ficha por correo al equipo, que responde desde la app de WhatsApp Business. El bot retoma con la frase de cierre o tras horas sin mensajes del equipo, y alerta si nadie responde a tiempo
- Pago con Bold: al confirmar el pedido se guarda en SQLite y el cliente recibe un link de pago (tarjeta, PSE, Botón Bancolombia, Nequi) por el total con la tarifa estándar de envío. El webhook `POST /webhooks/bold` verifica la firma, confirma con Bold que el link quedó pagado y por el monto correcto, avisa al cliente y envía al equipo la ficha para despachar
- Claude responde con la voz de Saboryarte y solo con la base de conocimiento pública (`contenido/base_de_conocimiento.md`)

---

## Requisitos previos

- Python 3.11+
- Una app en [Meta for Developers](https://developers.facebook.com/) con WhatsApp Business configurado
- Una API Key de [Anthropic](https://console.anthropic.com/) para acceder a Claude
- Un túnel público para desarrollo local (por ejemplo, [ngrok](https://ngrok.com/))

---

## Instalación

```bash
# 1. Clona el repositorio
git clone <url-del-repo>
cd whatsapp-webhook

# 2. Crea un entorno virtual
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Instala dependencias
pip install -r requirements.txt

# 4. Copia el archivo de entorno y rellena los valores
cp .env.example .env
# Edita .env con tu editor favorito
```

---

## Variables de entorno

| Variable | Requerida | Descripción |
|---|---|---|
| `VERIFY_TOKEN` | ✅ | Token aleatorio que usarás al configurar el webhook en Meta |
| `WHATSAPP_TOKEN` | ✅ | Token de acceso de la Graph API para enviar mensajes |
| `WHATSAPP_PHONE_NUMBER_ID` | ✅ | ID del número de teléfono en Meta Developers |
| `ANTHROPIC_API_KEY` | ✅ | API Key de Anthropic para consultar a Claude |
| `APP_SECRET` | ✅ en producción | App Secret de Meta: el bot rechaza los avisos de WhatsApp sin su firma (`X-Hub-Signature-256`) |
| `META_APP_ID`, `META_SIGNUP_CONFIG_ID` | ⬜ | Para conectar el número del negocio en coexistencia (`/conectar`). Ver [docs/despliegue/numero-del-negocio.md](docs/despliegue/numero-del-negocio.md) |
| `STATE_DB_PATH` | ⬜ | Archivo SQLite del estado de conversaciones (por defecto `data/conversaciones.db`) |
| `HANDOFF_EMAIL_TO` | ⬜ | Correo del equipo que recibe la ficha de traspaso y las alertas |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | ⬜ | Servidor SMTP para enviar esos correos (Gmail: `smtp.gmail.com`, 587, contraseña de aplicación) |
| `HANDOFF_ALERT_MINUTES` | ⬜ | Minutos hábiles sin respuesta antes de alertar al equipo (por defecto 60) |
| `HANDOFF_IDLE_HOURS` | ⬜ | Horas sin mensajes del equipo para que el bot retome solo (por defecto 12) |
| `BOLD_API_KEY` | ⬜ | Llave de identidad de Bold: con ella se crean los links de pago. Sin ella, «Confirmar pedido» pasa a una persona |
| `BOLD_SECRET_KEY` | ⬜ | Llave secreta de Bold: verifica la firma de los avisos de pago (obligatoria en producción) |
| `BOLD_SANDBOX` | ⬜ | `true` solo con las llaves de pruebas de Bold |
| `BOLD_LINK_HOURS` | ⬜ | Vigencia del link de pago en horas (por defecto 24) |
| `BOLD_RETURN_URL` | ⬜ | A dónde lleva «Volver a la tienda» tras pagar; idealmente `https://wa.me/<número del bot>` |
| `WHATSAPP_KIT_FLOW_ID` | ⬜ | WhatsApp Flow para elegir las mezclas del kit con casillas (se crea con `python -m app.whatsapp_flows crear <WABA_ID>`). Sin él, se eligen de una en una con listas |
| `WHATSAPP_FLOWS_MODE` | ⬜ | `draft` mientras se prueba el Flow; `published` después de publicarlo |
| `LOG_LEVEL` | ⬜ | Nivel de logging (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

> **Importante:** Nunca subas `.env` al repositorio. El archivo `.gitignore` ya lo excluye.

---

## Ejecución en desarrollo

```bash
uvicorn app.main:app --reload --port 8000
```

Luego expón el puerto con ngrok:

```bash
ngrok http 8000
```

Copia la URL HTTPS que ngrok genera (ej. `https://xxxx.ngrok-free.app`) y úsala en Meta Developers > WhatsApp > Configuration > Webhook URL.

---

## Configurar el webhook en Meta Developers

1. Ve a **Meta for Developers** → tu app → **WhatsApp** → **Configuration**
2. En **Webhook**, haz clic en **Edit**
3. Ingresa:
   - **Callback URL:** `https://<tu-dominio>/webhook`
   - **Verify Token:** el valor de `VERIFY_TOKEN` en tu `.env`
4. Haz clic en **Verify and Save**
5. Suscríbete al campo **messages** y, con coexistencia, también a **smb_message_echoes** (para que el bot sepa cuándo responde una persona desde la app), **account_update** (avisa al equipo si el número se desconecta), **history** y **smb_app_state_sync**

Para pasar del número de prueba al número de Saboryarte sin perder la app de WhatsApp Business, sigue [docs/despliegue/numero-del-negocio.md](docs/despliegue/numero-del-negocio.md).

---

## Pruebas

```bash
pytest -v
```

Para ver la cobertura:

```bash
pip install pytest-cov
pytest --cov=app --cov-report=term-missing
```

---

## Comparar modelos

`evals/casos_respuestas_libres.json` tiene 16 casos (13 críticos) para las respuestas libres: datos de envío y pago, salud, otras marcas, descuentos, granel, intentos de manipulación, reclamos graves y voz de marca. El script los corre varias veces por modelo con el prompt de producción y un juez (Claude Opus 5.5, a ciegas y con la base de conocimiento) decide si cada respuesta cumple los criterios y la voz:

```bash
python -m evals.comparar_modelos --modelos claude-sonnet-5-5
```

Llama a la API real (unos US$0,25 por corrida con Sonnet 5.5; el script se detiene antes de US$0,95). Vuelve a correrlo cuando cambies el prompt, la base de conocimiento o el modelo. Los resultados quedan en `evals/resultados/`.

## Costo real por conversación

Cada llamada a Claude (tokens de entrada, caché escrita y leída, salida y costo) y cada mensaje que Meta acepta quedan registrados en la misma base SQLite, a nombre del cliente. El registro se guarda 90 días. Para ver el reporte:

```bash
python -m app.usage            # últimos 30 días
python -m app.usage --dias 7
```

Muestra el costo de Claude y los mensajes enviados por conversación (una pausa de más de 24 h abre otra) y los mensajes de WhatsApp del mes con su costo estimado: 1.000 mensajes de servicio gratis al mes y US$0,0008 cada uno después (tarifa de Colombia, oct 2026). Los precios están en `app/usage.py`, y los evals usan los mismos. El reporte solo muestra los últimos 4 dígitos de cada número.

## Contenido que edita la dueña (`contenido/`)

Precios y voz de marca viven fuera del código, en la carpeta `contenido/`, para que se puedan cambiar desde github.com sin tocar programación:

| Archivo | Qué es | ¿Lo lee Especia? |
|---|---|---|
| `precios.yaml` | Precios al público, kits armados y tarifas de envío. **Única fuente de precios**: de aquí salen el cobro en Bold, los resúmenes del pedido y lo que Especia responde. | Sí |
| `base_de_conocimiento.md` | Lo que Especia sabe: marca, catálogo, envíos, pagos, puntos de venta. Las tablas de precios se generan desde `precios.yaml` (marcas `{{tabla_…}}`). | Sí |
| `voz_whatsapp.md` | Cómo habla Especia en WhatsApp, sacado del manual aprobado. | Sí |
| `voz_de_marca.md` | Manual de tono completo, tal como lo aprobó la marca (sobre todo para Instagram). | No: nombra otras cuentas y trae reglas de redes |

**Cómo cambiar un precio (la dueña):** con una hoja de cálculo, sin editar archivos de código:

1. Pedirle a Claude Code, en un chat del proyecto, «la hoja de precios actual». Claude la genera con `python -m app.hoja_precios exportar`.
2. Cambiar los precios en la columna amarilla (Excel o Google Sheets) y guardarla como `.xlsx`.
3. Adjuntarla en un chat de Claude Code del proyecto: «Actualiza los precios con esta hoja». Claude la valida, muestra qué cambia, pide confirmación y deja el cambio en un pull request (el procedimiento está en `CLAUDE.md`).

Los precios nuevos se cobran cuando el bot se reinicia con esa versión. También se puede editar `contenido/precios.yaml` directamente en github.com.

**Si el archivo queda mal escrito** (un precio con puntos, una mezcla borrada, un nombre cambiado), Especia no cobra con datos dudosos: sigue con los últimos precios válidos (copia en `data/precios_validos.json`) y envía un correo al equipo con el error. Sin ninguna copia válida, el bot no arranca.

## Pagos y registro de pedidos

En el resumen del pedido el cliente elige **«Pagar con Bold»** (link de tarjeta, PSE, Botón Bancolombia o Nequi; el webhook de Bold confirma el pago) o **«Transferencia»** (Especia da los datos de la cuenta de Bancolombia y pide el comprobante). El comprobante (foto o PDF) se guarda con el pedido y le llega al equipo por correo con los botones **«Confirmar pago»** y **«No llegó»**: al confirmar, Especia le avisa al cliente. Los botones necesitan `PUBLIC_BASE_URL` y `TRANSFER_LINK_SECRET`; sin ellos se confirma con `python -m app.transferencias confirmar SAB-00003`.

Los pedidos quedan en la tabla `pedidos` de `data/conversaciones.db` (en el servidor). Cada día a las 7:00 p. m. `python -m app.exporte diario` envía al equipo un Excel con los pedidos del día y el acumulado del mes, y lo deja en la carpeta de pedidos de Google Drive.

## Historial y copias de seguridad

La conversación activa se reinicia a las 24 h, pero el archivo de chats dura 60 días, la memoria del cliente (para ofrecerle «¿a los mismos datos de la vez pasada?») hasta 1 año si compró, y el chat de cada pedido pagado 1 año. La copia diaria cifrada va a Google Drive (60 días) y cada pedido pagado queda como un `.txt` legible en una carpeta del equipo. Plazos, configuración en Hostinger y cómo restaurar: [docs/despliegue/respaldos.md](docs/despliegue/respaldos.md).

## Estructura del proyecto

```
whatsapp-claude-agent/
├── app/
│   ├── __init__.py
│   ├── main.py        # Rutas FastAPI (GET y POST /webhook)
│   ├── whatsapp.py    # Modelos Pydantic + envío de mensajes via Meta Graph API
│   ├── claude.py      # Respuestas de Claude y extracción estructurada de pedido y envío
│   ├── greetings.py   # Detección de saludos y de "menú", y mensajes de bienvenida aleatorios
│   ├── router.py      # Decide qué responder según el estado y los pasos de los flujos
│   ├── prompt.py      # System prompt de Claude: voz de marca + base de conocimiento pública
│   ├── precios.py     # Precios de contenido/precios.yaml, validados (con respaldo de los últimos válidos)
│   ├── archivo.py     # Archivo de chats (60 días), memoria del cliente y chat de cada pedido pagado (1 año)
│   ├── respaldo.py    # Copia de seguridad diaria a Google Drive con rclone
│   ├── transferencias.py # Pago por transferencia: comprobantes y botones del equipo
│   ├── conexion.py      # Conexión del número del negocio en coexistencia (/conectar) y avisos de desconexión
│   ├── exporte.py     # Excel diario de pedidos (correo y Drive)
│   ├── hoja_precios.py # Hoja de precios para la dueña: exportar e importar con validación
│   ├── orders.py      # Cálculos del pedido: subtotal, tarifa de envío por ciudad y resumen (P8)
│   ├── handoff.py     # Traspaso: horario, ficha, silencio, reactivación y alertas
│   ├── notify.py      # Aviso al equipo por correo (SMTP)
│   ├── bold.py        # Bold: links de pago y firma de los avisos del webhook
│   ├── pedidos.py     # Pedidos confirmados y su estado de pago (SQLite)
│   ├── state.py       # Estado de conversación y deduplicación (SQLite)
│   ├── usage.py       # Registro y reporte del costo real (tokens de Claude y mensajes enviados)
│   ├── flows/
│   │   ├── __init__.py    # Lectura de mensajes.json y catalogo.json (pasos, opciones, fichas)
│   │   ├── catalogo.json  # Nombres y descripciones de los productos (los precios están en contenido/)
│   │   └── mensajes.json  # Texto, botones y listas de cada paso de los flujos de Saboryarte
│   └── config.py      # Configuración via variables de entorno
├── contenido/                       # Lo edita la dueña: precios, base de conocimiento y voz de marca
├── docs/
│   ├── guia-construccion-flujos.md  # Cómo diseñar los flujos de ventas, consultas, pedidos y atención
│   ├── flujos/saboryarte.md         # Diseño de los flujos de Saboryarte (rutas, traspaso, voz de marca)
│   └── knowledge-base/interno/      # Precios de distribuidor: nunca van al prompt
├── tests/
│   ├── __init__.py
│   └── test_webhook.py
├── .env.example       # Plantilla de variables de entorno
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Seguridad

- Los secretos se leen **únicamente** de variables de entorno, nunca del código fuente.
- `.env` está en `.gitignore`; solo `.env.example` (sin valores reales) se versiona.
- Cada aviso de WhatsApp se verifica con la firma HMAC-SHA256 del encabezado `X-Hub-Signature-256` y `APP_SECRET`; los que no coinciden se rechazan con 403. Sin `APP_SECRET` (solo en pruebas locales) no se verifica.
- La llave de acceso del número del negocio queda en `data/whatsapp_conexion.json` con permisos 600; los enlaces de conexión y de transferencias van firmados con `TRANSFER_LINK_SECRET`.

---

## Despliegue en producción

Cualquier plataforma que soporte contenedores o Python funciona. Ejemplo con Railway, Render o Fly.io:

1. Define las variables de entorno en el panel de la plataforma (nunca en el código).
2. El comando de inicio es: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Asegúrate de usar HTTPS (requerido por Meta).
