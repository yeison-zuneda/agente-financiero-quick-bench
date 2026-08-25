"""Valida la entrega ANTES de mandarla al Quick Golden Bench.

Replica el contrato que aplica el `submit.py` oficial (schema Answer, pydantic
con extra='forbid') mas los chequeos de traza que se deducen del leaderboard:
una traza que el evaluador no pueda convertir a pasos deja la pregunta con tope
0.50, asi que aqui se verifica que sea stream-json legible y que reporte costo.

    python analista/validar_entrega.py
    python analista/validar_entrega.py --dir bench
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

QUESTION_IDS = ("q01", "q02", "q03", "q04", "q05", "q06", "q07")
REQUERIDAS = {"question_id", "answer", "summary"}
OPCIONALES = {"method", "code", "caveats", "conventions"}
PERMITIDAS = REQUERIDAS | OPCIONALES

fallos: list[str] = []
avisos: list[str] = []


def falla(msg: str) -> None:
    fallos.append(msg)
    print("  [FALLA] " + msg)


def aviso(msg: str) -> None:
    avisos.append(msg)
    print("  [AVISO] " + msg)


def ok(msg: str) -> None:
    print("  [OK]    " + msg)


def validar_respuesta(ruta: Path, qid: str) -> dict | None:
    try:
        obj = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        falla("{}: no es JSON valido ({})".format(ruta.name, e))
        return None
    if not isinstance(obj, dict):
        falla("{}: la raiz debe ser un objeto".format(ruta.name))
        return None

    desconocidas = set(obj) - PERMITIDAS
    if desconocidas:
        falla("{}: claves no permitidas {} (submit.py rechaza la entrega entera)".format(
            ruta.name, sorted(desconocidas)))
    faltan = REQUERIDAS - set(obj)
    if faltan:
        falla("{}: faltan claves obligatorias {}".format(ruta.name, sorted(faltan)))
    if obj.get("question_id") != qid:
        falla("{}: question_id={!r} no coincide con el nombre del archivo".format(
            ruta.name, obj.get("question_id")))
    if obj.get("answer") is not None and not isinstance(obj["answer"], dict):
        falla("{}: 'answer' debe ser objeto o null".format(ruta.name))
    if not isinstance(obj.get("summary"), str) or not obj.get("summary", "").strip():
        falla("{}: 'summary' debe ser texto no vacio".format(ruta.name))
    for k in ("method", "code"):
        if k in obj and not isinstance(obj[k], str):
            falla("{}: '{}' debe ser texto".format(ruta.name, k))
    for k in ("caveats", "conventions"):
        if k in obj and not (isinstance(obj[k], list) and all(isinstance(x, str) for x in obj[k])):
            falla("{}: '{}' debe ser lista de textos".format(ruta.name, k))

    # Calidad, no contrato: esto es lo que mueve el puntaje.
    if obj.get("answer") is None:
        aviso("{}: 'answer' es null -> pierdes el criterio RESULTADO "
              "(peso 3/10 en q01 y q04-q07)".format(ruta.name))
    else:
        no_num = [k for k, v in obj["answer"].items() if not isinstance(v, (int, float, bool))]
        if no_num:
            aviso("{}: claves de 'answer' que no son numero ni booleano: {} "
                  "(el evaluador compara cifras con tolerancia)".format(ruta.name, no_num[:6]))
        for k, v in obj["answer"].items():
            if isinstance(v, str) and any(s in v for s in ("$", "%", ".")):
                aviso("{}: answer['{}'] parece texto formateado, no numero crudo".format(
                    ruta.name, k))
    if not obj.get("caveats"):
        aviso("{}: sin 'caveats' -> pierdes TRANSPARENCIA".format(ruta.name))
    if not obj.get("conventions"):
        aviso("{}: sin 'conventions' -> pierdes TRANSPARENCIA".format(ruta.name))
    if not obj.get("method"):
        aviso("{}: sin 'method'".format(ruta.name))
    return obj


def validar_traza(ruta: Path, qid: str, modelo_esperado: str | None) -> str | None:
    """Devuelve el modelo visto en la traza, o None si la traza no sirve."""
    if not ruta.is_file():
        falla("{}: NO EXISTE -> esa pregunta queda con tope 0.50".format(ruta.name))
        return None
    if ruta.stat().st_size == 0:
        falla("{}: esta vacio -> submit.py lo rechaza".format(ruta.name))
        return None

    lineas = [l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]
    eventos = []
    for i, l in enumerate(lineas, 1):
        try:
            eventos.append(json.loads(l))
        except json.JSONDecodeError:
            falla("{}: la linea {} no es JSON -> traza no convertible".format(ruta.name, i))
            return None

    tipos = [e.get("type") for e in eventos]
    usos = [b for e in eventos if e.get("type") == "assistant"
            for b in (e.get("message", {}).get("content") or []) if b.get("type") == "tool_use"]
    resultados = [b for e in eventos if e.get("type") == "user"
                  for b in (e.get("message", {}).get("content") or []) if b.get("type") == "tool_result"]

    if not usos:
        falla("{}: no hay bloques tool_use -> 'la traza no tiene pasos convertibles', "
              "tope 0.50".format(ruta.name))
        return None
    ids_uso = {b.get("id") for b in usos}
    ids_res = {b.get("tool_use_id") for b in resultados}
    huerfanos = ids_res - ids_uso
    if huerfanos:
        falla("{}: hay tool_result sin su tool_use ({}) -> pasos incompletos".format(
            ruta.name, len(huerfanos)))

    finales = [e for e in eventos if e.get("type") == "result"]
    if not finales:
        aviso("{}: sin evento 'result' -> no reporta USD, el costo se estimara "
              "con tokens".format(ruta.name))
        costo = None
    else:
        costo = finales[-1].get("total_cost_usd")
        if costo is None:
            aviso("{}: 'result' sin total_cost_usd -> el costo se estimara".format(ruta.name))
        elif not isinstance(costo, (int, float)):
            falla("{}: total_cost_usd no es numero".format(ruta.name))

    modelos = {e.get("message", {}).get("model") for e in eventos if e.get("type") == "assistant"}
    modelos |= {e.get("model") for e in eventos if e.get("type") == "system"}
    modelos.discard(None)
    if modelo_esperado and modelos and modelo_esperado not in modelos:
        falla("{}: la traza dice modelo {} pero vas a declarar {} -> "
              "'el modelo declarado no coincide'".format(ruta.name, sorted(modelos), modelo_esperado))

    ok("{}: {} pasos, {} tool_result, costo {}".format(
        ruta.name, len(usos), len(resultados),
        "US${:.4f}".format(costo) if isinstance(costo, (int, float)) else "no reportado"))
    return next(iter(modelos), None) if modelos else None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="bench", help="carpeta con answers/ y traces/")
    p.add_argument("--model", default=None, help="model id que vas a declarar en submit.py")
    args = p.parse_args()

    base = Path(args.dir)
    d_ans, d_trz = base / "answers", base / "traces"
    print("\nValidando entrega en {}/\n".format(base))
    if not d_ans.is_dir():
        print("  no existe {}/ — corre primero: node web/bench.mjs".format(d_ans))
        sys.exit(1)

    modelos_vistos: set[str] = set()
    costo_total = 0.0
    presentes = 0

    for qid in QUESTION_IDS:
        f_ans = d_ans / (qid + ".json")
        if not f_ans.is_file():
            falla("{}.json: NO EXISTE -> esa pregunta puntua 0.0".format(qid))
            continue
        presentes += 1
        print("{}:".format(qid))
        validar_respuesta(f_ans, qid)
        m = validar_traza(d_trz / (qid + ".events.jsonl"), qid, args.model)
        if m:
            modelos_vistos.add(m)
        f_t = d_trz / (qid + ".events.jsonl")
        if f_t.is_file():
            for l in reversed(f_t.read_text(encoding="utf-8").splitlines()):
                if not l.strip():
                    continue
                try:
                    o = json.loads(l)
                except json.JSONDecodeError:
                    break
                if o.get("type") == "result" and isinstance(o.get("total_cost_usd"), (int, float)):
                    costo_total += o["total_cost_usd"]
                break
        print()

    print("=" * 70)
    if len(modelos_vistos) > 1:
        falla("las trazas mezclan modelos {}: declara una sola corrida".format(sorted(modelos_vistos)))
    if modelos_vistos:
        print("  modelo en las trazas : {}".format(sorted(modelos_vistos)[0]))
    print("  preguntas con respuesta: {}/7".format(presentes))
    print("  costo sumado en trazas : US${:.4f}".format(costo_total))
    if costo_total:
        print("  factor de costo        : x{:.3f}  (puntaje = calidad x 2/(2+costo))".format(
            2 / (2 + costo_total)))
    print("  fallas: {} · avisos: {}".format(len(fallos), len(avisos)))
    if fallos:
        print("\n  NO entregues asi: submit.py o el evaluador lo van a castigar.")
        sys.exit(1)
    print("\n  Contrato OK. Listo para entregar.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
