#!/usr/bin/env python3
"""Entrega tu corrida al Quick Golden Bench. Solo stdlib: no instala nada.

    curl -sO https://quick-golden-bench.vercel.app/submit.py
    python3 submit.py --team mi-equipo --model claude-sonnet-5

Empaqueta answers/ + traces/ con un manifest.json (sha256 de cada archivo) y hace POST del
zip al leaderboard. No hace falta token ni cuenta.

Falla ruidoso a propósito: si una respuesta no cumple el contrato o si el servidor rechaza
la entrega, sale con código 1 y te dice qué pasó. Nunca "parece" enviado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BENCH_VERSION = "0.1.0"
DEFAULT_SITE = "https://quick-golden-bench.vercel.app"
QUESTION_IDS = ("q01", "q02", "q03", "q04", "q05", "q06", "q07")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TIMEOUT_S = 120

ANSWER_REQUIRED = {"question_id", "answer", "summary"}
ANSWER_OPTIONAL = {"method", "code", "caveats", "conventions"}
ANSWER_KEYS = ANSWER_REQUIRED | ANSWER_OPTIONAL


class Fail(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"\n✗ {msg}\n")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_answer(path: Path, qid: str) -> dict:
    """Mismo contrato que el schema `Answer` (pydantic, extra='forbid')."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Fail(f"{path}: no es JSON válido ({exc})")
    if not isinstance(obj, dict):
        raise Fail(f"{path}: la raíz debe ser un objeto JSON, no {type(obj).__name__}")
    unknown = set(obj) - ANSWER_KEYS
    if unknown:
        raise Fail(f"{path}: claves no permitidas {sorted(unknown)}. Permitidas: {sorted(ANSWER_KEYS)}")
    missing = ANSWER_REQUIRED - set(obj)
    if missing:
        raise Fail(f"{path}: faltan claves obligatorias {sorted(missing)}")
    if obj["question_id"] != qid:
        raise Fail(f"{path}: question_id es {obj['question_id']!r} pero el archivo se llama {qid}.json")
    if obj["answer"] is not None and not isinstance(obj["answer"], dict):
        raise Fail(f"{path}: 'answer' debe ser un objeto o null (es {type(obj['answer']).__name__})")
    if not isinstance(obj["summary"], str) or not obj["summary"].strip():
        raise Fail(f"{path}: 'summary' debe ser texto no vacío — es lo que le dirías al CFO")
    for key in ("method", "code"):
        if key in obj and not isinstance(obj[key], str):
            raise Fail(f"{path}: '{key}' debe ser texto")
    for key in ("caveats", "conventions"):
        if key in obj and not (isinstance(obj[key], list) and all(isinstance(x, str) for x in obj[key])):
            raise Fail(f"{path}: '{key}' debe ser una lista de textos")
    return obj


def find_trace(traces_dir: Path | None, qid: str) -> tuple[str, Path] | None:
    if traces_dir is None:
        return None
    for sub, name in (("trajectory", f"{qid}.atif.json"), ("events", f"{qid}.events.jsonl")):
        for cand in (traces_dir / name, traces_dir / sub / name):
            if cand.is_file():
                if cand.stat().st_size == 0:
                    raise Fail(f"{cand} está vacío: o entregas una traza real o no entregas traza (tope 0.5)")
                return sub, cand
    return None


def build(args: argparse.Namespace) -> tuple[Path, str, list[str]]:
    if not NAME_RE.match(args.team):
        raise Fail(f"nombre de equipo inválido {args.team!r}: usa letras, números, punto, guion o guion bajo")
    if not args.model.strip():
        raise Fail("--model no puede estar vacío: declara el model id que corriste, p.ej. claude-sonnet-5")

    answers_dir = Path(args.answers)
    if not answers_dir.is_dir():
        raise Fail(f"no existe el directorio de respuestas {answers_dir}/ (usa --answers)")
    found = [(qid, answers_dir / f"{qid}.json") for qid in QUESTION_IDS if (answers_dir / f"{qid}.json").is_file()]
    if not found:
        raise Fail(f"{answers_dir}/ no tiene ningún qNN.json (se esperan q01.json … q07.json)")

    traces_dir = Path(args.traces) if args.traces else None
    if traces_dir is not None and not traces_dir.is_dir():
        raise Fail(f"no existe el directorio de trazas {traces_dir}/ — pasa --traces '' si entregas sin traza")

    now = datetime.now(timezone.utc)
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", args.model)[:40]
    run_id = args.run_id or f"{now.strftime('%Y%m%d-%H%M%S')}-{slug}"
    if not NAME_RE.match(run_id):
        raise Fail(f"run-id inválido {run_id!r}: usa letras, números, punto, guion o guion bajo")

    members: list[tuple[str, Path]] = []
    files: dict[str, str] = {}
    no_trace: list[str] = []
    for qid, src in found:
        check_answer(src, qid)
        arc = f"answers/{qid}.json"
        members.append((arc, src))
        files[arc] = sha256(src)
        trace = find_trace(traces_dir, qid)
        if trace is None:
            no_trace.append(qid)
        else:
            sub, tpath = trace
            arc = f"{sub}/{tpath.name}"
            members.append((arc, tpath))
            files[arc] = sha256(tpath)

    manifest = {
        "team": args.team,
        "model": args.model,
        "created_at": now.isoformat(),
        "tooling": args.tooling,
        "bench_version": BENCH_VERSION,
        "files": files,
        "notes": args.notes,
    }

    zip_path = Path(args.out) / args.team / f"{run_id}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        raise Fail(f"ya existe {zip_path}: usa otro --run-id o bórralo")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{run_id}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for arc, src in sorted(members):
            z.write(src, f"{run_id}/{arc}")

    missing_q = [q for q in QUESTION_IDS if q not in dict(found)]
    if missing_q:
        print(f"⚠ sin respuesta para {', '.join(missing_q)} → esas preguntas puntúan 0.0")
    if no_trace:
        print(f"⚠ sin traza para {', '.join(no_trace)} → esas preguntas tienen tope 0.50")
    print(f"→ {zip_path} ({zip_path.stat().st_size / 1024:.0f} kB, {len(files)} archivos, run {run_id})")
    return zip_path, run_id, no_trace


def post(zip_path: Path, run_id: str, args: argparse.Namespace) -> None:
    url = args.to.rstrip("/") + "/api/submit"
    req = urllib.request.Request(
        url,
        data=zip_path.read_bytes(),
        method="POST",
        headers={
            "content-type": "application/zip",
            "x-bench-team": args.team,
            "x-bench-model": args.model,
            "x-bench-run-id": run_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise Fail(f"no se pudo conectar a {url}: {exc.reason}\n  El zip está en {zip_path}.")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise Fail(f"respuesta ilegible del servidor (HTTP {status}): {body[:300]!r}")
    if status >= 400:
        raise Fail(
            f"el servidor rechazó la entrega (HTTP {status}):\n"
            + json.dumps(parsed, ensure_ascii=False, indent=2)
            + f"\n  El zip está en {zip_path}: reintenta o entrégalo por el canal del evento."
        )
    print(f"✓ entrega recibida · equipo {parsed.get('team')} · run {parsed.get('run_id')}")
    print("  Tu fila aparece en el leaderboard cuando los organizadores la puntúen.")


def main() -> None:
    p = argparse.ArgumentParser(description="Entrega una corrida al Quick Golden Bench.")
    p.add_argument("--team", required=True, help="nombre del equipo [A-Za-z0-9._-]")
    p.add_argument("--model", required=True, help="model id que corriste, p.ej. claude-sonnet-5")
    p.add_argument("--answers", default="answers", help="directorio con qNN.json (por defecto: answers/)")
    p.add_argument("--traces", default="traces", help="directorio con qNN.events.jsonl o qNN.atif.json (por defecto: traces/)")
    p.add_argument("--tooling", default="", help="stack usado: 'Claude Code', 'Agent SDK', 'Managed Agents'…")
    p.add_argument("--notes", default="", help="nota libre para los organizadores")
    p.add_argument("--to", default=DEFAULT_SITE, help=f"URL del leaderboard (por defecto: {DEFAULT_SITE})")
    p.add_argument("--run-id", dest="run_id", default=None, help="id de la corrida; por defecto fecha + modelo")
    p.add_argument("--out", default="submissions", help="dónde dejar el zip (por defecto: submissions/)")
    p.add_argument("--pack-only", action="store_true", help="solo genera el zip, no lo envía")
    args = p.parse_args()

    zip_path, run_id, _ = build(args)
    if args.pack_only:
        print("  (--pack-only: no se envió nada)")
        return
    post(zip_path, run_id, args)


if __name__ == "__main__":
    main()
