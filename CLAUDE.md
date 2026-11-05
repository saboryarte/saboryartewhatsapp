# CLAUDE.md

Especia: bot de WhatsApp de Saboryarte (FastAPI + WhatsApp Cloud API + Claude + Bold). Código, comentarios y
mensajes en español. Ver README.md para la arquitectura.

- Pruebas: `.venv/bin/python -m pytest -q` (deben pasar todas antes de cualquier commit).
- Nunca subas `.env` ni `data/` (secretos y datos de clientes). No pidas llaves ni contraseñas por el chat.

## Actualizar precios con la hoja de la dueña

La dueña de Saboryarte puede adjuntar en el chat una hoja de cálculo (.xlsx) con precios nuevos. Quien escribe
puede no ser técnica: explícale todo en lenguaje sencillo, sin términos de programación.

**Si pide la hoja actual:** créala con
`.venv/bin/python -m app.hoja_precios exportar exportes/precios_saboryarte.xlsx` y entrégale ese archivo.

**Si adjunta una hoja editada:**

1. Guarda el adjunto en `exportes/` (no va al repositorio) y revísalo sin aplicar nada:
   `.venv/bin/python -m app.hoja_precios importar exportes/<archivo>.xlsx`
2. Si el comando dice que la hoja no se puede usar, explícale el error en palabras simples (qué fila y qué
   corregir) y pídele que la vuelva a adjuntar. No edites la hoja ni `precios.yaml` a mano para «arreglarla».
3. Muéstrale la lista de cambios tal como la imprime el comando («Sal de oro: $26.000 → $28.000») y pregúntale
   si está bien. Si un cambio parece un error de digitación (por ejemplo, un precio que sube o baja más de la
   mitad), pregúntale por ese cambio en particular. **No apliques nada sin su confirmación.**
4. Con su confirmación: repite el comando con `--aplicar`, corre las pruebas y haz commit solo de
   `contenido/precios.yaml` con el mensaje `precios: <resumen de los cambios>`.
5. Súbelo como pull request (rama `precios/<AAAA-MM-DD>`) para que quien administra el bot lo revise y lo
   despliegue. Dile que los precios nuevos empiezan a cobrarse cuando el bot se reinicie con esa versión.

Los precios solo viven en `contenido/precios.yaml`: de ahí salen el cobro en Bold, el resumen del pedido y lo que
Especia responde. No escribas precios en `contenido/base_de_conocimiento.md` ni en el código.
