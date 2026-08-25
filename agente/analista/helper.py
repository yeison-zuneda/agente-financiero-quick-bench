"""Funciones financieras verificadas sobre las tablas pre-calculadas.

El agente recibe SOLO las firmas y los docstrings de este modulo (no el codigo).
Cada funcion devuelve un DataFrame o un dict pequeno, listo para imprimir.

Convencion del caso, fijada una sola vez y usada en todas partes:
    margen = (Ingreso - Costo) / Ingreso,  Costo = clases 6 y 7.
El gasto administrativo (clase 5) NO entra al margen del proyecto.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

DATOS = Path(__file__).resolve().parent.parent / "datos"  # agente/datos

MESES = {5: "mayo", 6: "junio", 7: "julio"}
GERENCIAS_OPERATIVAS = ["LAST MILE COLOMBIA", "FIRST MILE", "LONG HAUL",
                        "WAREHOUSE", "COURIER COLOMBIA"]

hechos = pd.read_parquet(DATOS / "hechos.parquet")
rentabilidad_df = pd.read_parquet(DATOS / "rentabilidad.parquet")
informe_df = pd.read_parquet(DATOS / "informe.parquet")
nomina = pd.read_parquet(DATOS / "nomina.parquet")
ausencias = pd.read_parquet(DATOS / "ausencias.parquet")


class DescuadreError(Exception):
    """Dos metodos independientes dieron resultados distintos para el mismo KPI."""


def _periodos(periodo):
    if periodo is None:
        return [5, 6, 7]
    if isinstance(periodo, (list, tuple, set)):
        return sorted(int(p) for p in periodo)
    return [int(periodo)]


def rentabilidad(proyecto=None, gerencia=None, periodo=None, minimo_ingreso=0.0):
    """Ingreso, Costo, Utilidad y Margen agregados.

    proyecto: codigo como texto, p.ej. '594'. gerencia: p.ej. 'WAREHOUSE'.
    periodo: 5 (mayo), 6 (junio), 7 (julio), lista, o None para los tres.
    Si se da `proyecto`, agrupa por proyecto x periodo; si solo `gerencia`,
    agrupa por gerencia x periodo; si no se da nada, agrupa por gerencia.
    Devuelve DataFrame con Margen en FRACCION (0.138 = 13.8%).

    Verifica el resultado recalculando el ingreso desde `hechos` (clase 4) y
    lanza DescuadreError si los dos metodos no coinciden.
    """
    d = rentabilidad_df[rentabilidad_df["Periodo"].isin(_periodos(periodo))]
    if gerencia is not None:
        d = d[d["Gerencia"].str.upper() == str(gerencia).upper()]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]

    claves = ["Proyecto", "NombreCentroCosto", "Gerencia"] if proyecto is not None else ["Gerencia"]
    if proyecto is None and gerencia is not None:
        claves = ["Gerencia"]
    g = d.groupby(claves + ["Periodo"], as_index=False)[["Ingreso", "Costo", "Gasto"]].sum()

    # Verificacion por segunda via: el ingreso recalculado desde el detalle contable.
    h = hechos[hechos["Periodo"].isin(_periodos(periodo))]
    if gerencia is not None:
        h = h[h["Gerencia"].str.upper() == str(gerencia).upper()]
    if proyecto is not None:
        h = h[h["Proyecto"].astype(str) == str(proyecto)]
    if abs(g["Ingreso"].sum() - h["Ingreso"].sum()) > 1.0:
        raise DescuadreError("ingreso {:,.2f} != {:,.2f} recalculado desde hechos".format(
            g["Ingreso"].sum(), h["Ingreso"].sum()))

    g["Mes"] = g["Periodo"].map(MESES)
    g["Utilidad"] = g["Ingreso"] - g["Costo"]
    g["Margen"] = (g["Utilidad"] / g["Ingreso"]).where(g["Ingreso"].abs() > 1)
    g = g[g["Ingreso"].abs() >= minimo_ingreso]
    return g.sort_values(claves + ["Periodo"]).reset_index(drop=True)


def evolucion(nivel="gerencia", minimo_ingreso=1e8, gerencia=None):
    """Margen mes a mes (mayo, junio, julio) para ver quien mejora y quien cae.

    nivel: 'gerencia' o 'proyecto'.
    minimo_ingreso: filtro de MATERIALIDAD sobre el ingreso promedio mensual.
        Por defecto 100 millones COP. Sirve para no reportar como "mejor
        evolucion" a una unidad que mueve $4 millones en una compania que
        factura ~$24.500 millones al mes.
    Devuelve DataFrame con columnas mayo/junio/julio (margen en fraccion),
    IngresoPromedio y DeltaJulioMayo (en puntos porcentuales, ya x100).
    """
    clave = "Gerencia" if nivel == "gerencia" else "Proyecto"
    d = rentabilidad_df.copy()
    if gerencia is not None:
        d = d[d["Gerencia"].str.upper() == str(gerencia).upper()]
    g = d.groupby([clave, "Periodo"], as_index=False)[["Ingreso", "Costo"]].sum()
    g["Margen"] = ((g["Ingreso"] - g["Costo"]) / g["Ingreso"]).where(g["Ingreso"].abs() > 1)

    piv = g.pivot(index=clave, columns="Periodo", values="Margen")
    piv.columns = [MESES.get(c, c) for c in piv.columns]
    ing = g.pivot(index=clave, columns="Periodo", values="Ingreso")
    piv["IngresoPromedio"] = ing.mean(axis=1)
    piv["DeltaJulioMayo"] = (piv.get("julio") - piv.get("mayo")) * 100
    piv["DeltaJulioJunio"] = (piv.get("julio") - piv.get("junio")) * 100

    if nivel == "proyecto":
        nombres = rentabilidad_df.drop_duplicates("Proyecto").set_index("Proyecto")["Gerencia"]
        piv["Gerencia"] = nombres
    piv = piv[piv["IngresoPromedio"] >= minimo_ingreso]
    return piv.sort_values("DeltaJulioMayo", ascending=False).reset_index()


def informe_variacion(gerencia=None, proyecto=None, solo_relevantes=False):
    """La tabla exacta del Caso Financiero: junio vs julio por proyecto.

    Columnas: Gerencia, Proyecto, NombreCentroCosto, IngresoJunio, CostoJunio,
    MargenJunio, IngresoJulio, CostoJulio, MargenJulio, Variacion (diferencia de
    margen en fraccion), VariacionIngreso, Utilidad (IngresoJulio - CostoJulio) y
    Observacion (Aumenta / Disminuye / Se mantiene, umbral +-0.5 pp).

    solo_relevantes=True deja solo proyectos con ingreso > 0 en ambos meses.
    """
    d = informe_df.copy()
    if gerencia is not None:
        d = d[d["Gerencia"].str.upper() == str(gerencia).upper()]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]
    if solo_relevantes:
        d = d[(d["IngresoJunio"] != 0) & (d["IngresoJulio"] != 0)]
    return d.sort_values("Variacion").reset_index(drop=True)


def facturacion(periodo=None, por="gerencia"):
    """Facturacion REAL del mes: ingreso neto de cuentas clase 4.

    Neto = Credito - Debito. Los debitos en clase 4 son devoluciones y notas
    credito, que ya quedan restadas. Devuelve el bruto, las devoluciones y el
    neto por separado para que la respuesta pueda declarar la convencion.
    por: 'gerencia', 'total' o 'proyecto'.
    """
    d = hechos[(hechos["Clase"] == "4") & (hechos["Periodo"].isin(_periodos(periodo)))]
    clave = {"gerencia": ["Periodo", "Gerencia"], "proyecto": ["Periodo", "Gerencia", "Proyecto"],
             "total": ["Periodo"]}[por]
    g = d.groupby(clave, as_index=False).agg(
        FacturacionBruta=("Credito", "sum"),
        DevolucionesYNC=("Debito", "sum"))
    g["FacturacionNeta"] = g["FacturacionBruta"] - g["DevolucionesYNC"]
    g["Mes"] = g["Periodo"].map(MESES)

    # Verificacion por segunda via contra la columna Ingreso ya firmada.
    control = d.groupby(clave)["Ingreso"].sum().sum()
    if abs(g["FacturacionNeta"].sum() - control) > 1.0:
        raise DescuadreError("facturacion neta no cuadra con la columna Ingreso")
    return g


def desglose_costos(proyecto=None, gerencia=None, periodo=None, n=15, comparar=False):
    """Top-n cuentas de costo (clases 6 y 7) con su nombre.

    comparar=True devuelve una columna por periodo y la variacion entre el
    primero y el ultimo, que es como se encuentra QUE cuenta movio el margen.
    """
    d = hechos[(hechos["Clase"].isin(["6", "7"])) & (hechos["Periodo"].isin(_periodos(periodo)))]
    if gerencia is not None:
        d = d[d["Gerencia"].str.upper() == str(gerencia).upper()]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]

    if comparar:
        piv = d.pivot_table(index=["AccountId", "AccountName"], columns="Periodo",
                            values="Costo", aggfunc="sum", fill_value=0.0)
        piv.columns = [MESES.get(c, c) for c in piv.columns]
        cols = [c for c in ("mayo", "junio", "julio") if c in piv.columns]
        if len(cols) >= 2:
            piv["Variacion"] = piv[cols[-1]] - piv[cols[0]]
            piv = piv.reindex(piv["Variacion"].abs().sort_values(ascending=False).index)
        return piv.head(n).reset_index()

    g = d.groupby(["AccountId", "AccountName"], as_index=False)["Costo"].sum()
    total = g["Costo"].sum()
    g["PctDelCosto"] = g["Costo"] / total * 100 if total else 0.0
    return g.sort_values("Costo", ascending=False).head(n).reset_index(drop=True)


def novedades(proyecto=None, gerencia=None, periodo=None, detalle=False):
    """Novedades de personal (ausencias) que pueden explicar un cambio de costo.

    Cruza el listado de ausencias, que SI trae proyecto. Por defecto devuelve el
    resumen concepto x periodo (cuantas novedades y cuantos dias); detalle=True
    devuelve el listado persona por persona con fechas.
    Conceptos tipicos: incapacidad EPS, incapacidad por accidente laboral,
    licencia remunerada / no remunerada, vacaciones, suspension, calamidad.
    """
    d = ausencias[ausencias["Periodo"].isin(_periodos(periodo))]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]
    if gerencia is not None:
        proys = set(rentabilidad_df.loc[
            rentabilidad_df["Gerencia"].str.upper() == str(gerencia).upper(), "Proyecto"])
        d = d[d["Proyecto"].isin(proys)]
    if detalle:
        return d.sort_values(["Periodo", "ConceptName", "DateInitial"]).reset_index(drop=True)
    if d.empty:
        return d
    g = d.groupby(["Periodo", "ConceptName"], as_index=False).agg(
        Novedades=("ConceptName", "size"),
        Dias=("DaysCalendary", "sum"),
        Personas=("ClientName", "nunique"))
    g["Mes"] = g["Periodo"].map(MESES)
    return g.sort_values(["Periodo", "Novedades"], ascending=[True, False]).reset_index(drop=True)


def conceptos_nomina(proyecto=None, gerencia=None, periodo=None, n=20, comparar=False):
    """Conceptos de nomina pagados (salario, horas extra, recargos, auxilios...).

    Complementa a `novedades`: aquella dice QUE paso con la gente, esta dice
    CUANTO costo. comparar=True abre una columna por mes con la variacion.
    """
    d = nomina[nomina["Periodo"].isin(_periodos(periodo))]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]
    if gerencia is not None:
        d = d[d["Gerencia"].astype(str).str.upper() == str(gerencia).upper()]
    if d.empty:
        return d
    if comparar:
        piv = d.pivot_table(index="ConceptName", columns="Periodo", values="Value",
                            aggfunc="sum", fill_value=0.0)
        piv.columns = [MESES.get(c, c) for c in piv.columns]
        cols = [c for c in ("mayo", "junio", "julio") if c in piv.columns]
        if len(cols) >= 2:
            piv["Variacion"] = piv[cols[-1]] - piv[cols[0]]
            piv = piv.reindex(piv["Variacion"].abs().sort_values(ascending=False).index)
        return piv.head(n).reset_index()
    g = d.groupby(["Periodo", "ConceptName"], as_index=False).agg(
        Valor=("Value", "sum"), Registros=("Value", "size"), Personas=("ClientId", "nunique"))
    return g.sort_values(["Periodo", "Valor"], ascending=[True, False]).head(n).reset_index(drop=True)


def retroactivos(periodo=None, gerencia=None):
    """Cuanto del costo de un mes NO es de ese mes, por las dos vias disponibles.

    VIA 1 - nomina: conceptos causados en `Periodo` pero pagados en un
      `PeriodoPago` distinto (columna EsRetroactivo). Es la evidencia dura.
    VIA 2 - contable: asientos cuya Observation menciona un mes ANTERIOR
      ("FACTURA ... MES DE ABRIL 2026"). Es evidencia textual, mas ruidosa.

    OJO: en el contable la Fecha del documento SIEMPRE cae dentro de su Periodo,
    asi que comparar Fecha contra Periodo no detecta nada. Estas dos vias son las
    unicas. Devuelve un dict con ambos resultados y el total del mes de contexto.
    """
    pers = _periodos(periodo)
    nom = nomina[nomina["Periodo"].isin(pers) & nomina["EsRetroactivo"]]
    con = hechos[hechos["Periodo"].isin(pers) & hechos["EsAjusteMesAnterior"]]
    base = hechos[hechos["Periodo"].isin(pers)]
    if gerencia is not None:
        gu = str(gerencia).upper()
        nom = nom[nom["Gerencia"].astype(str).str.upper() == gu]
        con = con[con["Gerencia"].str.upper() == gu]
        base = base[base["Gerencia"].str.upper() == gu]

    costo_mes = float(base["Costo"].sum() + base["Gasto"].sum())
    resumen_nom = (nom.groupby(["Periodo", "PeriodoPago", "ConceptName"], as_index=False)
                   .agg(Valor=("Value", "sum"), Registros=("Value", "size"))
                   .sort_values("Valor", ascending=False)) if not nom.empty else nom

    # Cargos y reversiones se informan por separado: netearlos esconde el bruto.
    # Un cargo de abril registrado en junio y una reversion de marzo hecha en junio
    # son dos hechos distintos y el neto de ambos no describe ninguno.
    con = con.assign(Monto=con["Costo"] + con["Gasto"]) if not con.empty else con
    cargos = float(con.loc[con["Monto"] > 0, "Monto"].sum()) if not con.empty else 0.0
    reversiones = float(con.loc[con["Monto"] < 0, "Monto"].sum()) if not con.empty else 0.0
    resumen_con = (con.groupby(["Periodo", "MesMencionado"], as_index=False)
                   .agg(Monto=("Monto", "sum"), Asientos=("Monto", "size"))
                   .sort_values("Monto", key=abs, ascending=False)) if not con.empty else con

    nom_valor = float(nom["Value"].sum()) if not nom.empty else 0.0
    bruto = nom_valor + cargos + abs(reversiones)
    return {
        "costo_y_gasto_del_mes": costo_mes,
        "nomina_retroactiva_valor": nom_valor,
        "nomina_retroactiva_registros": int(len(nom)),
        "nomina_detalle": resumen_nom,
        "contable_cargos_de_mes_anterior": cargos,
        "contable_reversiones_de_mes_anterior": reversiones,
        "contable_neto": cargos + reversiones,
        "contable_asientos": int(len(con)),
        "contable_detalle": resumen_con,
        "ajustes_brutos_total": bruto,
        "pct_bruto_del_costo_del_mes": bruto / costo_mes * 100 if costo_mes else None,
        "pct_neto_del_costo_del_mes": (
            (nom_valor + cargos + reversiones) / costo_mes * 100 if costo_mes else None),
        "nota": ("La VIA 2 (texto de Observation) es indicativa, no contable: detecta que el "
                 "asiento MENCIONA un mes anterior. Reportar cargos y reversiones por separado."),
    }


def proyeccion_margen(gerencia=None, proyecto=None, metodo="tendencia"):
    """Proyecta el margen del mes siguiente (agosto 2026). NO ES UN DATO.

    Solo existen mayo, junio y julio de 2026. Cualquier cifra de agosto es una
    extrapolacion. Devuelve un dict con la serie observada, la proyeccion, el
    supuesto explicito y un rango. Declara SIEMPRE el supuesto en la respuesta.

    metodo:
      'tendencia'  - continua la pendiente de los ultimos dos meses.
      'promedio'   - vuelve al promedio de los tres meses (reversion a la media).
    """
    d = rentabilidad_df.copy()
    if gerencia is not None:
        d = d[d["Gerencia"].str.upper() == str(gerencia).upper()]
    if proyecto is not None:
        d = d[d["Proyecto"].astype(str) == str(proyecto)]
    g = d.groupby("Periodo", as_index=False)[["Ingreso", "Costo"]].sum()
    g["Margen"] = (g["Ingreso"] - g["Costo"]) / g["Ingreso"]
    serie = {MESES[int(r.Periodo)]: float(r.Margen) for r in g.itertuples()}
    if len(serie) < 2:
        return {"error": "serie insuficiente para proyectar", "serie": serie}

    may, jun, jul = serie.get("mayo"), serie.get("junio"), serie.get("julio")
    if metodo == "tendencia":
        proy = jul + (jul - jun)
        supuesto = ("se mantiene la pendiente junio->julio ({:+.1f} pp) un mes mas, "
                    "sin ninguna accion correctiva".format((jul - jun) * 100))
    else:
        proy = sum(v for v in (may, jun, jul) if v is not None) / len(
            [v for v in (may, jun, jul) if v is not None])
        proy = float(proy)
        supuesto = "el margen revierte al promedio de mayo-julio (sin causa estructural)"

    piso = min(v for v in (may, jun, jul) if v is not None)
    techo = max(v for v in (may, jun, jul) if v is not None)
    return {
        "serie_observada": serie,
        "proyeccion_agosto": float(proy),
        "metodo": metodo,
        "supuesto": supuesto,
        "rango_historico": {"min": float(piso), "max": float(techo)},
        "advertencia": ("Agosto 2026 NO existe en los datos. Esta cifra es una "
                        "proyeccion bajo el supuesto declarado, no un hecho."),
    }


def buscar(texto, donde="observacion", periodo=None, n=25):
    """Busca texto libre. donde: 'observacion', 'cuenta', 'concepto', 'proyecto'.

    Util para preguntas no previstas: encontrar una factura, un proveedor, un
    tipo de gasto, o entender que hay detras de una cuenta.
    """
    t = str(texto).upper()
    pers = _periodos(periodo)
    if donde == "observacion":
        d = hechos[hechos["Periodo"].isin(pers)]
        m = d["Observation"].fillna("").astype(str).str.upper().str.contains(t, regex=False)
        cols = ["Periodo", "AccountName", "Gerencia", "Proyecto", "Debito", "Credito", "Observation"]
        return d.loc[m, cols].head(n).reset_index(drop=True)
    if donde == "cuenta":
        d = hechos[hechos["Periodo"].isin(pers)]
        m = d["AccountName"].str.upper().str.contains(t, regex=False)
        return (d.loc[m].groupby(["Periodo", "AccountId", "AccountName"], as_index=False)
                [["Debito", "Credito", "Costo", "Gasto", "Ingreso"]].sum().head(n))
    if donde == "concepto":
        d = nomina[nomina["Periodo"].isin(pers)]
        m = d["ConceptName"].str.upper().str.contains(t, regex=False)
        return (d.loc[m].groupby(["Periodo", "ConceptName"], as_index=False)
                .agg(Valor=("Value", "sum"), Registros=("Value", "size")).head(n))
    if donde == "proyecto":
        d = ausencias[ausencias["ProjectName"].str.upper().str.contains(t, regex=False)]
        return d[["Proyecto", "ProjectName"]].drop_duplicates().head(n).reset_index(drop=True)
    raise ValueError("donde debe ser: observacion, cuenta, concepto o proyecto")


FIRMAS = """
Las columnas EXACTAS que devuelve cada funcion van listadas abajo. Usalas tal cual:
inventarse un nombre cuesta un turno de correccion. Los meses en minuscula.

helper.rentabilidad(proyecto=None, gerencia=None, periodo=None, minimo_ingreso=0.0) -> DataFrame
    -> Gerencia [, Proyecto, NombreCentroCosto si pasas proyecto], Periodo, Mes,
       Ingreso, Costo, Gasto, Utilidad, Margen
    Margen en FRACCION (0.138 = 13.8%). Verificado por doble via.

helper.evolucion(nivel='gerencia'|'proyecto', minimo_ingreso=1e8, gerencia=None) -> DataFrame
    -> Gerencia (o Proyecto), mayo, junio, julio, IngresoPromedio,
       DeltaJulioMayo, DeltaJulioJunio
    OJO: las columnas de mes van en MINUSCULA ('mayo', no 'Mayo').
    mayo/junio/julio son margen en fraccion; los Delta ya vienen en PUNTOS
    PORCENTUALES (x100). minimo_ingreso es el filtro de MATERIALIDAD.

helper.informe_variacion(gerencia=None, proyecto=None, solo_relevantes=False) -> DataFrame
    -> Gerencia, Proyecto, NombreCentroCosto, IngresoJunio, CostoJunio, MargenJunio,
       IngresoJulio, CostoJulio, MargenJulio, Variacion, VariacionIngreso, Utilidad,
       Observacion
    La tabla del Caso Financiero. Variacion = MargenJulio - MargenJunio, en fraccion.

helper.facturacion(periodo=None, por='gerencia'|'total'|'proyecto') -> DataFrame
    -> Periodo [, Gerencia, Proyecto segun `por`], FacturacionBruta, DevolucionesYNC,
       FacturacionNeta, Mes

helper.desglose_costos(proyecto=None, gerencia=None, periodo=None, n=15, comparar=False) -> DataFrame
    comparar=False -> AccountId, AccountName, Costo, PctDelCosto
    comparar=True  -> AccountId, AccountName, mayo, junio, julio, Variacion
                      (solo las columnas de los periodos que pediste)

helper.novedades(proyecto=None, gerencia=None, periodo=None, detalle=False) -> DataFrame
    detalle=False -> Periodo, ConceptName, Novedades, Dias, Personas, Mes
    detalle=True  -> el listado completo: ClientName, DateInitial, DateFinal, Quantity...
    NO acepta `comparar`: para comparar meses pide periodo=[6,7] y pivotea la columna Periodo.

helper.conceptos_nomina(proyecto=None, gerencia=None, periodo=None, n=20, comparar=False) -> DataFrame
    comparar=False -> Periodo, ConceptName, Valor, Registros, Personas
    comparar=True  -> ConceptName, mayo, junio, julio, Variacion
                      (aqui NO existen 'Value' ni 'Quantity': ya vienen agregadas)

helper.retroactivos(periodo=None, gerencia=None) -> dict (no DataFrame)
    claves: costo_y_gasto_del_mes, nomina_retroactiva_valor, nomina_retroactiva_registros,
    nomina_detalle (DataFrame), contable_cargos_de_mes_anterior,
    contable_reversiones_de_mes_anterior, contable_neto, contable_asientos,
    contable_detalle (DataFrame), ajustes_brutos_total, pct_bruto_del_costo_del_mes,
    pct_neto_del_costo_del_mes, nota

helper.proyeccion_margen(gerencia=None, proyecto=None, metodo='tendencia'|'promedio') -> dict
    claves: serie_observada (dict mes->margen), proyeccion_agosto, metodo, supuesto,
    rango_historico (dict con 'min' y 'max'), advertencia
    Agosto 2026 NO existe en los datos: siempre declara el supuesto.

helper.buscar(texto, donde='observacion'|'cuenta'|'concepto'|'proyecto', periodo=None, n=25)
    Busqueda de texto libre para preguntas no previstas.

DataFrames crudos ya cargados: hechos, rentabilidad_df, informe_df, nomina, ausencias.
Si necesitas algo que ninguna funcion cubre, agrega sobre `hechos` directamente.
""".strip()
