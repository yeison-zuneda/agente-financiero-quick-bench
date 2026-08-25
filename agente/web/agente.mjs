// Motor del analista: sandbox local + prompt + agent loop.
// Lo usan tanto `server.mjs` (el chat de la pagina) como `responder.mjs` (el
// generador de las 7 respuestas del caso). Separarlo evita que las dos copias
// del prompt se desincronicen.
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";
import { createInterface } from "node:readline";

const AQUI = dirname(fileURLToPath(import.meta.url));
const RAIZ = join(AQUI, "..");
const DATOS = join(RAIZ, "datos");
const PUBLICO = join(AQUI, "public");
const PUERTO = Number(process.env.PUERTO || 3000);

// Carga .env sin dependencias, para no tener que exportar la key en cada terminal.
// Va antes de MODELO porque esa constante se evalua al importar el modulo.
// Lo que ya este en el entorno gana sobre el archivo.
for (const dir of [RAIZ, join(RAIZ, "..", "..", "..")]) {
  const archivo = join(dir, ".env");
  if (!existsSync(archivo)) continue;
  for (const linea of (await readFile(archivo, "utf-8")).split(/\r?\n/)) {
    const m = linea.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    const valor = m[2].trim().replace(/^["']|["']$/g, "");
    if (valor && !process.env[m[1]]) process.env[m[1]] = valor;
  }
  console.log(`  .env cargado desde ${archivo}`);
}

const MODELO = process.env.MODELO || "claude-haiku-4-5-20251001";
// Techo de consultas por pregunta. Es una salvaguarda de costo, no un presupuesto
// de analisis: una pregunta causal legitima ("por que cayo el margen") necesita
// mirar rentabilidad, cuentas, nomina y ausencias, y eso ya son 4-6 consultas.
// En el ultimo turno se llama SIN herramientas, asi que siempre hay respuesta.
const MAX_TURNOS = Number(process.env.MAX_TURNOS || 14);

// USD por millon de tokens. Verificar el dia del evento en platform.claude.com.
const TARIFAS = {
  "claude-haiku-4-5-20251001": { entrada: 1.0, salida: 5.0 },
  "claude-sonnet-5": { entrada: 2.0, salida: 10.0 },
  "claude-opus-5": { entrada: 5.0, salida: 25.0 },
};

// ---------------------------------------------------------------- sandbox ---
// Un solo proceso Python vivo. Cargar los 5 parquet toma ~2 s; con 3-6 llamadas
// por pregunta, arrancar un proceso por llamada costaria ~12 s de puro arranque.
let py = null;
let pendientes = [];

function arrancarSandbox() {
  return new Promise((resolve, reject) => {
    py = spawn("python", [join(RAIZ, "analista", "sandbox.py")], {
      cwd: RAIZ,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });
    let listo = false;
    const rl = createInterface({ input: py.stdout });
    rl.on("line", (linea) => {
      let msg;
      try {
        msg = JSON.parse(linea);
      } catch {
        return;
      }
      if (!listo && msg.listo) {
        listo = true;
        console.log(`  sandbox listo (${msg.filas_hechos.toLocaleString("es-CO")} asientos en memoria)`);
        return resolve();
      }
      const siguiente = pendientes.shift();
      if (siguiente) siguiente(msg);
    });
    py.stderr.on("data", (d) => {
      const t = String(d).trim();
      if (t && !listo) console.error("  [python] " + t);
    });
    py.on("exit", (c) => {
      console.error(`  sandbox termino con codigo ${c}`);
      py = null;
    });
    setTimeout(() => {
      if (!listo) reject(new Error("el sandbox no respondio en 60 s"));
    }, 60000);
  });
}

function ejecutarPython(codigo, reset = false) {
  return new Promise((resolve) => {
    if (!py) return resolve({ ok: false, error: "el sandbox de Python no esta corriendo" });
    pendientes.push(resolve);
    py.stdin.write(JSON.stringify({ codigo, reset }) + "\n");
  });
}

// ------------------------------------------------------------ prompt base ---
let MANUAL = "";
let FIRMAS = "";

const SISTEMA = (extra = "") => `Eres un analista financiero senior de Quick Logistica. Respondes preguntas
sobre la operacion de mayo, junio y julio de 2026 con el rigor de alguien que firma el informe.

# Tu unica herramienta
\`ejecutar_python\`: un interprete Python con pandas donde YA estan cargados los DataFrames
(\`hechos\`, \`rentabilidad_df\`, \`informe_df\`, \`nomina\`, \`ausencias\`) y el modulo \`helper\`.
Usa print() para ver resultados; lo que no imprimes no lo ves.

# Reglas que no se negocian

1. NUNCA calcules de cabeza. Todo numero que afirmes tiene que haber salido de una ejecucion.
   Si no lo ejecutaste, no lo dices. Sin excepciones.
2. Usa las funciones de \`helper\` antes de escribir agregaciones a mano: estan verificadas por
   doble via. Escribe pandas propio solo cuando ninguna funcion cubra la pregunta.
3. MATERIALIDAD. La compania factura ~$24.500 millones COP al mes. Una unidad que mueve $4
   millones no es "la mejor evolucion" aunque su margen salte 37 puntos. Cuando un porcentaje
   grande venga de una base minuscula, dilo explicitamente y da la respuesta que un gerente
   podria usar.
4. CAUSA, no solo cifra. Ante una variacion, no te quedes en el cuanto: busca el porque cruzando
   cuentas de costo (\`desglose_costos\`), novedades de personal (\`novedades\`) y conceptos de
   nomina (\`conceptos_nomina\`). Nombra la cuenta, el concepto o la novedad concreta.
5. DECLARA LA CONVENCION cuando la pregunta sea ambigua: que periodo tomaste, si el margen es
   sobre ingreso neto, que entra en costo. Una linea basta, antes del numero.
6. PROYECCIONES: solo existen mayo, junio y julio de 2026. Agosto NO es un dato. Si te preguntan
   por agosto usa \`proyeccion_margen\`, declara el supuesto y da un rango, nunca un numero seco.
7. Si los datos no alcanzan, dilo. "No es determinable con esta informacion, haria falta X" es
   una respuesta profesional; inventar no lo es.
8. Cuando te pidan una recomendacion, sustentala en la cifra que la respalda y ordena por
   impacto en pesos, no por facilidad.

# Como consultar (esto decide si alcanzas a responder)
Tienes un techo de consultas por pregunta. Gastarlo en un numero por consulta te deja sin
presupuesto antes de llegar a la causa.

- AGRUPA. Un solo bloque de codigo puede calcular varias cosas: pide la evolucion, el
  desglose de costos y las novedades en el MISMO print. Piensa "que necesito ver todo junto
  para responder", no "cual es el siguiente dato".
- Consulta 1: el panorama que responde el QUE (casi siempre \`evolucion\` o \`rentabilidad\`).
  Consulta 2: el detalle que responde el POR QUE (\`desglose_costos\` + \`novedades\` +
  \`conceptos_nomina\` juntos). Con dos o tres consultas bien armadas se responde casi todo.
- Imprime agregados, no tablas crudas: \`.head(10)\`, \`.to_string(index=False)\`, columnas
  elegidas. La salida se corta a 6.000 caracteres y un volcado grande te hace repetir.
- Si una consulta falla, LEE el error y corrige en la siguiente; no repitas el mismo codigo.
- Cuando ya tengas con que responder, RESPONDE. No sigas consultando por completitud.

# Responde al NIVEL que te preguntaron
"Linea" = linea de negocio = una de las 5 gerencias operativas. "Proyecto" = uno de los 468
centros de costo. Ver el GLOSARIO del manual. Responder con un proyecto a una pregunta sobre
lineas es un error grave: la cifra queda bien pero contesta otra pregunta.

# Formato
Espanol. La respuesta directa PRIMERO, en una o dos frases.
Empieza por la conclusion, nunca por tu proceso: nada de "Perfecto", "Ahora tengo el cuadro
completo", "Dejame resumir". El lector quiere el hallazgo, no el relato de como llegaste.
No cierres preguntandole cosas al usuario. Si falta informacion para ser concluyente, di QUE
falta y por que cambiaria la respuesta, y ahi termina. Despues la evidencia: cifras en pesos
colombianos con separador de miles ($1.234.567), porcentajes con un decimal, variaciones de margen
en puntos porcentuales (pp). Usa tablas markdown cuando compares mas de dos cosas. Cierra con el
"y esto que significa" solo si aporta. No repitas el codigo que ejecutaste; el usuario lo ve aparte.

# Manual de datos
${MANUAL}

# Firmas de helper
${FIRMAS}
${extra}`;

const HERRAMIENTAS = [
  {
    name: "ejecutar_python",
    description:
      "Ejecuta codigo Python (pandas) contra los datos financieros ya cargados en memoria. " +
      "Los DataFrames hechos, rentabilidad_df, informe_df, nomina y ausencias, y el modulo " +
      "helper, ya estan disponibles: no los importes ni los leas de disco. Usa print() para " +
      "ver resultados. Devuelve stdout truncado a 6.000 caracteres, asi que filtra y agrega " +
      "antes de imprimir en vez de volcar tablas enteras.",
    input_schema: {
      type: "object",
      properties: {
        codigo: { type: "string", description: "Codigo Python a ejecutar. Debe imprimir con print()." },
      },
      required: ["codigo"],
    },
  },
];

function costoDe(uso, modelo) {
  const t = TARIFAS[modelo] || TARIFAS["claude-haiku-4-5-20251001"];
  const ent = uso.input_tokens || 0;
  const esc = uso.cache_creation_input_tokens || 0; // escritura de cache 1h = 2x
  const lec = uso.cache_read_input_tokens || 0; // lectura de cache = 0,1x
  const sal = uso.output_tokens || 0;
  // input_tokens NO incluye los tokens de cache: se suman aparte o se cuenta doble.
  const usd =
    (ent * t.entrada + esc * t.entrada * 2.0 + lec * t.entrada * 0.1 + sal * t.salida) / 1e6;
  return { usd, entrada: ent, escrituraCache: esc, lecturaCache: lec, salida: sal };
}

async function llamarClaude(mensajes, { conHerramientas = true, extra = "" } = {}) {
  const cuerpo = {
    model: MODELO,
    max_tokens: 4000,
    system: [
      {
        type: "text",
        text: SISTEMA(extra),
        // El bloque estatico se cachea 1 h: la 1a pregunta paga 2x, el resto lee a 0,1x.
        cache_control: { type: "ephemeral", ttl: "1h" },
      },
    ],
    messages: mensajes,
  };
  // Sin herramientas el modelo no puede pedir otra consulta: tiene que concluir.
  if (conHerramientas) cuerpo.tools = HERRAMIENTAS;

  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": process.env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(cuerpo),
  });
  if (!r.ok) {
    const cuerpo = await r.text();
    throw new Error(`API ${r.status}: ${cuerpo.slice(0, 400)}`);
  }
  return r.json();
}

// -------------------------------------------------------------- agent loop ---
async function responder(pregunta, emitir, { extra = "" } = {}) {
  const mensajes = [{ role: "user", content: pregunta }];
  let costoTotal = 0;
  const uso = { entrada: 0, escrituraCache: 0, lecturaCache: 0, salida: 0 };
  let primeraLlamada = true;

  for (let turno = 1; turno <= MAX_TURNOS; turno++) {
    const ultimo = turno === MAX_TURNOS;
    if (ultimo) {
      // Se agoto el presupuesto de consultas: en vez de devolver un error, se le
      // pide concluir con la evidencia que ya reunio, declarando lo que falto.
      mensajes.push({
        role: "user",
        content:
          "Se agoto el presupuesto de consultas. No hagas mas: responde AHORA la pregunta " +
          "con la evidencia que ya reuniste. Si algo quedo sin verificar, dilo en una linea " +
          "al final en vez de omitirlo.",
      });
      emitir("aviso", {
        texto: `Alcancé el techo de ${MAX_TURNOS} consultas; cierro con lo que ya calculé.`,
      });
    }
    const resp = await llamarClaude(mensajes, { conHerramientas: !ultimo, extra });
    const c = costoDe(resp.usage || {}, MODELO);
    // La respuesta cruda se expone para poder reconstruir una traza fiel
    // (ids de tool_use reales, usage por turno, modelo). La pagina la ignora.
    emitir("api", { resp, modelo: MODELO, turno, costoUSD: c.usd });
    costoTotal += c.usd;
    uso.entrada += c.entrada;
    uso.escrituraCache += c.escrituraCache;
    uso.lecturaCache += c.lecturaCache;
    uso.salida += c.salida;

    if (primeraLlamada) {
      emitir("cache", {
        leidos: c.lecturaCache,
        escritos: c.escrituraCache,
        estado: c.lecturaCache > 0 ? "reutilizada" : "creada",
      });
      primeraLlamada = false;
    }

    const texto = (resp.content || []).filter((b) => b.type === "text").map((b) => b.text).join("");
    const usos = (resp.content || []).filter((b) => b.type === "tool_use");

    if (resp.stop_reason !== "tool_use" || usos.length === 0) {
      emitir("respuesta", { texto });
      emitir("fin", { costoUSD: costoTotal, turnos: turno, uso, modelo: MODELO });
      return;
    }

    if (texto.trim()) emitir("pensando", { texto: texto.trim() });
    mensajes.push({ role: "assistant", content: resp.content });

    const resultados = [];
    for (const u of usos) {
      const codigo = u.input?.codigo || "";
      emitir("codigo", { codigo, turno });
      const r = await ejecutarPython(codigo, turno === 1);
      const salida = r.ok ? r.stdout : `ERROR:\n${r.error}\n${r.stdout || ""}`;
      emitir("salida", { salida, ok: !!r.ok, tool_use_id: u.id });
      resultados.push({
        type: "tool_result",
        tool_use_id: u.id,
        content: salida,
        is_error: !r.ok,
      });
    }
    mensajes.push({ role: "user", content: resultados });
  }
  // Inalcanzable: el turno MAX_TURNOS corre sin herramientas y siempre retorna arriba.
}


// --------------------------------------------------------------- exports ---
/** Carga el manual de datos y las firmas de helper, y arranca el sandbox. */
export async function iniciar({ silencioso = false } = {}) {
  for (const f of ["MANUAL_DATOS.md", "informe.json", "hechos.parquet"]) {
    if (!existsSync(join(DATOS, f))) {
      throw new Error(`Falta datos/${f}. Corre primero:  python analista/etl.py`);
    }
  }
  MANUAL = await readFile(join(DATOS, "MANUAL_DATOS.md"), "utf-8");
  FIRMAS = (await readFile(join(RAIZ, "analista", "helper.py"), "utf-8"))
    .split('FIRMAS = """')[1]?.split('""".strip()')[0]?.trim() || "";
  if (!silencioso) {
    console.log(`  modelo: ${MODELO}`);
    console.log(
      process.env.ANTHROPIC_API_KEY
        ? "  ANTHROPIC_API_KEY: detectada"
        : '  ANTHROPIC_API_KEY: NO DETECTADA -> revisa el archivo .env'
    );
  }
  await arrancarSandbox();
}

export function cerrar() { if (py) py.kill(); }
export { responder, MODELO, MAX_TURNOS, RAIZ, DATOS, ejecutarPython };
