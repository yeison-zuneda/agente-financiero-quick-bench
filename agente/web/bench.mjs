// Corre las 7 preguntas del Quick Golden Bench y deja la entrega lista:
//
//   bench/answers/qNN.json          respuesta (contrato del submit.py)
//   bench/traces/qNN.events.jsonl   traza en formato stream-json de la API
//
//   node web/bench.mjs              las 7
//   node web/bench.mjs 1 5          solo esas, conserva el resto
//
// Despues:  python submit.py --team TU-EQUIPO --model <el mismo MODELO>
//
// Por que el formato de traza importa: el evaluador convierte events.jsonl a
// pasos. Una traza con esquema propio se marca "no convertible" y la pregunta
// queda con tope 0.50. Aqui se emite la forma que produce Claude Code con
// --output-format stream-json: system/init, assistant con content blocks reales
// (ids de tool_use de la API), user con tool_result, y result con total_cost_usd.

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

import { iniciar, cerrar, responder, MODELO, RAIZ } from "./agente.mjs";

const BENCH = join(RAIZ, "bench");
const DIR_ANS = join(BENCH, "answers");
const DIR_TRZ = join(BENCH, "traces");

// El orden es el del Caso Financiero. Coincide con los pesos publicados: q02 y
// q03 (la prescriptiva y la proyeccion) pesan menos en "resultado" porque no
// tienen una cifra unica contra la cual comparar.
const PREGUNTAS = [
  "¿Qué línea en los tres meses ha tenido la mejor evolución en rentabilidad y esto a qué se debe?",
  "¿Para mejorar la rentabilidad de la línea de Warehouse qué debemos hacer?",
  "¿Warehouse va a recuperar su rentabilidad en agosto?",
  "¿Cuál fue la facturación real de cada mes?",
  "¿Qué parte del gasto de un mes es realmente de ese mes, y qué parte es un ajuste retroactivo de un mes anterior?",
  "¿Indícame cuáles fueron las novedades presentadas en el proyecto 594 porque tenemos variación en la rentabilidad entre junio y julio?",
  "¿Indícame cuáles fueron las novedades presentadas en el proyecto 600 porque tenemos variación en la rentabilidad entre junio y julio?",
];

// Lo que el evaluador premia y el prompt normal no cubre:
//  - RESULTADO: exige las cifras en un objeto, no solo en prosa.
//  - VERIFICACION: exige que el agente valide su propio numero ANTES de responder.
//  - TRANSPARENCIA: exige supuestos y limites explicitos.
const EXTRA = `
# CONTRATO DE SALIDA (obligatorio en este modo)

## Antes de responder: VERIFICA
Tu ultima consulta antes de responder debe RECALCULAR la cifra principal por una via
distinta a la que usaste, e imprimir ambas para compararlas. Ejemplos:
- si sacaste el margen con \`helper.rentabilidad\`, recalculalo agregando \`hechos\` a mano;
- si usaste \`helper.facturacion\`, recalculalo con \`hechos[hechos.Clase=='4']\`.
Imprime algo como: \`VERIFICACION metodo_1=... metodo_2=... diferencia=...\`.
Si las dos vias no coinciden, NO respondas la cifra: investiga la diferencia primero.
Esta consulta de verificacion es obligatoria y se evalua.

## Tu mensaje final
Primero la respuesta en prosa, como siempre. Y AL FINAL, un bloque \`\`\`json con
exactamente estas claves:

\`\`\`json
{
  "answer":      { ...cifras estructuradas... },
  "summary":     "una o dos frases: lo que le dirias al CFO, con la cifra dentro",
  "method":      "como lo calculaste y como lo verificaste, en 2-4 frases",
  "conventions": ["cada convencion que elegiste, p.ej. 'margen = (Ingreso-Costo)/Ingreso, costo = clases 6 y 7'"],
  "caveats":     ["cada supuesto, limite o cosa que NO pudiste calcular"]
}
\`\`\`

Reglas del bloque:
- \`answer\` lleva NUMEROS CRUDOS, no texto formateado: 24553077702.62, no "$24.553 millones".
  Margenes en fraccion (0.138), variaciones en puntos porcentuales como numero (-3.6).
  Usa claves descriptivas en minuscula con guion bajo. Incluye SIEMPRE la cifra que
  contesta la pregunta, y las que la sostienen.
- Si la pregunta no tiene una cifra unica (una recomendacion, una proyeccion), \`answer\`
  igual lleva las cifras que sustentan la conclusion, mas la conclusion como campo:
  p.ej. {"recupera_en_agosto": false, "margen_julio": 0.138, "proyeccion_agosto_tendencia": 0.102}.
- \`caveats\` nunca va vacio: siempre hay un supuesto que declarar.
- El bloque json va al final y no lleva comentarios.
`.trim();

const ESQUEMA_VACIO = (qid, motivo) => ({
  question_id: qid,
  answer: null,
  summary: motivo,
  caveats: ["La corrida no produjo un bloque JSON legible; la cifra no se declara para no inventarla."],
});

function extraerJson(texto) {
  // Se busca el ULTIMO bloque json: el modelo a veces muestra ejemplos antes.
  const bloques = [...texto.matchAll(/```json\s*([\s\S]*?)```/g)];
  for (let i = bloques.length - 1; i >= 0; i--) {
    try {
      const o = JSON.parse(bloques[i][1].trim());
      if (o && typeof o === "object" && !Array.isArray(o)) return o;
    } catch { /* sigue con el anterior */ }
  }
  return null;
}

function limpiarProsa(texto) {
  return texto.replace(/```json[\s\S]*?```/g, "").trim();
}

/** Construye la traza en el formato stream-json que el evaluador sabe convertir. */
function construirTraza({ sesion, eventos, modelo, prosa, costoUSD, ms, turnos }) {
  const lineas = [];
  const push = (o) => lineas.push(JSON.stringify(o));

  push({
    type: "system", subtype: "init", session_id: sesion, model: modelo,
    tools: ["ejecutar_python"], permissionMode: "default", cwd: RAIZ,
  });

  const total = { input_tokens: 0, output_tokens: 0,
    cache_creation_input_tokens: 0, cache_read_input_tokens: 0 };

  for (const ev of eventos) {
    if (ev.tipo === "api") {
      const r = ev.datos.resp || {};
      const u = r.usage || {};
      for (const k of Object.keys(total)) total[k] += u[k] || 0;
      push({
        type: "assistant", session_id: sesion,
        message: {
          id: r.id, type: "message", role: "assistant", model: r.model || modelo,
          content: r.content || [], stop_reason: r.stop_reason ?? null,
          stop_sequence: r.stop_sequence ?? null, usage: u,
        },
      });
    }
    if (ev.tipo === "salida") {
      push({
        type: "user", session_id: sesion,
        message: {
          role: "user",
          content: [{
            type: "tool_result",
            tool_use_id: ev.datos.tool_use_id,
            content: ev.datos.salida,
            is_error: !ev.datos.ok,
          }],
        },
      });
    }
  }

  push({
    type: "result", subtype: "success", is_error: false,
    duration_ms: ms, duration_api_ms: ms, num_turns: turnos,
    result: prosa, session_id: sesion,
    total_cost_usd: Number(costoUSD.toFixed(6)),
    usage: total,
  });
  return lineas.join("\n") + "\n";
}

async function main() {
  console.log("\nQuick Golden Bench — generando entrega\n");
  await mkdir(DIR_ANS, { recursive: true });
  await mkdir(DIR_TRZ, { recursive: true });
  await iniciar();

  if (!process.env.ANTHROPIC_API_KEY) {
    console.error("\n  Falta ANTHROPIC_API_KEY. Revisa el archivo .env.\n");
    cerrar(); process.exit(1);
  }

  const pedidas = process.argv.slice(2).map(Number).filter((n) => n >= 1 && n <= 7);
  let costoTotal = 0, sinJson = [];
  const t0 = Date.now();

  for (let i = 0; i < PREGUNTAS.length; i++) {
    const n = i + 1;
    const qid = "q0" + n;
    const fAns = join(DIR_ANS, `${qid}.json`);
    if (pedidas.length && !pedidas.includes(n)) {
      console.log(`[${qid}] ${existsSync(fAns) ? "(se conserva la anterior)" : "(omitida: aún no existe)"}`);
      continue;
    }
    process.stdout.write(`[${qid}] ${PREGUNTAS[i].slice(0, 58)}…\n`);

    const eventos = [];
    let prosa = "", costo = 0, turnos = 0, verifico = false;
    const inicio = Date.now();

    await responder(PREGUNTAS[i], (tipo, datos) => {
      eventos.push({ tipo, datos });
      if (tipo === "codigo" && /VERIFICACION|verificacion|verificación/i.test(datos.codigo || "")) {
        verifico = true;
      }
      if (tipo === "salida") {
        process.stdout.write(`       consulta ${eventos.filter(e => e.tipo === "salida").length} ` +
          `${datos.ok ? "ok" : "ERROR"}\n`);
      }
      if (tipo === "respuesta") prosa = datos.texto;
      if (tipo === "fin") { costo = datos.costoUSD; turnos = datos.turnos; }
    }, { extra: EXTRA });

    const ms = Date.now() - inicio;
    costoTotal += costo;

    const bloque = extraerJson(prosa);
    let respuesta;
    if (bloque) {
      respuesta = { question_id: qid };
      if ("answer" in bloque) respuesta.answer = bloque.answer ?? null;
      else respuesta.answer = null;
      respuesta.summary = String(bloque.summary || "").trim() ||
        limpiarProsa(prosa).slice(0, 500);
      for (const k of ["method", "conventions", "caveats"]) {
        if (bloque[k] == null) continue;
        if (k === "method") respuesta.method = String(bloque[k]);
        else if (Array.isArray(bloque[k])) respuesta[k] = bloque[k].map(String).filter(Boolean);
      }
    } else {
      sinJson.push(qid);
      respuesta = ESQUEMA_VACIO(qid, limpiarProsa(prosa).slice(0, 800) || "sin respuesta");
    }
    // El codigo ejecutado es la evidencia; va tambien en la respuesta.
    const codigos = eventos.filter((e) => e.tipo === "codigo").map((e) => e.datos.codigo);
    if (codigos.length) respuesta.code = codigos.join("\n\n# ---\n\n");
    if (!respuesta.caveats?.length) {
      respuesta.caveats = ["Solo hay datos de mayo, junio y julio de 2026."];
    }

    await writeFile(fAns, JSON.stringify(respuesta, null, 2) + "\n", "utf-8");
    await writeFile(join(DIR_TRZ, `${qid}.events.jsonl`),
      construirTraza({ sesion: randomUUID(), eventos, modelo: MODELO,
        prosa: limpiarProsa(prosa), costoUSD: costo, ms, turnos }), "utf-8");

    console.log(`       ${codigos.length} consulta(s) · ${(ms / 1000).toFixed(1)} s · ` +
      `US$${costo.toFixed(4)} · json ${bloque ? "ok" : "FALTA"} · ` +
      `verificacion ${verifico ? "si" : "NO"}`);
  }

  console.log(`\n  answers -> ${DIR_ANS}`);
  console.log(`  traces  -> ${DIR_TRZ}`);
  console.log(`  modelo declarado: ${MODELO}`);
  console.log(`  costo de esta corrida: US$${costoTotal.toFixed(4)}  ·  ` +
    `${((Date.now() - t0) / 1000).toFixed(0)} s`);
  if (sinJson.length) console.log(`  ⚠ sin bloque json: ${sinJson.join(", ")} (answer queda null)`);
  console.log(`\n  Entregar:  python submit.py --team TU-EQUIPO --model ${MODELO} \\`);
  console.log(`               --answers bench/answers --traces bench/traces\n`);
  cerrar();
  process.exit(0);
}

main().catch((e) => { console.error("\n  FALLO:", e.message, "\n"); cerrar(); process.exit(1); });
