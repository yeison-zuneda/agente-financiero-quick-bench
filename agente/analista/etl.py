"""ETL de diseno: corre UNA vez y deja tablas chicas listas para el agente.

Lee el movimiento contable (255k filas) + 6 archivos de nomina/ausencias y
escribe datos/*.parquet + datos/informe.json + datos/MANUAL_DATOS.md.

El objetivo economico: que ninguna pregunta tenga que volver a sumar 255k filas.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

# Este proyecto vive dentro de la carpeta del caso:
#   CASO FINANCIERO/CASO FINANCIERO/          <- CASO: archivos del organizador
#     |- MAYO-JUNIO-JULIO 2026.csv/, Nomina/
#     `- agente/                              <- BASE: todo lo que generamos
#          |- analista/  web/  datos/
BASE = Path(__file__).resolve().parent.parent
CASO = BASE.parent
CSV = CASO / "MAYO-JUNIO-JULIO 2026.csv" / "MAYO-JUNIO-JULIO 2026.csv"
NOMINA = CASO / "Nomina"
SALIDA = BASE / "datos"

MESES = {5: "mayo", 6: "junio", 7: "julio"}

ACUMULADOS = {
    5: NOMINA / "05. MAYO" / "ACUMULADO FINAL QH MAY 2026.xlsx",
    6: NOMINA / "06. JUNIO" / "ACUMULADO FINAL QH JUN 2026.xlsx",
    7: NOMINA / "07. JULIO" / "ACUMULADO INICIAL QH 03.08.2026.xlsx",
}
AUSENCIAS = {
    5: NOMINA / "05. MAYO" / "Listado_de_Ausencias (80) - QH ABR - MAY 2026.xlsx",
    6: NOMINA / "06. JUNIO" / "Listado_de_Ausencias (81).xlsx",
    7: NOMINA / "07. JULIO" / "Listado_de_Ausencias - JUN - JUL 2026 - QH.xlsx",
}

COLUMNAS = [
    "Period", "Year", "BookId", "AccountId", "AccountName", "DocumentId", "ConceptId",
    "NumberId", "LocationId", "Date", "Debito", "Credito", "ClientId", "ClientName",
    "CostCenterId", "BusinessId", "ProjectId", "BaseRetention", "Value1", "Module",
    "Observation", "Invoice", "CheckNumber", "State", "RowId", "Nature", "Conciliate",
    "CurrencyId", "CostCenterName", "EntryId", "SourceId", "Contractor",
]
IDX_OBSERVATION = COLUMNAS.index("Observation")


def _leer_contable_reparado():
    """Lee el CSV reparando las filas partidas por un ';' dentro de Observation.

    58 asientos vienen con campos de mas. Hay DOS averias distintas y se arreglan
    en este orden, porque confundirlas corre las columnas:

    1. Campo vacio de sobra AL FINAL (2 asientos de tiquetes aereos, junio). Los
       32 campos reales estan bien; solo hay un ';' colgando. Se descarta la cola.
    2. Un ';' DENTRO del texto de Observation (56 asientos: listas de guias,
       direcciones con tabuladores). Ahi si hay que volver a pegar el texto.

    Si se aplica (2) sobre un caso de (1), el CostCenterName termina tomando el
    valor de CurrencyId ('0') y el asiento pierde su gerencia.
    """
    crudo = CSV.read_text(encoding="latin-1").splitlines()
    cuerpo = crudo[1:]
    n = len(COLUMNAS)
    rep_cola = rep_texto = 0
    filas = []
    for linea in cuerpo:
        if not linea.strip():
            continue
        partes = linea.split(";")
        # (1) cola vacia
        while len(partes) > n and partes[-1] == "":
            partes.pop()
            rep_cola += 1
        # (2) ';' dentro de Observation
        if len(partes) > n:
            sobra = len(partes) - n
            fusion = ";".join(partes[IDX_OBSERVATION:IDX_OBSERVATION + sobra + 1])
            partes = partes[:IDX_OBSERVATION] + [fusion] + partes[IDX_OBSERVATION + sobra + 1:]
            rep_texto += 1
        if len(partes) == n:
            filas.append(partes)

    df = pd.DataFrame(filas, columns=COLUMNAS)
    return df, (rep_cola, rep_texto)


def _a_float(serie):
    """Decimal con coma -> float. '210200,0000' -> 210200.0"""
    limpia = serie.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(limpia, errors="coerce").fillna(0.0)


def construir_hechos():
    df, (rep_cola, rep_texto) = _leer_contable_reparado()
    print("[contable] {:,} filas leidas | reparadas: {} con ';' colgando al final, "
          "{} con ';' dentro de Observation".format(len(df), rep_cola, rep_texto))

    for col in ("Debito", "Credito"):
        df[col] = _a_float(df[col])

    df["Periodo"] = pd.to_numeric(df["Period"], errors="coerce").astype("Int64")
    df["Fecha"] = pd.to_datetime(df["Date"], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    df["Clase"] = df["AccountId"].astype(str).str.strip().str[0]

    for col in ("ProjectId", "DocumentId", "ClientId", "NumberId", "RowId",
                "CostCenterName", "AccountName", "AccountId", "Module", "ConceptId"):
        df[col] = df[col].astype(str).str.strip()

    df["Gerencia"] = df["CostCenterName"]
    df["Proyecto"] = df["ProjectId"]
    df["NombreCentroCosto"] = "Proyecto " + df["ProjectId"]

    # Convencion de signo declarada: ingreso natural credito, costo natural debito.
    ingreso = df["Credito"] - df["Debito"]
    costo = df["Debito"] - df["Credito"]
    df["Ingreso"] = ingreso.where(df["Clase"] == "4", 0.0)
    df["Costo"] = costo.where(df["Clase"].isin(["6", "7"]), 0.0)
    df["Gasto"] = costo.where(df["Clase"] == "5", 0.0)
    df["Mes"] = df["Periodo"].map(MESES)

    cols = ["Periodo", "Mes", "Fecha", "Clase", "AccountId", "AccountName", "Gerencia",
            "Proyecto", "NombreCentroCosto", "ClientId", "ClientName", "DocumentId",
            "NumberId", "RowId", "Module", "ConceptId", "Debito", "Credito",
            "Ingreso", "Costo", "Gasto", "Observation", "Invoice"]
    return df[cols]


def construir_rentabilidad(hechos):
    """Ingreso / Costo / Margen por proyecto y periodo. Doble verificacion del total."""
    g = (hechos.groupby(["Gerencia", "Proyecto", "NombreCentroCosto", "Periodo", "Mes"],
                        as_index=False)[["Ingreso", "Costo", "Gasto"]].sum())
    g["Utilidad"] = g["Ingreso"] - g["Costo"]
    g["Margen"] = (g["Utilidad"] / g["Ingreso"]).where(g["Ingreso"] != 0)

    # Verificacion independiente: recalcular el total de ingreso por otra via.
    v1 = g["Ingreso"].sum()
    m4 = hechos[hechos["Clase"] == "4"]
    v2 = m4["Credito"].sum() - m4["Debito"].sum()
    if abs(v1 - v2) > 1.0:
        raise SystemExit("[FATAL] descuadre de ingreso: {:,.2f} vs {:,.2f}".format(v1, v2))
    print("[rentabilidad] {:,} filas proyecto-mes | ingreso total verificado ${:,.0f}".format(
        len(g), v1))
    return g


def construir_informe(rent):
    """La tabla exacta que pide el Caso Financiero: junio vs julio con semaforo."""
    piv = rent.pivot_table(index=["Gerencia", "Proyecto", "NombreCentroCosto"],
                           columns="Periodo", values=["Ingreso", "Costo"],
                           aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame(index=piv.index)
    for p, etq in ((6, "Junio"), (7, "Julio")):
        out["Ingreso" + etq] = piv[("Ingreso", p)] if ("Ingreso", p) in piv.columns else 0.0
        out["Costo" + etq] = piv[("Costo", p)] if ("Costo", p) in piv.columns else 0.0
        ing = out["Ingreso" + etq]
        out["Margen" + etq] = ((ing - out["Costo" + etq]) / ing).where(ing != 0)
    out = out.reset_index()
    out["Variacion"] = out["MargenJulio"] - out["MargenJunio"]
    out["VariacionIngreso"] = out["IngresoJulio"] - out["IngresoJunio"]
    out["Utilidad"] = out["IngresoJulio"] - out["CostoJulio"]

    def semaforo(v):
        if pd.isna(v):
            return "No determinable"
        if v > 0.005:
            return "Aumenta"
        if v < -0.005:
            return "Disminuye"
        return "Se mantiene"

    out["Observacion"] = out["Variacion"].map(semaforo)
    out = out.sort_values(["Gerencia", "Proyecto"]).reset_index(drop=True)
    print("[informe] {:,} proyectos junio-vs-julio".format(len(out)))
    return out


def construir_nomina(hechos):
    """Acumulados de nomina + puente al proyecto + marca de retroactividad.

    El acumulado NO trae ProjectId. El puente natural seria el asiento contable de
    nomina (Module='N'), pero `RowId` NO sirve como llave: el contable numera en
    pares (2,4,6...) y el acumulado en impares/consecutivos (1,3,5,6...). Cruzar
    por RowId deja el 97% de la nomina sin proyecto.

    El puente que si funciona es empleado x periodo: el 95,4% de los empleados
    carga a un solo proyecto en el mes y la cobertura es del 100%. Al 4,6%
    repartido entre varios proyectos se le asigna el de mayor costo y se marca en
    `ProyectoRepartido` para poder declararlo en la respuesta.
    """
    n_cont = hechos[hechos["Module"] == "N"].copy()
    por_emp = (n_cont.groupby(["ClientId", "Periodo", "Proyecto", "Gerencia"], as_index=False)
               ["Costo"].sum()
               .sort_values("Costo", ascending=False))
    puente = por_emp.drop_duplicates(subset=["ClientId", "Periodo"])[
        ["ClientId", "Periodo", "Proyecto", "Gerencia"]]
    repartidos = (n_cont.groupby(["ClientId", "Periodo"])["Proyecto"].nunique()
                  .rename("NProyectos").reset_index())

    partes = []
    for periodo, ruta in ACUMULADOS.items():
        d = pd.read_excel(ruta)
        d["Periodo"] = periodo
        d["Mes"] = MESES[periodo]
        partes.append(d)
    nom = pd.concat(partes, ignore_index=True)

    for c in ("ClientId", "DocumentId", "NumberId", "RowId"):
        nom[c] = nom[c].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)

    nom = nom.merge(puente, on=["ClientId", "Periodo"], how="left")
    nom = nom.merge(repartidos, on=["ClientId", "Periodo"], how="left")
    nom["ProyectoRepartido"] = nom["NProyectos"].fillna(0) > 1
    cobertura = nom["Proyecto"].notna().mean()

    # Retroactividad: el concepto se causa en Periodo pero se paga con PeriodoPago.
    nom["PeriodoPago"] = pd.to_numeric(nom.get("PeriodPayment"), errors="coerce")
    nom["EsRetroactivo"] = nom["PeriodoPago"].notna() & (nom["PeriodoPago"] != nom["Periodo"])
    nom["ConceptName"] = nom["ConceptName"].astype(str).str.strip()

    cols = ["Periodo", "Mes", "ClientId", "ClientName", "ConceptId", "ConceptName", "Quantity",
            "Value", "Debit", "Credit", "PeriodoPago", "EsRetroactivo", "Proyecto", "Gerencia",
            "ProyectoRepartido"]
    nom = nom[[c for c in cols if c in nom.columns]]
    print("[nomina] {:,} lineas | proyecto asignado a {:.1%} | {:,} retroactivas".format(
        len(nom), cobertura, int(nom["EsRetroactivo"].sum())))
    return nom


def construir_ausencias():
    partes = []
    for periodo, ruta in AUSENCIAS.items():
        d = pd.read_excel(ruta, sheet_name=" Data")
        d["Periodo"] = periodo
        d["Mes"] = MESES[periodo]
        partes.append(d)
    a = pd.concat(partes, ignore_index=True)
    a["Proyecto"] = a["ProjectId"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    for c in ("ConceptName", "ProjectName", "ClientName"):
        a[c] = a[c].astype(str).str.strip()
    for c in ("DateInitial", "DateFinal"):
        a[c] = pd.to_datetime(a[c], errors="coerce")
    cols = ["Periodo", "Mes", "ClientName", "ConceptId", "ConceptName", "DateInitial",
            "DateFinal", "Quantity", "DaysCalendary", "Proyecto", "ProjectName", "Observation"]
    a = a[[c for c in cols if c in a.columns]]
    print("[ausencias] {:,} novedades | {} proyectos".format(len(a), a["Proyecto"].nunique()))
    return a


MES_PAT = r"\b(ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)\b"
ORDEN_MES = {m: i + 1 for i, m in enumerate(
    ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
     "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"])}


def marcar_devengo(hechos):
    """Marca asientos cuya Observation menciona un mes ANTERIOR al de su periodo.

    En el contable la Fecha siempre cae dentro de su Period (verificado), asi que
    la unica huella de un ajuste retroactivo esta en el texto de la observacion.
    """
    obs = hechos["Observation"].fillna("").astype(str).str.upper()
    mes_txt = obs.str.extract(MES_PAT, expand=False)
    mes_num = mes_txt.map(ORDEN_MES)
    hechos = hechos.copy()
    hechos["MesMencionado"] = pd.to_numeric(mes_num, errors="coerce").astype("Int64")
    hechos["EsAjusteMesAnterior"] = (
        hechos["MesMencionado"].notna() & (hechos["MesMencionado"] < hechos["Periodo"])
    ).fillna(False)
    n = int(hechos["EsAjusteMesAnterior"].sum())
    monto = hechos.loc[hechos["EsAjusteMesAnterior"], ["Costo", "Gasto"]].sum().sum()
    print("[devengo] {:,} asientos mencionan un mes anterior | ${:,.0f} en costo+gasto".format(
        n, monto))
    return hechos


def escribir_json_dashboard(rent):
    """Series chicas que alimentan el dashboard: totales, gerencias y proyectos.

    El dashboard necesita los TRES meses (el informe del caso solo trae junio y
    julio), asi que se emiten aparte en vez de recalcularlos en el navegador.
    """
    def margen(ing, cos):
        return None if abs(ing) < 1 else (ing - cos) / ing

    # Totales de la compania por mes.
    t = rent.groupby("Periodo", as_index=False)[["Ingreso", "Costo"]].sum()
    totales = [{"periodo": int(r.Periodo), "mes": MESES[int(r.Periodo)],
                "ingreso": float(r.Ingreso), "costo": float(r.Costo),
                "utilidad": float(r.Ingreso - r.Costo),
                "margen": margen(r.Ingreso, r.Costo)} for r in t.itertuples()]

    # Gerencia x mes: la serie del grafico de evolucion.
    g = rent.groupby(["Gerencia", "Periodo"], as_index=False)[["Ingreso", "Costo"]].sum()
    gerencias = {}
    for r in g.itertuples():
        gerencias.setdefault(r.Gerencia, []).append(
            {"periodo": int(r.Periodo), "mes": MESES[int(r.Periodo)],
             "ingreso": float(r.Ingreso), "costo": float(r.Costo),
             "margen": margen(r.Ingreso, r.Costo)})
    for v in gerencias.values():
        v.sort(key=lambda x: x["periodo"])

    # Proyecto x mes: alimenta el comparador de proyectos.
    p = rent.groupby(["Gerencia", "Proyecto", "NombreCentroCosto", "Periodo"],
                     as_index=False)[["Ingreso", "Costo"]].sum()
    proyectos = {}
    for r in p.itertuples():
        d = proyectos.setdefault(r.Proyecto, {
            "proyecto": r.Proyecto, "gerencia": r.Gerencia,
            "nombre": r.NombreCentroCosto, "meses": []})
        d["meses"].append({"periodo": int(r.Periodo), "mes": MESES[int(r.Periodo)],
                           "ingreso": float(r.Ingreso), "costo": float(r.Costo),
                           "margen": margen(r.Ingreso, r.Costo)})
    for d in proyectos.values():
        d["meses"].sort(key=lambda x: x["periodo"])

    payload = {"totales": totales, "gerencias": gerencias,
               "proyectos": list(proyectos.values())}
    (SALIDA / "series.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("[dashboard] series.json: {} meses, {} gerencias, {} proyectos".format(
        len(totales), len(gerencias), len(proyectos)))


def escribir_manual(hechos, rent, informe, nomina, ausencias):
    ger = rent.groupby(["Gerencia", "Periodo"])[["Ingreso", "Costo"]].sum().reset_index()
    ger["MargenPct"] = (ger["Ingreso"] - ger["Costo"]) / ger["Ingreso"] * 100
    tabla = ger.pivot(index="Gerencia", columns="Periodo", values="MargenPct").round(1)
    ing = ger.pivot(index="Gerencia", columns="Periodo", values="Ingreso")

    filas = []
    for g in tabla.index:
        celdas = []
        for p in (5, 6, 7):
            v = tabla.loc[g, p] if p in tabla.columns else None
            celdas.append("{:.1f}%".format(v) if pd.notna(v) else "n/d")
        ij = ing.loc[g, 7] if 7 in ing.columns and pd.notna(ing.loc[g, 7]) else 0
        filas.append("| {} | {} | ${:,.0f} |".format(g, " | ".join(celdas), ij))
    tabla_md = "\n".join(filas)

    md = """# MANUAL DE DATOS - Quick Logistica, mayo-julio 2026

Este manual describe las tablas YA CALCULADAS. Los numeros de aqui son de
contexto: para responder SIEMPRE ejecuta codigo, nunca cites de memoria.

## GLOSARIO - como habla el negocio (leelo antes de elegir el nivel de analisis)

Confundir estos terminos hace que respondas al nivel equivocado, que es el error
mas caro posible: la cifra sale bien pero contesta otra pregunta.

| Cuando el usuario dice... | Se refiere a | Columna |
|---|---|---|
| **LINEA**, linea de negocio, unidad, gerencia, "la linea de Warehouse" | Una de las 5 gerencias operativas | `Gerencia` |
| **PROYECTO**, centro de costo, "el 594", "Proyecto 1030" | Uno de los 468 proyectos | `Proyecto` |
| **LA COMPANIA**, el total, consolidado | Todo junto | sin agrupar |

Las LINEAS son exactamente estas cinco: LAST MILE COLOMBIA, FIRST MILE,
LONG HAUL, WAREHOUSE y COURIER COLOMBIA. GERENCIAS y GLOBAL COLOMBIA NO son
lineas de negocio (ver trampas 3 y abajo).

Regla practica: si la pregunta dice "linea", agrupa por `Gerencia` y responde con
el NOMBRE DE UNA GERENCIA. Nunca respondas con un proyecto a una pregunta sobre
lineas, ni al reves. Si de verdad es ambiguo, responde al nivel que pidieron y
agrega una linea con el hallazgo del otro nivel.

## Tablas disponibles en el sandbox (ya cargadas como DataFrames de pandas)

### `hechos` - {n_hechos:,} filas. Movimiento contable normalizado.
Columnas: Periodo (5/6/7), Mes, Fecha, Clase, AccountId, AccountName, Gerencia,
Proyecto, NombreCentroCosto, ClientId, ClientName, DocumentId, NumberId, RowId,
Module, ConceptId, Debito, Credito, **Ingreso**, **Costo**, **Gasto**,
Observation, Invoice, MesMencionado, EsAjusteMesAnterior.

Convencion de signo YA APLICADA (no la vuelvas a aplicar):
- `Ingreso` = Credito - Debito, solo Clase 4. Las notas credito ya vienen netas.
- `Costo`   = Debito - Credito, solo Clases 6 y 7 (costo de proyecto).
- `Gasto`   = Debito - Credito, solo Clase 5 (gasto administrativo; NO entra al
  margen del caso, que se define como (Ingreso - Costo) / Ingreso).

Clases de cuenta = primer digito de AccountId: 4 Ingresos | 5 Gastos |
6 Costo de mercancia (solo Warehouse) | 7 Costos de proyecto.

### `rentabilidad` - {n_rent:,} filas. Una por proyecto x periodo.
Gerencia, Proyecto, NombreCentroCosto, Periodo, Mes, Ingreso, Costo, Gasto,
Utilidad, Margen. `Margen` es fraccion (0.138 = 13.8%), NaN si Ingreso = 0.

### `informe` - {n_inf:,} filas. La tabla del Caso Financiero, junio vs julio.
Gerencia, Proyecto, NombreCentroCosto, IngresoJunio, CostoJunio, MargenJunio,
IngresoJulio, CostoJulio, MargenJulio, Variacion (diferencia de margen en
fraccion), VariacionIngreso, Utilidad (= IngresoJulio - CostoJulio), Observacion
(Aumenta / Disminuye / Se mantiene, umbral +-0.5 pp).

### `nomina` - {n_nom:,} filas. Acumulados de nomina de los 3 meses.
Periodo, Mes, ClientId, ClientName, ConceptId, ConceptName, Quantity, Value,
Debit, Credit, PeriodoPago, **EsRetroactivo**, Proyecto, Gerencia.
`EsRetroactivo` = el concepto se causa en `Periodo` pero se paga en un
`PeriodoPago` distinto. Es la huella dura del gasto retroactivo.
El acumulado original no trae proyecto; se asigno via el asiento contable de
nomina (Module='N'). Las filas sin proyecto quedan NaN: declaralo si afecta.

### `ausencias` - {n_aus:,} filas. Novedades de personal, CON proyecto.
Periodo, Mes, ClientName, ConceptId, ConceptName, DateInitial, DateFinal,
Quantity, DaysCalendary, Proyecto, ProjectName, Observation.
Conceptos: incapacidades EPS/laborales, licencias remuneradas y no remuneradas,
vacaciones, suspensiones, calamidad domestica.

## Modulo `helper` (ya importado en el sandbox)
Funciones verificadas. Usalas antes de escribir agregaciones a mano.

## Contexto de magnitud (para juzgar materialidad)

| Gerencia | Margen mayo | junio | julio | Ingreso julio |
|---|---|---|---|---|
{tabla_md}

La compania factura ~$24.500 millones COP al mes. Una gerencia que mueve $4
millones es INMATERIAL aunque su margen salte 37 puntos: dilo explicitamente en
vez de nombrarla como "la mejor evolucion".

## Trampas verificadas en estos datos
1. En el contable, `Fecha` SIEMPRE cae dentro de su `Periodo`. No busques ahi el
   ajuste retroactivo: esta en `nomina.EsRetroactivo` y en
   `hechos.EsAjusteMesAnterior` (mes anterior mencionado en Observation).
2. Dos asientos venian partidos por un ';' dentro de Observation. Ya reparados.
3. La gerencia GERENCIAS no tiene ingreso operativo: su margen es artificial.
   Sus cuentas clase 5 son el gasto administrativo de la compania.
4. Hay proyectos con Ingreso 0 y Costo > 0 (centros de costo puros): su margen es
   NaN, no menos infinito. No los reportes como "peor rentabilidad".
5. Solo hay datos de mayo, junio y julio de 2026. Agosto NO existe: cualquier
   pregunta sobre agosto es una proyeccion con supuesto declarado, no un dato.
""".format(n_hechos=len(hechos), n_rent=len(rent), n_inf=len(informe),
           n_nom=len(nomina), n_aus=len(ausencias), tabla_md=tabla_md)

    (SALIDA / "MANUAL_DATOS.md").write_text(md, encoding="utf-8")
    return md


def main():
    SALIDA.mkdir(exist_ok=True)
    hechos = construir_hechos()
    hechos = marcar_devengo(hechos)
    rent = construir_rentabilidad(hechos)
    informe = construir_informe(rent)
    nomina = construir_nomina(hechos)
    ausencias = construir_ausencias()

    hechos.to_parquet(SALIDA / "hechos.parquet", index=False)
    rent.to_parquet(SALIDA / "rentabilidad.parquet", index=False)
    informe.to_parquet(SALIDA / "informe.parquet", index=False)
    nomina.to_parquet(SALIDA / "nomina.parquet", index=False)
    ausencias.to_parquet(SALIDA / "ausencias.parquet", index=False)

    registros = json.loads(informe.to_json(orient="records", date_format="iso"))
    (SALIDA / "informe.json").write_text(
        json.dumps(registros, ensure_ascii=False), encoding="utf-8")

    escribir_json_dashboard(rent)

    escribir_manual(hechos, rent, informe, nomina, ausencias)

    print("\n=== CONTROL: margen % por gerencia (5=may, 6=jun, 7=jul) ===")
    g = rent.groupby(["Gerencia", "Periodo"])[["Ingreso", "Costo"]].sum()
    g["MargenPct"] = ((g["Ingreso"] - g["Costo"]) / g["Ingreso"] * 100).round(1)
    print(g["MargenPct"].unstack().to_string())
    print("\nOK -> {}".format(SALIDA))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
