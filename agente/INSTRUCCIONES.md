# Analista Financiero Agéntico — cómo arrancarlo

## Una sola vez: preparar los datos

```powershell
cd agente
python analista/etl.py
```

Lee el contable (255.859 asientos) y los 6 archivos de nómina/ausencias, y deja
las tablas listas en `datos/`. Tarda ~90 s. **No necesita internet ni API key.**

## Comprobar que los números están bien (sin gastar un centavo)

```powershell
python analista/verificar.py
```

Responde las 7 preguntas del caso usando solo pandas e imprime `[OK]` / `[FALLA]`
por cada chequeo. Si esto falla, no tiene sentido conectar el modelo.

## Levantar la página

Pon la llave en un archivo `.env` **dentro de `agente/`** (ya está en `.gitignore`):

```
ANTHROPIC_API_KEY=sk-ant-...
```

Sin comillas y sin espacios alrededor del `=`. Luego:

```powershell
node web/server.mjs
```

Abre <http://localhost:3000>. El servidor imprime `ANTHROPIC_API_KEY: detectada`
si la leyó bien.

## Generar las respuestas a las 7 preguntas del caso

```powershell
node web/responder.mjs          # las 7
node web/responder.mjs 1 4      # solo la 1 y la 4, conserva las demás
```

Escribe `datos/respuestas.json`, que alimenta la pestaña **Las 7 preguntas** del
dashboard. Guarda también el código que ejecutó y el costo de cada respuesta.
Esto **sí gasta API**; el resto de la página no.

Para cambiar de modelo (si el razonamiento causal se queda corto):

```powershell
$env:MODELO = "claude-sonnet-5"
```

## Qué hay en cada carpeta

| Ruta | Qué es |
|---|---|
| `analista/etl.py` | Lee las fuentes una sola vez y escribe `datos/`. Repara las 58 filas rotas del CSV. |
| `analista/helper.py` | Las funciones financieras verificadas. El agente recibe solo sus firmas. |
| `analista/sandbox.py` | Proceso Python persistente donde el agente ejecuta el código que escribe. |
| `analista/verificar.py` | El gate: las 7 preguntas resueltas sin LLM. |
| `web/agente.mjs` | El motor: sandbox, prompt del analista y agent loop. Lo comparten el servidor y el generador de respuestas, para que no haya dos copias del prompt. |
| `web/server.mjs` | Servidor local. Node puro, sin `npm install`. |
| `web/responder.mjs` | Corre las 7 preguntas del caso y guarda `datos/respuestas.json`. |
| `web/public/index.html` | La página: chat + dashboard + las 7 respuestas + tabla de detalle. |
| `datos/` | Generado por el ETL. Se puede borrar y regenerar. |
| `PRD.md` | El documento de producto: contexto, arquitectura y criterios. |

## Privacidad

La página corre en tu máquina. El CSV de 51 MB y las nóminas **nunca salen de
aquí**: el sandbox de Python es local. A la API de Claude solo viajan tu pregunta,
el manual de datos y los resultados agregados que el agente decide mirar.

---

# Cómo se entregó

Este agente se construyó para el **Quick Golden Bench**, el ranking del hackathon de
Claude Community Bogotá en Quick Logística (22-ago-2026). **Ese leaderboard ya cerró**,
así que esto queda como registro de cómo fue la entrega, no como algo repetible.

Fueron tres pasos — generar, validar el formato, enviar:

```powershell
node web/bench.mjs                      # corre las 7 preguntas → bench/answers + bench/traces
python analista/validar_entrega.py --model claude-haiku-4-5-20251001
python submit.py --team yeison-zuneda --model claude-haiku-4-5-20251001 `
       --answers bench/answers --traces bench/traces `
       --tooling "Node puro + sandbox Python local, pre-computo con pandas"
```

Las respuestas y las trazas que se enviaron están en `bench/answers/` y `bench/traces/`,
tal como salieron.

## Las reglas del hackathon

Las escribió **Jairo Torregrosa** y viven en su repositorio, que es la fuente:

- <https://github.com/JairoTorregrosa/charlas>
- <https://github.com/JairoTorregrosa/charlas/tree/main/2026-agents-bogota/hackathon>

Ahí están las convenciones contables del caso, la rúbrica, el formato de entrega y los
7 enunciados.
