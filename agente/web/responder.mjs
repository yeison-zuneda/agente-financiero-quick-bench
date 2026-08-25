// Corre las 7 preguntas del Caso Financiero contra el agente y guarda el
// resultado en datos/respuestas.json, que alimenta la pestana "Respuestas"
// del dashboard.
//
//   node web/responder.mjs            -> las 7 preguntas
//   node web/responder.mjs 1 4        -> solo la 1 y la 4 (reusa las demas)
//
// Guarda tambien el codigo que ejecuto y el costo de cada respuesta: es la
// evidencia de que las cifras salieron de los datos y no de la memoria del modelo.

import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { iniciar, cerrar, responder, MODELO, DATOS } from "./agente.mjs";

const PREGUNTAS = [
  "¿Qué línea en los tres meses ha tenido la mejor evolución en rentabilidad y esto a qué se debe?",
  "¿Para mejorar la rentabilidad de la línea de Warehouse qué debemos hacer?",
  "¿Warehouse va a recuperar su rentabilidad en agosto?",
  "¿Cuál fue la facturación real de cada mes?",
  "¿Qué parte del gasto de un mes es realmente de ese mes, y qué parte es un ajuste retroactivo de un mes anterior?",
  "¿Indícame cuáles fueron las novedades presentadas en el proyecto 594 porque tenemos variación en la rentabilidad entre junio y julio?",
  "¿Indícame cuáles fueron las novedades presentadas en el proyecto 600 porque tenemos variación en la rentabilidad entre junio y julio?",
];

const SALIDA = join(DATOS, "respuestas.json");

function barra(i, n) {
  return `[${String(i).padStart(2)}/${n}]`;
}

async function main() {
  console.log("\nGenerando respuestas del Caso Financiero\n");
  await iniciar();

  if (!process.env.ANTHROPIC_API_KEY) {
    console.error("\n  Falta ANTHROPIC_API_KEY. Revisa el archivo .env.\n");
    cerrar();
    process.exit(1);
  }

  // Permite regenerar solo algunas sin perder las demas ni volver a pagarlas.
  const pedidas = process.argv.slice(2).map(Number).filter((n) => n >= 1 && n <= PREGUNTAS.length);
  let previas = [];
  if (existsSync(SALIDA)) {
    try {
      previas = JSON.parse(await readFile(SALIDA, "utf-8")).respuestas || [];
    } catch { /* archivo corrupto: se regenera entero */ }
  }

  const resultados = [];
  let costoTotal = 0;
  const t0 = Date.now();

  for (let i = 0; i < PREGUNTAS.length; i++) {
    const n = i + 1;
    if (pedidas.length && !pedidas.includes(n)) {
      const vieja = previas.find((r) => r.n === n);
      if (vieja) { resultados.push(vieja); console.log(`${barra(n, PREGUNTAS.length)} (se conserva la anterior)`); continue; }
    }
    const pregunta = PREGUNTAS[i];
    process.stdout.write(`${barra(n, PREGUNTAS.length)} ${pregunta.slice(0, 62)}…\n`);

    const pasos = [];
    let texto = "", costo = 0, turnos = 0, uso = {}, aviso = null, error = null;
    const inicio = Date.now();

    await responder(pregunta, (evento, datos) => {
      if (evento === "codigo") pasos.push({ codigo: datos.codigo, salida: null, ok: null });
      if (evento === "salida" && pasos.length) {
        const p = pasos[pasos.length - 1];
        p.salida = datos.salida; p.ok = datos.ok;
        process.stdout.write(`        consulta ${pasos.length} ${datos.ok ? "ok" : "ERROR"}\n`);
      }
      if (evento === "respuesta") texto = datos.texto;
      if (evento === "aviso") aviso = datos.texto;
      if (evento === "error") error = datos.mensaje;
      if (evento === "fin") { costo = datos.costoUSD; turnos = datos.turnos; uso = datos.uso || {}; }
    });

    const seg = (Date.now() - inicio) / 1000;
    costoTotal += costo;
    resultados.push({
      n, pregunta, respuesta: texto, pasos, consultas: pasos.length,
      turnos, costoUSD: costo, segundos: Number(seg.toFixed(1)),
      aviso, error, modelo: MODELO, generado: new Date().toISOString(), uso,
    });
    console.log(`        ${pasos.length} consulta(s) · ${seg.toFixed(1)} s · US$${costo.toFixed(4)}` +
      (error ? `  ERROR: ${error}` : ""));
  }

  const payload = {
    generado: new Date().toISOString(),
    modelo: MODELO,
    costoTotalUSD: costoTotal,
    segundosTotal: Number(((Date.now() - t0) / 1000).toFixed(1)),
    respuestas: resultados.sort((a, b) => a.n - b.n),
  };
  await writeFile(SALIDA, JSON.stringify(payload, null, 2), "utf-8");

  console.log(`\n  ${resultados.length} respuestas -> datos/respuestas.json`);
  console.log(`  costo total: US$${costoTotal.toFixed(4)}  ·  ` +
    `promedio US$${(costoTotal / resultados.length).toFixed(4)} por respuesta`);
  console.log(`  tiempo total: ${payload.segundosTotal} s\n`);
  cerrar();
  process.exit(0);
}

main().catch((e) => { console.error("\n  FALLO:", e.message, "\n"); cerrar(); process.exit(1); });
