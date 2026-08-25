// Analista Financiero Agentico - servidor local.
// Node puro: cero dependencias de npm, cero build. Solo `node web/server.mjs`.
//
// Lo unico que sale de esta maquina es la pregunta y los resultados agregados que
// el agente decide mirar. Los 51 MB de contable y las nominas se quedan aqui.
// El motor (sandbox + prompt + agent loop) vive en agente.mjs.

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";

import { iniciar, cerrar, responder, MODELO, DATOS } from "./agente.mjs";

const AQUI = dirname(fileURLToPath(import.meta.url));
const PUBLICO = join(AQUI, "public");
const PUERTO = Number(process.env.PUERTO || 3000);

// ------------------------------------------------------------------- HTTP ---
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };

const servidor = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PUERTO}`);

  if (req.method === "POST" && url.pathname === "/api/preguntar") {
    let cuerpo = "";
    for await (const t of req) cuerpo += t;
    let pregunta = "";
    try {
      pregunta = JSON.parse(cuerpo).pregunta || "";
    } catch {
      res.writeHead(400).end("json invalido");
      return;
    }
    res.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    const emitir = (evento, datos) =>
      res.write(`data: ${JSON.stringify({ evento, ...datos })}\n\n`);
    try {
      if (!process.env.ANTHROPIC_API_KEY) {
        throw new Error(
          "Falta ANTHROPIC_API_KEY. Cierra el servidor, corre " +
            '$env:ANTHROPIC_API_KEY = "sk-ant-..." y vuelve a arrancarlo.'
        );
      }
      await responder(pregunta, emitir);
    } catch (e) {
      emitir("error", { mensaje: String(e.message || e) });
      emitir("fin", { costoUSD: 0, turnos: 0 });
    }
    res.end();
    return;
  }

  // informe.json = la tabla del caso (junio vs julio).
  // series.json  = los tres meses por gerencia y por proyecto, para el dashboard.
  // respuestas.json lo genera `node web/responder.mjs` y puede no existir todavia.
  const ESTATICOS = {
    "/api/informe": "informe.json",
    "/api/series": "series.json",
    "/api/respuestas": "respuestas.json",
  };
  if (ESTATICOS[url.pathname]) {
    const ruta = join(DATOS, ESTATICOS[url.pathname]);
    if (!existsSync(ruta)) {
      res.writeHead(404, { "content-type": "application/json; charset=utf-8" })
        .end(JSON.stringify({ error: `falta datos/${ESTATICOS[url.pathname]}` }));
      return;
    }
    res.writeHead(200, { "content-type": "application/json; charset=utf-8" })
      .end(await readFile(ruta, "utf-8"));
    return;
  }

  const archivo = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  const ruta = join(PUBLICO, archivo);
  if (!ruta.startsWith(PUBLICO) || !existsSync(ruta)) {
    res.writeHead(404).end("no encontrado");
    return;
  }
  res.writeHead(200, { "content-type": MIME[extname(ruta)] || "text/plain; charset=utf-8" });
  res.end(await readFile(ruta));
});


// ------------------------------------------------------------------ arranque ---
console.log("\nAnalista Financiero Agentico - Quick Logistica\n");
await iniciar();

servidor.listen(PUERTO, () => {
  console.log(`\n  Abre  http://localhost:${PUERTO}\n`);
});

for (const s of ["SIGINT", "SIGTERM"]) {
  process.on(s, () => { cerrar(); process.exit(0); });
}
