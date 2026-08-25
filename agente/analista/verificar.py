"""Responde las 7 preguntas del caso usando SOLO pandas, sin LLM.

Es el gate: si estos numeros estan mal, no tiene sentido conectar la API.
Tambien produce los valores gold contra los que se compara al agente.

    python analista/verificar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helper  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
pd.set_option("display.float_format", lambda v: "{:,.2f}".format(v))

fallos = []


def titulo(n, txt):
    print("\n" + "=" * 78)
    print("P{}. {}".format(n, txt))
    print("=" * 78)


def chequear(condicion, mensaje):
    if condicion:
        print("   [OK] " + mensaje)
    else:
        print("   [FALLA] " + mensaje)
        fallos.append(mensaje)


# --------------------------------------------------------------------------
titulo(1, "Que linea en los tres meses ha tenido la mejor evolucion en "
          "rentabilidad y a que se debe?")
ev = helper.evolucion(nivel="gerencia", minimo_ingreso=1e8)
print(ev[["Gerencia", "mayo", "junio", "julio", "DeltaJulioMayo", "IngresoPromedio"]].to_string(index=False))
ev_todo = helper.evolucion(nivel="gerencia", minimo_ingreso=0)
print("\n   Sin filtro de materialidad (para ver la trampa):")
print(ev_todo[["Gerencia", "mayo", "julio", "DeltaJulioMayo", "IngresoPromedio"]].to_string(index=False))
mejor = ev.iloc[0]["Gerencia"]
print("\n   >>> MEJOR EVOLUCION MATERIAL: {}".format(mejor))
chequear(mejor == "LAST MILE COLOMBIA",
         "la mejor evolucion material es LAST MILE COLOMBIA (COURIER sube mas pero es inmaterial)")
chequear("COURIER COLOMBIA" not in set(ev["Gerencia"]),
         "COURIER COLOMBIA queda excluida por materialidad")

# --------------------------------------------------------------------------
titulo(2, "Para mejorar la rentabilidad de Warehouse, que debemos hacer?")
r = helper.rentabilidad(gerencia="WAREHOUSE")
print(r[["Gerencia", "Periodo", "Mes", "Ingreso", "Costo", "Utilidad", "Margen"]].to_string(index=False))
print("\n   Cuentas de costo que mas se movieron (mayo -> julio):")
dc = helper.desglose_costos(gerencia="WAREHOUSE", comparar=True, n=8)
print(dc.to_string(index=False))
print("\n   Peores proyectos de Warehouse por variacion de margen jun->jul:")
iv = helper.informe_variacion(gerencia="WAREHOUSE", solo_relevantes=True)
print(iv[["Proyecto", "IngresoJulio", "MargenJunio", "MargenJulio", "Variacion", "Utilidad"]]
      .head(6).to_string(index=False))
chequear(len(iv) > 0, "hay proyectos de Warehouse con ingreso en ambos meses")

# --------------------------------------------------------------------------
titulo(3, "Warehouse va a recuperar su rentabilidad en agosto?")
for metodo in ("tendencia", "promedio"):
    p = helper.proyeccion_margen(gerencia="WAREHOUSE", metodo=metodo)
    print("   metodo={:10s} agosto={:.1%}  supuesto: {}".format(
        metodo, p["proyeccion_agosto"], p["supuesto"]))
p = helper.proyeccion_margen(gerencia="WAREHOUSE")
print("   serie observada:", {k: "{:.1%}".format(v) for k, v in p["serie_observada"].items()})
print("   " + p["advertencia"])
chequear(p["proyeccion_agosto"] < p["serie_observada"]["julio"] + 0.001,
         "la tendencia NO indica recuperacion espontanea en agosto")

# --------------------------------------------------------------------------
titulo(4, "Cual fue la facturacion real de cada mes?")
f = helper.facturacion(por="total")
print(f[["Periodo", "Mes", "FacturacionBruta", "DevolucionesYNC", "FacturacionNeta"]].to_string(index=False))
jul = f.loc[f["Periodo"] == 7, "FacturacionNeta"].iloc[0]
chequear(abs(jul / 1e6 - 24553) < 50,
         "facturacion neta de julio ~ $24.553 millones (control cruzado)")

# --------------------------------------------------------------------------
titulo(5, "Que parte del gasto de un mes es realmente de ese mes y que parte es "
          "un ajuste retroactivo de un mes anterior?")
for per in (6, 7):
    d = helper.retroactivos(periodo=per)
    print("\n   --- {} ---".format(helper.MESES[per]))
    print("   costo+gasto del mes            : ${:,.0f}".format(d["costo_y_gasto_del_mes"]))
    print("   VIA 1 nomina retroactiva       : ${:,.0f} en {} registros".format(
        d["nomina_retroactiva_valor"], d["nomina_retroactiva_registros"]))
    print("   VIA 2 cargos de mes anterior   : ${:,.0f}".format(
        d["contable_cargos_de_mes_anterior"]))
    print("   VIA 2 reversiones de mes ant.  : ${:,.0f} ({} asientos en total)".format(
        d["contable_reversiones_de_mes_anterior"], d["contable_asientos"]))
    print("   ajustes BRUTOS                 : ${:,.0f} = {:.2f}% del costo del mes".format(
        d["ajustes_brutos_total"], d["pct_bruto_del_costo_del_mes"]))
    print("   efecto NETO                    : {:.2f}% del costo del mes".format(
        d["pct_neto_del_costo_del_mes"]))
    if not isinstance(d["contable_detalle"], pd.DataFrame) or not d["contable_detalle"].empty:
        print(d["contable_detalle"].head(4).to_string(index=False))
d6 = helper.retroactivos(periodo=6)
chequear(d6["nomina_retroactiva_registros"] > 0, "se detecta nomina retroactiva en junio")
chequear(d6["contable_asientos"] > 0, "se detectan asientos contables que citan un mes anterior")
chequear(d6["contable_cargos_de_mes_anterior"] > 0 and d6["contable_reversiones_de_mes_anterior"] < 0,
         "cargos y reversiones se reportan por separado, no neteados")

# --------------------------------------------------------------------------
for pid in ("594", "600"):
    titulo("6/7", "Novedades del proyecto {} que expliquen la variacion de "
                  "rentabilidad entre junio y julio".format(pid))
    r = helper.rentabilidad(proyecto=pid)
    print(r[["Proyecto", "Gerencia", "Periodo", "Mes", "Ingreso", "Costo", "Utilidad", "Margen"]]
          .to_string(index=False))
    iv = helper.informe_variacion(proyecto=pid)
    if not iv.empty:
        row = iv.iloc[0]
        print("\n   Variacion de margen jun->jul: {:+.2f} pp | Variacion ingreso: ${:,.0f} | "
              "Observacion: {}".format(row["Variacion"] * 100, row["VariacionIngreso"],
                                       row["Observacion"]))
    print("\n   Novedades de personal:")
    nv = helper.novedades(proyecto=pid, periodo=[6, 7])
    print(nv.to_string(index=False) if not nv.empty else "   (sin novedades registradas)")
    print("\n   Conceptos de nomina, junio vs julio:")
    cn = helper.conceptos_nomina(proyecto=pid, periodo=[6, 7], comparar=True, n=6)
    print(cn.to_string(index=False) if not cn.empty else "   (sin nomina asignada)")
    print("\n   Cuentas de costo que mas se movieron:")
    dc = helper.desglose_costos(proyecto=pid, periodo=[6, 7], comparar=True, n=6)
    print(dc.to_string(index=False) if not dc.empty else "   (sin costo)")
    chequear(not r.empty, "el proyecto {} tiene movimiento contable".format(pid))
    chequear(not nv.empty, "el proyecto {} tiene novedades de personal".format(pid))

# --------------------------------------------------------------------------
print("\n" + "=" * 78)
if fallos:
    print("RESULTADO: {} chequeo(s) FALLARON".format(len(fallos)))
    for f_ in fallos:
        print("  - " + f_)
    sys.exit(1)
print("RESULTADO: todos los chequeos pasaron. Los numeros estan listos.")
