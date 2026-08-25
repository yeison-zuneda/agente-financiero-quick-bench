"""Sandbox local persistente: ejecuta el codigo Python que escribe el agente.

Protocolo (una linea JSON por mensaje, para que Node lo maneje sin dependencias):
    entrada : {"codigo": "..."}
    salida  : {"ok": true, "stdout": "..."} | {"ok": false, "error": "..."}

Es PERSISTENTE a proposito: cargar los 5 parquet toma ~2 s y una pregunta puede
gastar 3-6 llamadas. Arrancando un proceso por llamada se pagarian ~12 s de puro
arranque por pregunta.

El estado NO se comparte entre preguntas (cada pregunta manda "reset": true), asi
que una respuesta no puede contaminar la siguiente.

    echo '{"codigo":"print(1+1)"}' | python analista/sandbox.py
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
import helper  # noqa: E402

MAX_SALIDA = 6000

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_rows", 60)
pd.set_option("display.float_format", lambda v: "{:,.2f}".format(v))


def namespace_base():
    return {
        "__name__": "__sandbox__",
        "pd": pd,
        "helper": helper,
        "hechos": helper.hechos,
        "rentabilidad_df": helper.rentabilidad_df,
        "informe_df": helper.informe_df,
        "nomina": helper.nomina,
        "ausencias": helper.ausencias,
        "MESES": helper.MESES,
    }


def truncar(texto):
    if len(texto) <= MAX_SALIDA:
        return texto
    corte = MAX_SALIDA // 2
    omitidos = len(texto) - MAX_SALIDA
    return (texto[:corte] + "\n\n... [{} caracteres omitidos: filtra o agrega mas, "
            "no pidas la tabla completa] ...\n\n".format(omitidos) + texto[-corte:])


def ejecutar(codigo, ns):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            exec(compile(codigo, "<agente>", "exec"), ns)
    except Exception:
        salida = buf.getvalue()
        tb = traceback.format_exc(limit=3)
        return {"ok": False, "error": truncar(tb), "stdout": truncar(salida)}
    salida = buf.getvalue().strip()
    if not salida:
        salida = ("(el codigo corrio sin errores pero no imprimio nada: "
                  "usa print() para ver el resultado)")
    return {"ok": True, "stdout": truncar(salida)}


def main():
    ns = namespace_base()
    # Aviso de que el modulo cargo bien; Node espera esta linea antes de aceptar preguntas.
    print(json.dumps({"listo": True, "filas_hechos": len(helper.hechos)}), flush=True)
    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue
        try:
            msg = json.loads(linea)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": "JSON invalido: {}".format(e)}), flush=True)
            continue
        if msg.get("reset"):
            ns = namespace_base()
        codigo = msg.get("codigo", "")
        if not codigo.strip():
            print(json.dumps({"ok": True, "stdout": "(sin codigo)"}), flush=True)
            continue
        print(json.dumps(ejecutar(codigo, ns), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
