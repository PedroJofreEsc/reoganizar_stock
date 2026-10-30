"""EM directa: una Entrada de Mercancía de una línea por artículo, sin SM de origen.

Excel, una fila por lote y bin:
    ARTICULO, BODEGA, LOTE, VENCIMIENTO, CANTIDAD, BIN, CUENTA, DIM1..DIM5, COMENTARIO (opcional)
No se envía UnitPrice: SAP asigna el costo. Después de crear cada EM se lee el documento
para registrar el precio y el total que usó SAP.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from reorg.dominio import FINALES, REENVIABLES, EntradaDirecta, ErrorSAP, ErrorValidacion, Estado, Gateway, ordenar_lotes
from reorg.procesos import excel, registro
from reorg.procesos.comun import asignaciones, enviar, resolver_bins
from reorg.procesos.registro import CargaEntrada
from reorg.procesos.transformacion import avisos_vencimiento

COLUMNAS = ["ARTICULO", "BODEGA", "LOTE", "VENCIMIENTO", "CANTIDAD", "BIN", "CUENTA"]
DIMS = ["DIM1", "DIM2", "DIM3", "DIM4", "DIM5"]
OPCIONALES = ["COMENTARIO"]
MAX_COMENTARIO = 254

Progreso = Callable[[int, int, dict], None] | None


@dataclass
class PlanEntrada:
    items: list[EntradaDirecta]
    avisos: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Preparar: leer, validar, resolver bins (solo lecturas a SAP)
# --------------------------------------------------------------------------- #
def preparar(origen, gateway: Gateway, company_db: str | None = None, dias_minimos: int = 90) -> PlanEntrada:
    filas = excel.leer_filas(origen, COLUMNAS, DIMS + OPCIONALES)
    errores = excel.obligatorias(filas, ["ARTICULO", "BODEGA", "LOTE", "BIN", "CUENTA"])
    errores += [f"Fila {f['FILA']}: COMENTARIO tiene {len(f['COMENTARIO'])} caracteres (máximo {MAX_COMENTARIO})"
                for f in filas if len(f["COMENTARIO"]) > MAX_COMENTARIO]
    for f in filas:
        try:
            f["VENCIMIENTO"] = excel.fecha(f["VENCIMIENTO"])
        except ValueError as e:
            errores.append(f"Fila {f['FILA']}: {e}")
    if errores:
        raise ErrorValidacion(errores)

    plan = PlanEntrada([])
    for (bodega, articulo), fa in excel.agrupar(filas, "BODEGA", "ARTICULO").items():
        ctx = f"{bodega} / {articulo}"
        try:
            en = _entrada(articulo, bodega, fa)
        except ValueError as e:
            errores.append(f"{ctx}: {e}")
            continue
        errores_bins = resolver_bins(gateway, bodega, en.entradas, ctx)
        if errores_bins:
            errores += errores_bins
            continue
        plan.avisos += _avisar_lotes_existentes(en, gateway, ctx)
        plan.avisos += avisos_vencimiento(en.lotes, dias_minimos, ctx)
        plan.items.append(en)
    if errores:
        raise ErrorValidacion(errores)
    if company_db:
        avisar_duplicados(plan, company_db)
    return plan


def _unico(filas: list[dict], col: str) -> str:
    vals = list(dict.fromkeys(f[col] for f in filas))
    if len(vals) != 1:
        raise ValueError(f"{col} debe ser igual en todas las filas del artículo (hay {vals})")
    return vals[0]


def _entrada(articulo: str, bodega: str, filas: list[dict]) -> EntradaDirecta:
    cuenta = _unico(filas, "CUENTA")
    dims = [_unico(filas, d) for d in DIMS]
    comentario = _unico(filas, "COMENTARIO")
    lotes = ordenar_lotes({l: q for (l,), q in excel.sumar(filas, "LOTE").items()})
    for lt in lotes:
        venc = list(dict.fromkeys(f["VENCIMIENTO"] for f in filas if f["LOTE"] == lt.lote))
        if len(venc) != 1:
            raise ValueError(f"el lote {lt.lote} tiene más de una fecha de vencimiento {venc}")
        lt.vencimiento = venc[0]
    return EntradaDirecta(articulo, bodega, sum(f["CANTIDAD"] for f in filas), cuenta, dims, lotes,
                          asignaciones(filas, "BIN", {l.lote: i for i, l in enumerate(lotes)}, "LOTE"),
                          comentario)


def _avisar_lotes_existentes(en: EntradaDirecta, gateway: Gateway, ctx: str) -> list[str]:
    try:
        existentes = {l.upper(): f for l, f in gateway.lotes_existentes(en.articulo).items()}
    except ErrorSAP as e:
        return [f"{ctx}: no se pudo verificar si los lotes ya existen ({e})"]
    return [f"{ctx}: el lote {lt.lote} ya existe en SAP (vence {existentes[lt.lote.upper()] or 'sin fecha'}). "
            f"La EM suma stock a ese lote y se mantiene la fecha de SAP, no la del Excel ({lt.vencimiento})."
            for lt in en.lotes if lt.lote.upper() in existentes]


def avisar_duplicados(plan: PlanEntrada, company_db: str):
    """Aviso (no bloquea): EM directa idéntica (sin contar el comentario) ya creada en otra carga."""
    previas = registro.entradas_registradas(company_db)
    for en in plan.items:
        h = registro.huella_transformacion(en.to_dict())
        for carga_id, _, e in previas:
            if e["huella"] == h:
                plan.avisos.append(f"{en.bodega} / {en.articulo}: EM idéntica a la de la carga {carga_id} "
                                   f"(DocNum {e['em']['doc_num']}). Ejecutarla crea otra EM.")


# --------------------------------------------------------------------------- #
# Crear, reenviar y cerrar
# --------------------------------------------------------------------------- #
def crear(plan: PlanEntrada, carga: CargaEntrada, gateway: Gateway, on_progreso: Progreso = None) -> list[dict]:
    """Registra los artículos del plan en la carga (nueva) y envía sus EM."""
    for en in plan.items:
        carga.registrar(en)
    carga.guardar()
    return _enviar(carga, [en.clave for en in plan.items], gateway, on_progreso)


def enviables(carga: CargaEntrada, incluir_revisar: bool = False) -> list[str]:
    estados = ("",) + REENVIABLES + ((Estado.REVISAR.value,) if incluir_revisar else ())
    return [k for k, e in carga.pendientes() if e["em"]["estado"] in estados]


def reenviar(carga: CargaEntrada, gateway: Gateway, incluir_revisar: bool = False,
             on_progreso: Progreso = None) -> list[dict]:
    """Reenvía con el mismo JSON las EM de la carga que no se crearon."""
    return _enviar(carga, enviables(carga, incluir_revisar), gateway, on_progreso)


def _enviar(carga: CargaEntrada, claves: list[str], gateway: Gateway, on_progreso: Progreso) -> list[dict]:
    log, detenido = [], False
    for i, clave in enumerate(claves, start=1):
        e = carga.get(clave)
        em = e["em"]
        if detenido:
            if not em["estado"]:
                em["estado"] = Estado.NO_EJECUTADO.value
            fila = carga.fila_log(clave)
            fila["ESTADO"] = Estado.NO_EJECUTADO.value
        else:
            if not em["payload"]:
                em["payload"] = gateway.guardar_entrada(carga.entrada(e))
            em.update(estado=Estado.ENVIANDO.value, error="", fecha=registro.ahora(), intentos=em["intentos"] + 1)
            carga.guardar()
            estado, r, error, detenido = enviar(gateway, "em", em["payload"])
            em.update(estado=estado, error=error)
            if r:
                em.update(doc_entry=r.doc_entry, doc_num=r.doc_num, doc_date=r.doc_date)
                carga.guardar()
                _leer_precio_sap(em, gateway)
            fila = carga.fila_log(clave)
        carga.guardar()
        log.append(fila)
        if on_progreso:
            on_progreso(i, len(claves), fila)
    return log


def _leer_precio_sap(em: dict, gateway: Gateway):
    """Registra el precio y el total que SAP asignó a la EM (no envía UnitPrice)."""
    try:
        r = gateway.leer_em(int(em["doc_entry"]))
        em.update(precio_sap=r["precio"], total_sap=r["total_linea"])
    except (ErrorSAP, ValueError, KeyError) as ex:
        em["error"] = f"EM creada; no se pudo leer el precio asignado por SAP ({ex})"


def cerrables(carga: CargaEntrada) -> list[str]:
    return [k for k, e in carga.pendientes() if e["em"]["estado"] not in FINALES]


def cerrar(carga: CargaEntrada, claves: list[str], usuario: str, motivo: str) -> list[dict]:
    if not motivo.strip():
        raise ValueError("indica el motivo del cierre")
    validos = set(cerrables(carga))
    filas = []
    for clave in claves:
        if clave in validos:
            registro.cerrar_documento(carga.get(clave)["em"], usuario, motivo)
            filas.append(carga.fila_log(clave))
    carga.guardar()
    return filas
