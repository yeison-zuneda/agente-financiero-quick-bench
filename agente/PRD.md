# PRD — Analista Financiero Agéntico (Hackathon Quick, 22-ago-2026)

## Context

El reto real del hackathon (confirmado en la diapositiva `reglas formales del proyecto.png`) es:

> **Construir un sistema agéntico, eficiente y lo más económico posible, que pueda analizar
> los datos explicados y responder preguntas arbitrarias sobre la información como lo haría
> un analista financiero profesional.**

Con tres criterios de evaluación explícitos:

| Criterio | Qué exige | Cómo lo cumple este diseño |
|---|---|---|
| **AGÉNTICO** | "Razona sobre los datos, no solo consulta" | El agente escribe y ejecuta Python real contra el ledger completo; cruza contable ↔ nómina ↔ ausencias para explicar *por qué* cambió un margen |
| **EFICIENTE** | "Responde preguntas arbitrarias sin reescribir el sistema" | Un `helper.py` de funciones verificadas + un sandbox de código libre. Nada está cableado a las 7 preguntas conocidas |
| **ECONÓMICO** | "El costo por respuesta cuenta en la evaluación" | Pre-cómputo (los 255k movimientos se suman UNA vez, no por pregunta) + prompt caching TTL 1h + Haiku 4.5 + solo resultados chicos vuelven al modelo |

El usuario quiere una **página web local** donde pueda preguntar en lenguaje natural y recibir
respuestas de calidad de analista senior. Tiene **~1 hora**. Ya existe trabajo previo reutilizable
de la práctica de logística (`track_mock/`, `track_claude/`), pero el caso real es distinto y el
código nuevo vive aparte.

### Decisiones ya tomadas por el usuario
- **Motor**: híbrido — funciones Python pre-establecidas **y** sandbox de ejecución libre.
- **Web**: Node, 100% local (`localhost`). Los datos nunca salen de la máquina; solo la pregunta
  y los resultados agregados viajan a la API de Claude.
- **Alcance**: chat + panel con el informe de rentabilidad del caso.
- **Si falta tiempo**: se sacrifica el panel visual, **no** la capacidad de responder preguntas
  arbitrarias.

---

## Hallazgos verificados en los datos reales

Corridos con pandas sobre los archivos reales, no asumidos:

**`MAYO-JUNIO-JULIO 2026.csv`** — 255.859 filas × 32 columnas, separador `;`, encoding `latin-1`,
decimal con **coma**, fechas `DD/MM/YYYY HH:MM:SS`.

- Clase de cuenta = **primer dígito de `AccountId`**: `4`=Ingresos, `5`=Gastos, `6`=Costo mercancía
  (solo Warehouse), `7`=Costos de proyecto.
- `CostCenterName` = **GERENCIA**; `ProjectId` = **PROYECTO** (468 distintos).
- Signo: ingreso = `Credito − Debito`; costo = `Debito − Credito`.
- **Margen % por gerencia (mayo → junio → julio)** — ya calculado:

  | Gerencia | May | Jun | Jul | Lectura |
  |---|---|---|---|---|
  | LAST MILE COLOMBIA | 19,2 | 20,2 | 20,5 | **Mejor evolución real y material** |
  | LONG HAUL | 13,2 | 13,4 | 14,0 | Mejora sostenida |
  | FIRST MILE | 14,5 | 14,0 | 14,0 | Plano |
  | **WAREHOUSE** | **18,1** | **17,4** | **13,8** | **La caída — foco de 3 de las 7 preguntas** |
  | COURIER COLOMBIA | −18,2 | 9,8 | 19,1 | **Trampa**: salto enorme sobre ~$4M (inmaterial) |

- **Trampa de parsing**: 2 filas (índices 94385-94386) traen `";3964613"` dentro de `Observation`
  → corren las columnas. Se detectan porque `Conciliate` deja de ser nulo. Hay que repararlas
  o el `CostCenterName` de esas filas queda en `0`.
- **Retroactividad (pregunta 5)**: en el contable `Date` **siempre** cae dentro de su `Period`
  (verificado con crosstab) → la respuesta **no** está ahí. Está en dos lugares:
  1. Nómina: `Period` ≠ `PeriodPayment` (65 registros en junio pertenecen al período 5).
  2. Texto de `Observation`: "ARRIENDO MES DE MAYO 2026", "FACTURA ... ABRIL 2026", etc.

**Nómina** (`ACUMULADO *.xlsx`, ~32-35k filas c/u): **no trae `ProjectId`**. El puente al proyecto
es el join contra el contable por `ClientId + DocumentId + NumberId + RowId` (las filas contables
con `Module = 'N'`).

**Ausencias** (`Listado_de_Ausencias*.xlsx`, hoja `' Data'` — ojo al espacio inicial): **sí** trae
`ProjectId` y `ProjectName`, más `ConceptName` (incapacidades, licencias, vacaciones, suspensiones)
y `DateInitial`/`DateFinal`. Es la fuente directa para las preguntas 6 y 7 (proyectos 594 y 600).

**Entorno verificado**: pandas 2.3.3, pyarrow 21, openpyxl 3.1.5, Node v24.19. `ANTHROPIC_API_KEY`
**no está** en el entorno — el sistema debe avisarlo claramente al arrancar.

---

## Arquitectura

```
                    ┌──── TIEMPO DE DISEÑO (corre 1 vez, ~90 s) ────┐
  CSV 51 MB  ──────►│                                                │
  6 XLSX     ──────►│   analista/etl.py                              │
                    │     · repara las 2 filas corridas              │
                    │     · normaliza coma decimal + fechas          │
                    │     · clasifica cuentas 4/5/6/7                │
                    │     · une nómina→proyecto, marca retroactivos  │
                    └───────────────────┬────────────────────────────┘
                                        ▼
                         datos/ (chico, se lee en ms)
                          ├─ hechos.parquet          255k filas normalizadas
                          ├─ rentabilidad.parquet    468 proy × 3 meses
                          ├─ nomina.parquet          + flag retroactivo
                          ├─ ausencias.parquet       + ProjectId
                          ├─ informe.json            la tabla del caso, lista
                          └─ MANUAL_DATOS.md         el "data manual" cacheado

  ┌──── POR PREGUNTA (~3-6 s, ~US$0,01) ─────────────────────────────┐
  │                                                                   │
  │  navegador ──► server.mjs (Node puro, 0 dependencias npm)         │
  │                    │                                              │
  │                    ├─ system prompt + MANUAL_DATOS.md             │
  │                    │  + firmas de helper.py   ◄── cache 1h (0,1x) │
  │                    │                                              │
  │                    └─ tool: ejecutar_python(codigo)               │
  │                            │                                      │
  │                            ▼  spawn hijo local                    │
  │                       analista/sandbox.py                         │
  │                         · dataframes YA cargados                  │
  │                         · helper.* disponible                     │
  │                         · devuelve solo stdout (truncado)         │
  └───────────────────────────────────────────────────────────────────┘
```

**Por qué esta forma y no otra**: subir el CSV de 51 MB a la Files API y dejar que Claude lo lea en
el sandbox de Anthropic cuesta 10-30x más por pregunta y arriesga timeouts en cada arranque de
contenedor. Acá los 255k movimientos se agregan una sola vez; el modelo solo ve números chicos.
Es el patrón NVIDIA KGMON documentado en `Contexto chat y hakaton/deep research analista financiero.md`
(#1 en DABStep con Haiku 4.5, 30x más rápido que el baseline con Opus).

---

## Archivos a crear

Todo lo nuevo vive en `analista/` y `web/`. **No se toca** `track_mock/`, `track_claude/` ni
`repo_cama/` (quedan como referencia probada).

| Archivo | Responsabilidad |
|---|---|
| `analista/etl.py` | Lee CSV + 6 XLSX → escribe `datos/*.parquet`, `datos/informe.json`, `datos/MANUAL_DATOS.md`. Imprime totales de control. |
| `analista/helper.py` | Funciones financieras verificadas (abajo). Importable desde el sandbox. |
| `analista/sandbox.py` | Recibe código Python por stdin, lo ejecuta con los dataframes y `helper` ya en el namespace, devuelve JSON `{ok, stdout, error}`. Trunca a ~4.000 chars. |
| `analista/verificar.py` | Corre las 7 preguntas del caso contra `helper.py` e imprime los valores gold. Es el chequeo de que los números están bien **antes** de conectar el LLM. |
| `web/server.mjs` | Servidor HTTP + agent loop contra la API de Claude vía `fetch` nativo. Cero npm. SSE para streaming. |
| `web/public/index.html` | Página única: chat izquierda + panel informe derecha. CSS inline, sin build. |
| `INSTRUCCIONES.md` | Cómo arrancar en 2 comandos. |

### Funciones de `helper.py` (las "fórmulas ya programadas")

Cada una con **doble verificación** donde aplique (patrón `KPIMismatchError` que ya existe en
`track_mock/kpis_financieros.py:41` — se reusa la clase):

- `rentabilidad(proyecto=None, gerencia=None, periodo=None)` → ingreso, costo, margen %
- `informe_variacion(gerencia=None)` → la tabla del caso: jun/jul, variación pp, semáforo 🟢🔴🟡, utilidad
- `evolucion(nivel='gerencia'|'proyecto', minimo_ingreso=0)` → margen por período, con filtro de
  materialidad (para no responder "COURIER" a la pregunta 1)
- `facturacion(periodo=None)` → ingreso neto clase 4 (crédito − débito), separando notas crédito
- `desglose_costos(proyecto, periodo, n=15)` → top cuentas de costo con nombre
- `novedades(proyecto=None, periodo=None)` → ausencias + conceptos de nómina del proyecto
- `retroactivos(periodo)` → nómina con `Period ≠ PeriodPayment` + asientos cuya `Observation`
  menciona un mes anterior (regex de meses)
- `proyeccion_margen(gerencia, metodo='tendencia')` → extrapola con **supuesto declarado** (para
  la pregunta "¿Warehouse recupera en agosto?")

El agente recibe **solo las firmas y los docstrings**, no el código — reduce el contexto de
inferencia y por tanto el costo (KGMON: 5.011 → 1.870 chars).

### Reglas del system prompt (lo que hace que suene a analista senior)

1. **Nunca calcules mentalmente.** Todo número sale de `ejecutar_python`. Si no lo ejecutaste, no
   lo afirmas.
2. **Declara la convención** cuando la pregunta es ambigua (base de cálculo, período, si se
   incluyen notas crédito) en una línea, antes del número.
3. **Materialidad**: un margen que salta 37 pp sobre $4 millones no es "la mejor evolución" de una
   empresa que factura $24.500 millones al mes. Dilo.
4. **Causa, no solo cifra.** Ante una variación, cruza contra nómina y ausencias y nombra las
   novedades concretas.
5. **Proyecciones**: declara el supuesto y da rango, no un número falsamente preciso.
6. **Si el dato no alcanza, dilo.** "No determinable con la información disponible" es una
   respuesta válida y mejor que inventar.
7. Formato: respuesta directa primero, evidencia después, en español, cifras en COP con separador
   de miles.

### Economía por respuesta

- Modelo por defecto **`claude-haiku-4-5-20251001`** ($1/$5 por MTok). Variable `MODELO` en el
  entorno lo cambia a `claude-sonnet-5` si en pruebas el razonamiento causal se queda corto.
- `cache_control` con `"ttl": "1h"` sobre el bloque estático (system + manual + firmas). La 1ª
  pregunta paga 1,25x; las siguientes leen a **0,1x**.
- Máximo 6 turnos de tool-use por pregunta (corta bucles y acota el gasto).
- Cada respuesta reporta su costo real en USD desde `usage`, sumando por separado
  `input_tokens` + `cache_creation_input_tokens`×1,25 + `cache_read_input_tokens`×0,1 + `output_tokens`
  (la API **no** incluye los tokens de caché dentro de `input_tokens` — doble conteo es el bug
  clásico de esta instrumentación).

---

## Plan de ejecución (orden estricto, ~60 min)

| Min | Paso | Criterio de "listo" |
|---|---|---|
| 0-15 | `analista/etl.py` + correrlo | Imprime ingreso/costo/margen por gerencia y **cuadra** con los números de la tabla de arriba. Las 2 filas rotas reparadas. |
| 15-22 | `analista/helper.py` + `verificar.py` | `python analista/verificar.py` imprime respuesta a las 7 preguntas del caso sin excepciones |
| 22-30 | `analista/sandbox.py` | `echo "print(helper.rentabilidad(gerencia='WAREHOUSE'))" \| python analista/sandbox.py` devuelve JSON válido |
| 30-45 | `web/server.mjs` | `node web/server.mjs` levanta en `http://localhost:3000` y responde una pregunta de punta a punta con costo impreso |
| 45-55 | `web/public/index.html` | Chat funcional con streaming + tabla del informe renderizada |
| 55-60 | Prueba de las 7 preguntas | Las 7 responden con cifras que coinciden con `verificar.py` |

**Punto de corte**: si a los 45 min el chat no funciona, se congela el panel del informe (queda
como tabla estática desde `informe.json`) y todo el tiempo restante va al agente.

---

## Verificación

1. **Los números antes que el LLM.** `python analista/verificar.py` — imprime los valores gold de
   las 7 preguntas calculados solo con pandas. Si estos están mal, nada más importa.
   El control cruzado: la suma de ingresos por gerencia debe dar **$24.553 millones en julio**
   y el margen de WAREHOUSE en julio **13,8%**.
2. **El sandbox aislado.** Ejecutar código con error a propósito y confirmar que devuelve
   `{ok: false, error: ...}` sin tumbar el servidor.
3. **Punta a punta en la web.** Levantar `node web/server.mjs`, abrir `localhost:3000` y correr las
   7 preguntas del `Preguntas.docx`, comparando contra la salida de `verificar.py`.
4. **Prueba de "agéntico"** (lo que va a hacer el jurado): preguntar algo **no previsto**, p.ej.
   *"¿cuánto se pagó en horas extra festivas en Warehouse en julio y qué peso tiene sobre su costo?"*.
   Debe escribir Python nuevo y responder — no decir "no tengo esa función".
5. **Prueba de costo.** Confirmar en la UI que la 2ª pregunta en adelante muestra
   `cache_read_input_tokens > 0` y un costo por respuesta de un orden de centavos.
6. **Prueba de honestidad.** Preguntar por agosto de 2026 (no hay datos) → debe responder que no
   es determinable y explicar qué supuesto haría falta, no inventar.

## Requisito previo del usuario

Antes de arrancar el servidor:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Hoy no está definida en el entorno. `server.mjs` debe detectarlo y decirlo en la primera línea,
no fallar con un 401 oscuro.
