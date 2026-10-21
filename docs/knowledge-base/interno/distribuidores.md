# Distribuidores — información interna

> **Solo para la asesora y para la lógica de calificación.** Este archivo **no se carga en el prompt** de Claude y su contenido nunca se envía al cliente desde el bot. Las condiciones comerciales las comunica siempre una persona.
> **Fuente:** artefacto «Chatbot WhatsApp Saboryarte — Flujos del chatbot» · **Última revisión:** 21 oct 2026

## Precios de distribuidor

| Producto | Precio distribuidor | Precio sugerido al público |
|---|---|---|
| Cada mezcla (100 g, o 60 g la tierra de fuego) | $18.500 | $26.000 |
| Kit mini | $48.000 | $60.000 |
| Granel (todas las sales, menos la tierra de fuego) | $160 por gramo | — |
| Kits armados de 2 a 6 mezclas | No se ofrecen a distribuidores | — |

- **Condiciones de pago:** contra entrega o plazo máximo de 2 semanas. Las explica la asesora.
- **Propuesta comercial (PDF):** incluye los precios de distribuidor. El bot la envía solo al terminar la calificación (paso D8), no a cualquiera que pregunte.

## Calificación del prospecto

Se calcula en código con las respuestas de D2 (tipo de negocio) y D5 (volumen). No la decide Claude.

| Etiqueta | Regla | Qué hace la asesora |
|---|---|---|
| **Caliente** | D5 = pedido recurrente o volumen grande **y** D2 en un canal objetivo | Escribe el mismo día hábil. Propone muestras o una llamada. |
| **Tibio** | D5 = pedido recurrente o volumen grande en otro tipo de negocio, **o** D5 = prueba pequeña en un canal objetivo | Escribe en 1 o 2 días hábiles con catálogo y condiciones. |
| **Frío** | Prueba pequeña fuera de los canales objetivo, o «apenas empiezo» o aún no tiene negocio | Responde con calma y lo deja en seguimiento. |

**Canales objetivo:** supermercado con sección gourmet, tienda especializada o delicatessen, y tienda de regalos o anchetas (`canal_objetivo: true` en `app/flows/mensajes.json`).

```text
si D6 == "apenas empiezo" o D2 == "empezar a revender" → frío
si no, si D5 in {recurrente, grande} y canal_objetivo  → caliente
si no, si D5 in {recurrente, grande}                   → tibio
si no, si canal_objetivo                               → tibio
si no                                                  → frío
```

## Pendientes por confirmar

- **Tienda natural o ecológica:** ¿cuenta como «tienda especializada» y, por lo tanto, como canal objetivo? En los datos quedó como no objetivo.
- **Kit mini para distribuidores:** hoy es solo de venta directa. Se quiere abrir de forma selectiva a distribuidores de canal gourmet, regalos o anchetas en ciudades donde Saboryarte no vende directo. Si el prospecto pregunta, el bot toma la ciudad (D4) y el canal (D2), y la asesora decide.
