# Fichaitor

Nombre comercial decidido el 2026-11-11. Dominio: `fichaitor.es` (registrar también `fichaitor.com`). La carpeta conserva el nombre provisional `agente-gbp`.

Agente conversacional que gestiona la ficha de Google Business Profile de un negocio desde WhatsApp. Es experto en la ficha y en SEO local, se adelanta a las fechas clave y nunca cambia nada sin la confirmación del dueño.

Estado: **fase de diseño**. No hay código. Fecha: 2026-11-11.

## Qué hay construido

Primer servicio: el **diagnóstico público** de una ficha (`POST /api/v1/public/diagnosis`). Puntúa de 0 a 100 con reglas fijas sobre datos públicos de Google Places y devuelve hasta cinco mejoras. Las reglas están en `app/domain/diagnosis.py` y sus pruebas en `tests/`.

Segundo bloque: la **lista de espera** (`POST /api/v1/public/waitlist`) y el **embudo** del experimento, sobre PostgreSQL. El esquema está en `db/schema.sql`. El usuario de la API solo puede insertar: no puede leer, cambiar ni borrar contactos.

Para ver la lista de espera y el embudo, con el usuario propietario:

```bash
docker compose exec -T db psql -U fichaitor -d fichaitor -c "select created_at, channel, contact, business_name, score, source from waitlist_entry order by created_at desc"
```

```bash
docker compose exec -T db psql -U fichaitor -d fichaitor -c "select kind, source, count(*) from funnel_event group by 1, 2 order by 1, 2"
```

Falta: la página web, los textos de privacidad y consentimiento, y la prueba contra Google real.

### Ejecutarlo

Con el negocio de ejemplo, sin llamar a Google:

```bash
docker compose up -d --build
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/public/diagnosis -H "content-type: application/json" -d '{"query":"demo bar granada"}'
```

Con Google real: copia `.env.example` a `.env`, pon `FICHAITOR_PLACES_API_KEY` y `FICHAITOR_FAKE_PLACES=false`.

Pruebas y formato:

```bash
uv run pytest -q
```

```bash
uv run ruff check .
```

## Documentos

| Documento | Contenido |
|---|---|
| [docs/01-flujos.md](docs/01-flujos.md) | Recorridos del usuario, flujo conversacional, estados y excepciones |
| [docs/02-hld.md](docs/02-hld.md) | Arquitectura general, componentes y comunicación entre servicios |
| [docs/03-lld.md](docs/03-lld.md) | Esquema de datos, agentes, prompts y contratos de API |
| [docs/04-ux-conversacional.md](docs/04-ux-conversacional.md) | Tono, patrones de mensaje, botones y pantallas del panel |
| [docs/05-roadmap.md](docs/05-roadmap.md) | Fases, qué hay que investigar, qué hay que construir y criterios de salida |
| [docs/06-estandares.md](docs/06-estandares.md) | Stack, separación de responsabilidades, Docker y estrategia de pruebas |

## Decisiones tomadas

1. **Venta directa**, negocio a negocio, con captación por TikTok y TikTok Ads. El alta es autoservicio.
2. **WhatsApp es la interfaz principal.** El panel web es secundario. Telegram y email son canales alternativos.
3. **Repo nuevo, desde cero.** No se toca nada de lo existente (GBP Autopilot en n8n sigue como banco de pruebas; Bitclick Social sigue su plan).
4. **Se reutilizan decisiones, no código.** El stack y los patrones vienen de la evaluación de arquitectura de Bitclick Social (FastAPI, Postgres con RLS, cola en Postgres, React + Vite).
5. **El modelo de lenguaje nunca escribe en Google.** Propone una acción estructurada; un ejecutor determinista la aplica solo tras la confirmación del dueño.
6. **MVP:** diagnóstico público, alta, reseñas con aprobación, fechas clave (festivos y fechas comerciales) y "hoy cierro". Posts, fotos y competencia van en la versión Pro.

## Decisiones abiertas

| Decisión | Cuándo se cierra |
|---|---|
| Precios definitivos. Hipótesis de lanzamiento en [docs/fase0/precios.md](docs/fase0/precios.md): gratis, Pro a 35 €/año y Pro WhatsApp a 59 €/año, sin monedero | Tras la beta |
| Proveedor del modelo de lenguaje (se decide con el conjunto de evaluación, no por preferencia) | Fase 1 |
| Acceso a WhatsApp: Cloud API directa o a través de un proveedor | Fase 0 |
| Relación futura con el módulo 009 de Bitclick Social (lo sustituye, lo alimenta o conviven) | Tras la beta |
| Precios definitivos | Tras el experimento de TikTok |
