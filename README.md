# WhatsApp Claude Agent

Agente de WhatsApp construido con **Python + FastAPI** que recibe mensajes via [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api), consulta a **Claude** (Anthropic) y responde automáticamente al remitente.

## Características

- `GET /webhook` — verificación del webhook por parte de Meta
- `POST /webhook` — recibe mensajes de WhatsApp, consulta a Claude y envía la respuesta
- Integración con **Claude Opus** via Anthropic SDK (pensamiento adaptativo)
- Variables de entorno con validación via **pydantic-settings**
- Pruebas con **pytest**

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
5. Suscríbete al campo **messages**

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

## Estructura del proyecto

```
whatsapp-claude-agent/
├── app/
│   ├── __init__.py
│   ├── main.py        # Rutas FastAPI (GET y POST /webhook)
│   ├── whatsapp.py    # Modelos Pydantic + envío de mensajes via Meta Graph API
│   ├── claude.py      # Consulta a Claude via Anthropic SDK
│   └── config.py      # Configuración via variables de entorno
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
