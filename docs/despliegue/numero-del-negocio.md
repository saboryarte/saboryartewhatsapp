# Pasar Especia al número de WhatsApp de Saboryarte (coexistencia)

Hoy Especia responde en el número de prueba de Meta. Esta guía conecta el **número real de Saboryarte** sin que el
equipo pierda la app: el número queda a la vez en la **app de WhatsApp Business** (el celular de la dueña) y en el
bot. Meta llama a esto **coexistencia**.

> **Conectar es salir al aire.** En cuanto termina la conexión, todo cliente que escriba al número recibe respuesta
> de Especia. Haz la conexión solo cuando todo lo de la sección 1 esté listo.

## Qué cambia para el equipo

- La app de WhatsApp Business sigue funcionando: el equipo ve todos los chats, también lo que responde Especia, y
  puede escribir en cualquier momento (cuando escribe, Especia se calla en ese chat).
- **Hay que abrir la app en el celular de la dueña al menos cada 14 días.** Si no, Meta desconecta el bot. Llega un
  correo «🚨 Especia quedó desconectado» con el motivo.
- Al conectar, **WhatsApp Web y los demás dispositivos vinculados se desconectan**: hay que volver a vincularlos
  (hasta 4; Meta indica que no se admiten los de Windows ni WearOS).
- Según la tabla de Meta, con coexistencia la app **deja de tener**: grupos, mensajes temporales y de una sola
  vista, ubicación en tiempo real, listas de difusión, llamadas, catálogo/pedidos/estados, mensajes de bienvenida y
  de ausencia, **respuestas rápidas** y **etiquetas**, perfil del negocio y canales.
  - La frase para devolverle el chat a Especia («Te dejo de nuevo con Especia») hay que **escribirla**. Truco:
    crear en el teclado del celular un reemplazo de texto (iPhone: Ajustes → General → Teclado → Reemplazo de
    texto; Android: diccionario del teclado), por ejemplo «eesp» → «Te dejo de nuevo con Especia 💛».
- Los mensajes que el equipo envía desde la app siguen siendo gratis. Los de Especia se cobran con la tarifa de la
  API de WhatsApp.

## 1. Antes de conectar (una sola vez)

### Meta (business.facebook.com y developers.facebook.com)

1. **Verificar el negocio** en Meta Business Manager → Centro de seguridad → Verificación del negocio. Pide nombre,
   dirección, teléfono, correo, sitio web y documentos (RUT, certificado de Cámara de Comercio). Sin esto no hay
   revisión de la app. ✅ Hecho (noviembre de 2026).
2. **Política de privacidad publicada** con una dirección web (Ley 1581) y cargada en la app: Configuración de la
   app → Básica → URL de la política de privacidad. También el ícono y la categoría de la app.
3. **Tech Provider: solo si el ensayo lo exige.** La app de Especia es solo para Saboryarte, y registrarla como
   Tech Provider («Yes, I'm a Tech Provider») es irreversible y obliga a una revisión pensada para quien atiende a
   otros negocios. Primero se hace el ensayo de la sección «Ensayo con otro número». Si Meta rechaza la coexistencia
   sin ese registro, se pregunta a Meta por soporte directo (business.facebook.com/direct-support) antes de
   registrarla, o se usa un proveedor oficial.
4. **Revisión de la app (App Review)**, si Meta la pide, con *acceso avanzado* a `whatsapp_business_messaging` y
   `whatsapp_business_management`. Meta pide dos videos:
   - Un mensaje enviado desde la app y recibido en WhatsApp (sirve grabar a Especia respondiendo en el número de
     prueba).
   - La creación de una plantilla de mensaje (sirve grabarla en WhatsApp Manager).
5. **Configuración de «Embedded Signup»**: Facebook Login for Business → Configurations → Create from template →
   **«WhatsApp Embedded Signup Configuration With 60 Expiration Token»** (no la de «Measurement Partner», que es de
   solo lectura). Aparece sin ser Tech Provider. Su Configuration ID va en `META_SIGNUP_CONFIG_ID`.
   - La llave que entrega esta configuración vence a los 60 días. Ver «La llave de 60 días» en la sección 3.
6. En Facebook Login for Business → Configuración: activar «Login with the JavaScript SDK» y agregar el dominio
   del bot (el de `PUBLIC_BASE_URL`) en los dominios permitidos.
7. **Webhook** (WhatsApp → Configuration): la dirección `https://<dominio del bot>/webhook` y suscribir los campos
   `messages`, `smb_message_echoes`, `account_update`, `history` y `smb_app_state_sync`.
8. En WhatsApp Manager: **método de pago** para los mensajes de la API.

### Servidor (Hostinger)

1. El bot corriendo con **https** (Meta no acepta http). Ver también [respaldos.md](respaldos.md) para las tareas
   programadas.
2. En el `.env` del servidor:
   - `APP_SECRET`: obligatorio. Con él el bot rechaza avisos que no vengan de Meta, y se usa para la conexión.
   - `META_APP_ID` (Configuración de la app → Básica) y `META_SIGNUP_CONFIG_ID` (paso 5).
   - `PUBLIC_BASE_URL` y `TRANSFER_LINK_SECRET`: firman el enlace de conexión y los botones de transferencias.
3. **Pagos en producción**: llaves de producción de Bold, `BOLD_SANDBOX=false`, el aviso de pagos de Bold apuntando
   a `https://<dominio del bot>/webhooks/bold` y `BOLD_RETURN_URL=https://wa.me/<número del negocio>`.

### En el celular de la dueña

- App **WhatsApp Business** actualizada (los proveedores citan la versión 2.24.17 o posterior), con el número de
  Saboryarte y en uso normal desde hace varios días.
- Hacer una copia de seguridad de los chats (Ajustes → Chats → Copia de seguridad) por precaución.

## 2. El día de la conexión (unos 10 minutos)

1. En el servidor, crear el enlace (dura 48 horas):

   ```bash
   .venv/bin/python -m app.conexion enlace
   ```

2. La dueña abre el enlace **en un computador**, con el celular de la app a la mano, y toca **«Conectar WhatsApp»**.
3. Inicia sesión en Facebook con la cuenta que administra Saboryarte en Meta Business Manager, elige el negocio y
   la opción de conectar su app de WhatsApp Business existente.
4. Meta muestra un **código QR**: se escanea desde la app de WhatsApp Business (el aviso aparece en la app) y se
   aceptan los permisos, incluido compartir el historial.
5. La página muestra «¡Listo! …» y al correo del equipo llega «✅ WhatsApp … conectado a Especia».
6. Comprobar:

   ```bash
   .venv/bin/python -m app.conexion estado
   ```

   Debe decir `en la app (coexistencia) = True` y `plataforma = CLOUD_API`.

7. Prueba real: escribir «hola» al número de Saboryarte desde otro celular. Especia responde y el chat se ve en la
   app de la dueña.

El bot guarda el número y su llave de acceso en `data/whatsapp_conexion.json` (solo legible por el usuario del bot,
nunca va al repositorio) y desde ese momento lo usa en lugar de `WHATSAPP_PHONE_NUMBER_ID` y `WHATSAPP_TOKEN` del
`.env`, también después de reiniciar.

## 3. Después

- **La llave de 60 días.** Si el usuario del sistema del `.env` (`WHATSAPP_TOKEN`, que no vence) ya tiene acceso a
  la cuenta de WhatsApp conectada, el bot la usa y no hay vencimiento. Si no, usa la llave de la conexión, que vence
  a los 60 días, y el correo de conexión lo advierte. Para pasar a la permanente:
  1. business.facebook.com → Configuración → Usuarios del sistema → el usuario del bot → **Asignar activos** →
     Cuentas de WhatsApp → la cuenta de Saboryarte → control total.
  2. En el servidor:

     ```bash
     .venv/bin/python -m app.conexion llave
     ```

  Mientras siga con la de 60 días, el bot envía un correo «⏳ La llave de WhatsApp de Especia vence pronto» cada
  día durante los últimos 10 días.

- **Contactos e historial**: el bot los pide a Meta al conectar. Meta solo los envía una vez y solo si se piden en
  las primeras 24 horas; si `estado` muestra que falló, repetirlo a tiempo:

  ```bash
  .venv/bin/python -m app.conexion sincronizar
  ```

  El historial que llega no se copia al archivo de chats (solo se registra en el log que llegó).
- **Si llega el correo «🚨 Especia quedó desconectado»**: los clientes siguen escribiendo a la app, pero nadie les
  responde automáticamente. Atenderlos desde la app y repetir la sección 2 con un enlace nuevo.
- **Volver al número de prueba**: borrar `data/whatsapp_conexion.json` y reiniciar el bot. (Para desconectar el
  número del lado de Meta: en la app, Ajustes → Cuenta → Plataforma para empresas → Desconectar.)
- El límite inicial es de 250 clientes nuevos por día; sube con la verificación del negocio y la buena calidad de
  los mensajes. Con coexistencia el número envía hasta 20 mensajes por segundo.

## Ensayo con otro número (antes de tocar el de Saboryarte)

La documentación de Meta exige ser Tech Provider para la coexistencia, pero la configuración de Embedded Signup sí
aparece sin serlo. El ensayo dice si Meta acepta conectar un número de la app de WhatsApp Business a nuestra app:

1. Un celular con **otro número** (una SIM prepago sirve) y la app WhatsApp Business, con algunos chats.
2. En el computador, con el bot y ngrok corriendo: en el `.env` local, `PUBLIC_BASE_URL` con la dirección de ngrok,
   `TRANSFER_LINK_SECRET`, `META_APP_ID` y `META_SIGNUP_CONFIG_ID`. Agregar el dominio de ngrok en Facebook Login
   for Business → Settings → dominios permitidos del SDK de JavaScript.
3. `python -m app.conexion enlace`, abrir el enlace y conectar el número de ensayo.
4. Resultado:
   - **Conecta y `estado` dice `en la app (coexistencia) = True`:** el camino funciona sin Tech Provider.
   - **Meta lo rechaza** (la página muestra el error): queda confirmado que hace falta ser Tech Provider o usar un
     proveedor oficial.
5. Desconectar el número de ensayo (en su app: Ajustes → Cuenta → Plataforma para empresas → Desconectar) y borrar
   `data/whatsapp_conexion.json` para que el bot vuelva al número de prueba.

## Pendiente de confirmar con Meta

- Si un negocio puede conectar **su propio número a su propia app** en coexistencia sin registrarse como Tech
  Provider (lo dice el ensayo). Si Meta lo bloquea, la alternativa es un proveedor oficial que ya ofrezca
  coexistencia.
- Meta no menciona restricciones por país, ni a favor ni en contra de Colombia.
- Meta anunció la versión 5 de Embedded Signup (20 de octubre de 2026). La página de conexión usa el formato de la
  versión 4 y acepta las dos respuestas; si Meta cambia algo, el error aparece en la misma página.
