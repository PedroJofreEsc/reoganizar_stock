"""Traslado de stock entre ubicaciones de una misma bodega.

Se usa para consolidar a PISO (paso 1) y para redistribuir desde PISO (paso 3).
Excel, una fila por movimiento: ARTICULO, BODEGA, BIN_ORIGEN, BIN_DESTINO, CANTIDAD, LOTE.
Un documento por bodega con hasta `max_articulos` artículos (una línea por artículo).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from reorg.dominio import (REENVIABLES, DocTraslado, ErrorValidacion, Estado, Gateway, LineaTraslado,
                           ordenar_lotes)
from reorg.procesos import excel, registro
from reorg.procesos.comun import asignaciones, enviar, redondear, resolver_bins
from reorg.procesos.registro import huella

COLUMNAS = ["ARTICULO", "BODEGA", "BIN_ORIGEN", "BIN_DESTINO", "CANTIDAD", "LOTE"]

Progreso = Callable[[int, int, dict], None] | None


@dataclass
class PlanTraslado:
    docs: list[DocTraslado]
    avisos: list[str]


# --------------------------------------------------------------------------- #
# Preparar: leer, validar, resolver bins y armar documentos (solo lecturas a SAP)
# --------------------------------------------------------------------------- #
def preparar(origen, gateway: Gateway, max_articulos: int = 20, comentario: str = "") -> PlanTraslado:
    filas = excel.leer_filas(origen, COLUMNAS, crudas=())
    errores = excel.obligatorias(filas, ["ARTICULO", "BODEGA", "BIN_ORIGEN", "BIN_DESTINO"])
    if errores:
        raise ErrorValidacion(errores)

    avisos = []
    mismos = [f["FILA"] for f in filas if f["BIN_ORIGEN"].upper() == f["BIN_DESTINO"].upper()]
    if mismos:
        avisos.append(f"Se omiten {len(mismos)} fila(s) con BIN_ORIGEN = BIN_DESTINO: {mismos}")
        filas = [f for f in filas if f["FILA"] not in mismos]
    if not filas:
        raise ErrorValidacion(["No quedan movimientos después de omitir las filas con origen = destino"])

    docs = []
    for (bodega,), fb in excel.agrupar(filas, "BODEGA").items():
        lineas = []
        for (articulo,), fa in excel.agrupar(fb, "ARTICULO").items():
            ctx = f"{bodega} / {articulo}"
            try:
                linea = _linea(articulo, fa)
            except ValueError as e:
                errores.append(f"{ctx}: {e}")
                continue
            errores += resolver_bins(gateway, bodega, linea.salidas + linea.entradas, ctx)
            lineas.append(linea)
        for n, ini in enumerate(range(0, len(lineas), max_articulos), start=1):
            docs.append(DocTraslado(f"{bodega}_{n:03d}", bodega,
                                    comentario or f"Traslado ubicaciones {bodega} doc {n}",
                                    lineas[ini:ini + max_articulos]))
    if errores:
        raise ErrorValidacion(errores)
    return PlanTraslado(docs, avisos)


def _linea(articulo: str, filas: list[dict]) -> LineaTraslado:
    con_lote = [bool(f["LOTE"]) for f in filas]
    if any(con_lote) and not all(con_lote):
        raise ValueError("hay filas con y sin LOTE")
    lotes = ordenar_lotes({l: q for (l,), q in excel.sumar(filas, "LOTE").items()}) if all(con_lote) else []
    orden = {lt.lote: i for i, lt in enumerate(lotes)}
    return LineaTraslado(articulo, sum(f["CANTIDAD"] for f in filas), lotes,
                         asignaciones(filas, "BIN_ORIGEN", orden),
                         asignaciones(filas, "BIN_DESTINO", orden))


# --------------------------------------------------------------------------- #
# Duplicados: documentos idénticos ya creados en otras cargas
# --------------------------------------------------------------------------- #
def huella_doc(doc: DocTraslado) -> str:
    """Contenido del documento sin el ID ni el comentario."""
    return huella({"bodega": doc.bodega, "lineas": [asdict(l) for l in doc.lineas]})


def avisar_duplicados(plan: PlanTraslado, company_db: str):
    """Agrega a plan.avisos los documentos idénticos a uno ya creado en cualquier carga de la base."""
    creados = registro.traslados_creados(company_db)
    for doc in plan.docs:
        for carga_id, d in creados.get(huella_doc(doc), []):
            plan.avisos.append(f"{doc.id}: es idéntico al documento {d['doc']} ya creado en la carga {carga_id} "
                               f"(DocNum {d['doc_num']}). Ejecutarlo lo crea otra vez.")


# --------------------------------------------------------------------------- #
# Ejecutar: guarda el JSON de cada documento en la carga y lo envía
# --------------------------------------------------------------------------- #
def ejecutar(plan: PlanTraslado, carga: registro.CargaTraslado, gateway: Gateway,
             on_progreso: Progreso = None) -> list[dict]:
    for doc in plan.docs:
        carga.docs.append({"doc": doc.id, "bodega": doc.bodega, "n_articulos": len(doc.lineas),
                           "articulos": ", ".join(doc.articulos), "cantidad": redondear(doc.cantidad),
                           "huella": huella_doc(doc), "payload": gateway.guardar_traslado(doc),
                           "estado": "", "doc_entry": None, "doc_num": None, "error": "", "intentos": 0,
                           "fecha": ""})
    carga.guardar()
    return _enviar(carga, carga.docs, gateway, on_progreso)


def reenviar(carga: registro.CargaTraslado, gateway: Gateway, incluir_revisar: bool = False,
             on_progreso: Progreso = None) -> list[dict]:
    """Reenvía el mismo JSON de los documentos de la carga que no quedaron OK.

    ERROR y NO_EJECUTADO siempre; REVISAR (sin respuesta) solo si `incluir_revisar`,
    después de comprobar en SAP que no se creó.
    """
    return _enviar(carga, [d for d in carga.docs if d["estado"] in enviables(incluir_revisar)], gateway,
                   on_progreso)


def enviables(incluir_revisar: bool = False) -> tuple[str, ...]:
    return ("",) + REENVIABLES + ((Estado.REVISAR.value,) if incluir_revisar else ())


def _enviar(carga: registro.CargaTraslado, docs: list[dict], gateway: Gateway, on_progreso: Progreso) -> list[dict]:
    filas, detenido = [], False
    for i, d in enumerate(docs, start=1):
        if detenido:
            if not d["estado"]:
                d["estado"] = Estado.NO_EJECUTADO.value
            fila = carga.fila_log(d, carga.id)
            fila["ESTADO"] = Estado.NO_EJECUTADO.value
        else:
            d.update(estado=Estado.ENVIANDO.value, error="", fecha=registro.ahora(), intentos=d["intentos"] + 1)
            carga.guardar()
            estado, r, error, detenido = enviar(gateway, "traslado", d["payload"])
            d.update(estado=estado, error=error)
            if r:
                d.update(doc_entry=r.doc_entry, doc_num=r.doc_num)
            fila = carga.fila_log(d, carga.id)
        carga.guardar()
        filas.append(fila)
        if on_progreso:
            on_progreso(i, len(docs), fila)
    return filas
