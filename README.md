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
- Toma de pedidos (P6 → P7 → P8): Claude extrae del texto las mezclas, cantidades y datos de envío; el código valida contra el catálogo y calcula subtotal, envío por ciudad y total. El pedido aún no se registra ni se pasa al equipo
- Traspaso a una persona en modo coexistencia: "asesor", "Hablar con alguien" o "Confirmar pedido" silencian el bot y envían la ficha por correo al equipo, que responde desde la app de WhatsApp Business. El bot retoma con la frase de cierre o tras horas sin mensajes del equipo, y alerta si nadie responde a tiempo
- Claude responde con la voz de Saboryarte y solo con la base de conocimiento pública (`docs/knowledge-base/saboryarte.md`)

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
| `APP_SECRET` | ⬜ | App Secret para validar firmas de payload |
| `STATE_DB_PATH` | ⬜ | Archivo SQLite del estado de conversaciones (por defecto `data/conversaciones.db`) |
| `HANDOFF_EMAIL_TO` | ⬜ | Correo del equipo que recibe la ficha de traspaso y las alertas |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | ⬜ | Servidor SMTP para enviar esos correos (Gmail: `smtp.gmail.com`, 587, contraseña de aplicación) |
| `HANDOFF_ALERT_MINUTES` | ⬜ | Minutos hábiles sin respuesta antes de alertar al equipo (por defecto 60) |
| `HANDOFF_IDLE_HOURS` | ⬜ | Horas sin mensajes del equipo para que el bot retome solo (por defecto 12) |
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
5. Suscríbete al campo **messages** (y, con coexistencia, también a **smb_message_echoes**, para que el bot sepa cuándo responde una persona desde la app)

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
│   ├── orders.py      # Cálculos del pedido: subtotal, tarifa de envío por ciudad y resumen (P8)
│   ├── handoff.py     # Traspaso: horario, ficha, silencio, reactivación y alertas
│   ├── notify.py      # Aviso al equipo por correo (SMTP)
│   ├── state.py       # Estado de conversación y deduplicación (SQLite)
│   ├── flows/
│   │   ├── __init__.py    # Lectura de mensajes.json y catalogo.json (pasos, opciones, fichas)
│   │   ├── catalogo.json  # Productos con precio al público (fichas y recomendaciones)
│   │   └── mensajes.json  # Texto, botones y listas de cada paso de los flujos de Saboryarte
│   └── config.py      # Configuración via variables de entorno
├── docs/
│   ├── guia-construccion-flujos.md  # Cómo diseñar los flujos de ventas, consultas, pedidos y atención
│   ├── flujos/saboryarte.md         # Diseño de los flujos de Saboryarte (rutas, traspaso, voz de marca)
│   └── knowledge-base/              # Contenido que puede usar el bot (interno/ nunca va al prompt)
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
- Para producción se recomienda validar la firma HMAC-SHA256 del encabezado `X-Hub-Signature-256` usando `APP_SECRET`.

---

## Despliegue en producción

Cualquier plataforma que soporte contenedores o Python funciona. Ejemplo con Railway, Render o Fly.io:

1. Define las variables de entorno en el panel de la plataforma (nunca en el código).
2. El comando de inicio es: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Asegúrate de usar HTTPS (requerido por Meta).
