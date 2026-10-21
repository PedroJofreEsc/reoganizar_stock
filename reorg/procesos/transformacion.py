"""Transformación de lotes: una SM + una EM por artículo, en dos etapas, dentro de una carga.

Excel, una fila por movimiento de lote:
    ARTICULO, BODEGA, LOTE_ORIGEN, LOTE_DESTINO, VENCIMIENTO, CANTIDAD, BIN_ORIGEN, CUENTA, DIM1..DIM5
Etapa 1, SM (una línea): salen los lotes de origen desde BIN_ORIGEN.
Etapa 2, EM (una línea, asociada a la SM): entran los lotes destino en el mismo BIN_ORIGEN,
al costo unitario real leído del asiento de la SM (promedio si hay varios lotes de origen).

Cada "Crear SM" abre una carga nueva (registro.CargaTransformacion). Reenvíos y etapa EM se
hacen sobre la carga, sin volver a subir el Excel.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from reorg.dominio import FINALES, REENVIABLES, ErrorSAP, ErrorValidacion, Estado, Gateway, Transformacion, ordenar_lotes
from reorg.procesos import costo, excel, registro
from reorg.procesos.comun import asignaciones, enviar, resolver_bins
from reorg.procesos.registro import CargaTransformacion

COLUMNAS = ["ARTICULO", "BODEGA", "LOTE_ORIGEN", "LOTE_DESTINO", "VENCIMIENTO", "CANTIDAD", "BIN_ORIGEN", "CUENTA"]
DIMS = ["DIM1", "DIM2", "DIM3", "DIM4", "DIM5"]
OPCIONALES = ["COMENTARIO"]  # va en Comments de la SM y de la EM; igual en todas las filas del artículo
MAX_COMENTARIO = 254

Progreso = Callable[[int, int, dict], None] | None


@dataclass
class PlanTransformacion:
    items: list[Transformacion]
    avisos: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Preparar: leer, validar, resolver bins (solo lecturas a SAP)
# --------------------------------------------------------------------------- #
def preparar(origen, gateway: Gateway, company_db: str | None = None,
             dias_minimos: int = 90) -> PlanTransformacion:
    filas = excel.leer_filas(origen, COLUMNAS, DIMS + OPCIONALES)
    errores = excel.obligatorias(filas, ["ARTICULO", "BODEGA", "LOTE_ORIGEN", "LOTE_DESTINO", "BIN_ORIGEN", "CUENTA"])
    errores += [f"Fila {f['FILA']}: COMENTARIO tiene {len(f['COMENTARIO'])} caracteres (máximo {MAX_COMENTARIO})"
                for f in filas if len(f["COMENTARIO"]) > MAX_COMENTARIO]
    for f in filas:
        try:
            f["VENCIMIENTO"] = excel.fecha(f["VENCIMIENTO"])
        except ValueError as e:
            errores.append(f"Fila {f['FILA']}: {e}")
        if f["LOTE_ORIGEN"] and f["LOTE_ORIGEN"].upper() == f["LOTE_DESTINO"].upper():
            errores.append(f"Fila {f['FILA']}: LOTE_DESTINO no puede ser igual a LOTE_ORIGEN")
    if errores:
        raise ErrorValidacion(errores)

    plan = PlanTransformacion([])
    for (bodega, articulo), fa in excel.agrupar(filas, "BODEGA", "ARTICULO").items():
        ctx = f"{bodega} / {articulo}"
        try:
            t = _transformacion(articulo, bodega, fa)
        except ValueError as e:
            errores.append(f"{ctx}: {e}")
            continue
        errores_bins = resolver_bins(gateway, bodega, t.salidas_sm + t.entradas_em, ctx)
        if errores_bins:
            errores += errores_bins
            continue
        plan.avisos += _avisar_lotes_existentes(t, gateway, ctx)
        plan.avisos += avisos_vencimiento(t.lotes_destino, dias_minimos, ctx)
        plan.items.append(t)
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


def _transformacion(articulo: str, bodega: str, filas: list[dict]) -> Transformacion:
    cuenta = _unico(filas, "CUENTA")
    dims = [_unico(filas, d) for d in DIMS]
    comentario = _unico(filas, "COMENTARIO")
    origen = ordenar_lotes({l: q for (l,), q in excel.sumar(filas, "LOTE_ORIGEN").items()})
    destino = ordenar_lotes({l: q for (l,), q in excel.sumar(filas, "LOTE_DESTINO").items()})
    cruzados = {l.lote.upper() for l in origen} & {l.lote.upper() for l in destino}
    if cruzados:
        raise ValueError(f"lote(s) {sorted(cruzados)} aparecen como origen y como destino")
    for lt in destino:
        venc = list(dict.fromkeys(f["VENCIMIENTO"] for f in filas if f["LOTE_DESTINO"] == lt.lote))
        if len(venc) != 1:
            raise ValueError(f"el lote destino {lt.lote} tiene más de una fecha de vencimiento {venc}")
        lt.vencimiento = venc[0]
    return Transformacion(
        articulo, bodega, sum(f["CANTIDAD"] for f in filas), cuenta, dims, origen, destino,
        salidas_sm=asignaciones(filas, "BIN_ORIGEN", {l.lote: i for i, l in enumerate(origen)}, "LOTE_ORIGEN"),
        entradas_em=asignaciones(filas, "BIN_ORIGEN", {l.lote: i for i, l in enumerate(destino)}, "LOTE_DESTINO"),
        comentario=comentario)


def _avisar_lotes_existentes(t: Transformacion, gateway: Gateway, ctx: str) -> list[str]:
    try:
        existentes = {l.upper(): f for l, f in gateway.lotes_existentes(t.articulo).items()}
    except ErrorSAP as e:
        return [f"{ctx}: no se pudo verificar si los lotes destino ya existen ({e})"]
    return [f"{ctx}: el lote destino {lt.lote} ya existe en SAP (vence {existentes[lt.lote.upper()] or 'sin fecha'}). "
            f"Se mantiene la fecha de SAP, no la del Excel ({lt.vencimiento})."
            for lt in t.lotes_destino if lt.lote.upper() in existentes]


def vencen_antes(lotes_destino, dias_minimos: int) -> list[tuple[str, str]]:
    """[(lote, vencimiento)] de los lotes que vencen antes de hoy + dias_minimos (0 = no revisar)."""
    if dias_minimos <= 0:
        return []
    limite = (date.today() + timedelta(days=dias_minimos)).isoformat()
    out = []
    for l in lotes_destino:
        lote, venc = (l["lote"], l["vencimiento"]) if isinstance(l, dict) else (l.lote, l.vencimiento)
        if venc and venc < limite:
            out.append((lote, venc))
    return out


def avisos_vencimiento(lotes_destino, dias_minimos: int, ctx: str) -> list[str]:
    """Aviso (no bloquea): SAP rechazará la EM salvo autorización temporal."""
    return [f"{ctx}: el lote destino {lote} vence el {venc}, a menos de {dias_minimos} días. SAP rechazará la EM: "
            "si la fecha es correcta (real físico), solicita la autorización temporal en SAP antes de crear la EM."
            for lote, venc in vencen_antes(lotes_destino, dias_minimos)]


def avisar_duplicados(plan: PlanTransformacion, company_db: str):
    """Avisos (no bloquean) contra todas las cargas de la base:
    transformación idéntica ya hecha, o artículo con EM pendiente en otra carga."""
    previas = registro.transformaciones_registradas(company_db)
    for t in plan.items:
        h = registro.huella_transformacion(t.to_dict())
        for carga_id, clave, e in previas:
            if e["huella"] == h:
                plan.avisos.append(f"{t.bodega} / {t.articulo}: transformación idéntica a la de la carga {carga_id} "
                                   f"(SM {e['sm']['doc_num']}). Ejecutarla crea otra SM.")
            elif clave == t.clave and e["em"]["estado"] not in FINALES:
                plan.avisos.append(f"{t.bodega} / {t.articulo}: tiene la SM {e['sm']['doc_num']} con EM pendiente "
                                   f"en la carga {carga_id}.")


# --------------------------------------------------------------------------- #
# Etapa 1: SM
# --------------------------------------------------------------------------- #
def crear_sm(plan: PlanTransformacion, carga: CargaTransformacion, gateway: Gateway,
             on_progreso: Progreso = None) -> list[dict]:
    """Registra los artículos del plan en la carga (nueva) y envía sus SM."""
    for t in plan.items:
        carga.registrar(t)
    carga.guardar()
    return _enviar_sm(carga, [t.clave for t in plan.items], gateway, on_progreso)


def sm_enviables(carga: CargaTransformacion, incluir_revisar: bool = False) -> list[str]:
    estados = ("",) + REENVIABLES + ((Estado.REVISAR.value,) if incluir_revisar else ())
    return [k for k, e in carga.sm_pendientes() if e["sm"]["estado"] in estados]


def reenviar_sm(carga: CargaTransformacion, gateway: Gateway, incluir_revisar: bool = False,
                on_progreso: Progreso = None) -> list[dict]:
    """Reenvía con el mismo JSON las SM de la carga que no se crearon."""
    return _enviar_sm(carga, sm_enviables(carga, incluir_revisar), gateway, on_progreso)


def _enviar_sm(carga: CargaTransformacion, claves: list[str], gateway: Gateway, on_progreso: Progreso) -> list[dict]:
    log, detenido = [], False
    for i, clave in enumerate(claves, start=1):
        e = carga.get(clave)
        sm = e["sm"]
        if detenido:
            if not sm["estado"]:
                sm["estado"] = Estado.NO_EJECUTADO.value
            fila = carga.fila_log(clave, "SM")
            fila["ESTADO"] = Estado.NO_EJECUTADO.value
        else:
            if not sm["payload"]:
                sm["payload"] = gateway.guardar_sm(carga.transformacion(e))
            sm.update(estado=Estado.ENVIANDO.value, error="", fecha=registro.ahora(), intentos=sm["intentos"] + 1)
            carga.guardar()
            estado, r, error, detenido = enviar(gateway, "sm", sm["payload"])
            sm.update(estado=estado, error=error)
            if r:
                sm.update(doc_entry=r.doc_entry, doc_num=r.doc_num, trans_num=r.trans_num, doc_date=r.doc_date)
                carga.guardar()
                calcular_costo(e, gateway)
            fila = carga.fila_log(clave, "SM")
        carga.guardar()
        log.append(fila)
        if on_progreso:
            on_progreso(i, len(claves), fila)
    return log


# --------------------------------------------------------------------------- #
# Costo unitario desde el asiento de la SM
# --------------------------------------------------------------------------- #
def calcular_costo(e: dict, gateway: Gateway):
    """Completa em.costo_unit y em.asiento_total. Si no se puede, deja el motivo en em.nota."""
    sm, em = e["sm"], e["em"]
    t = CargaTransformacion.transformacion(e)
    try:
        if not sm["trans_num"]:
            sm["trans_num"] = gateway.leer_transnum_sm(int(sm["doc_entry"]))
        asiento = gateway.leer_asiento(int(sm["trans_num"])) if sm["trans_num"] else []
        cu, nota = costo.costo_unitario(asiento, t.cuenta, t.cantidad)
        em.update(costo_unit=round(cu, 6), asiento_total=round(cu * t.cantidad, 2), nota=nota)
    except (ErrorSAP, ValueError) as ex:
        em.update(costo_unit=None, asiento_total=None, nota=f"Costo no calculado: {ex}")


def recalcular_costos(carga: CargaTransformacion, gateway: Gateway) -> int:
    """Calcula el costo de las EM pendientes que aún no lo tienen. Devuelve cuántas quedaron con costo."""
    n = 0
    for _, e in carga.em_pendientes():
        if e["em"]["costo_unit"] is None:
            calcular_costo(e, gateway)
            n += e["em"]["costo_unit"] is not None
    carga.guardar()
    return n


# --------------------------------------------------------------------------- #
# Etapa 2: EM
# --------------------------------------------------------------------------- #
def em_corregibles(carga: CargaTransformacion) -> list[str]:
    """EM que se pueden corregir: SM creada y EM no creada ni en duda (REVISAR podría estar creada)."""
    estados = ("",) + REENVIABLES
    return [k for k, e in carga.em_pendientes() if e["em"]["estado"] in estados]


def corregir_em(carga: CargaTransformacion, clave: str, cambios: dict[str, tuple[str, str]],
                gateway: Gateway | None = None, dias_minimos: int = 90) -> list[str]:
    """Corrige nombre y/o vencimiento de los lotes destino de una EM pendiente.

    `cambios`: {lote actual: (lote nuevo, vencimiento nuevo AAAA-MM-DD)}. Cantidades, bin, cuenta y
    dimensiones no se tocan (deben calzar con la SM ya creada). El JSON anterior queda guardado en
    payloads_anteriores y el próximo envío arma uno nuevo. Devuelve avisos (lote existente, vencimiento).
    """
    if clave not in em_corregibles(carga):
        raise ValueError(f"{clave}: la EM no se puede corregir (ya creada o en REVISAR)")
    e = carga.get(clave)
    d, em = e["datos"], e["em"]
    destino = d["lotes_destino"]
    actuales = {l["lote"] for l in destino}
    errores = []
    nuevos = {}
    for viejo, (nuevo, venc) in cambios.items():
        if viejo not in actuales:
            errores.append(f"el lote {viejo} no es lote destino de {clave}")
            continue
        nuevo = (nuevo or "").strip()
        if not nuevo:
            errores.append(f"el lote destino {viejo} no puede quedar vacío")
        try:
            venc = excel.fecha(venc)
        except ValueError as ex:
            errores.append(f"lote {viejo}: {ex}")
        nuevos[viejo] = (nuevo, venc)
    nombres = [nuevos.get(l["lote"], (l["lote"],))[0] for l in destino]
    if len({n.upper() for n in nombres}) != len(nombres):
        errores.append(f"los lotes destino quedarían repetidos: {nombres}")
    cruzados = {n.upper() for n in nombres} & {l["lote"].upper() for l in d["lotes_origen"]}
    if cruzados:
        errores.append(f"lote(s) {sorted(cruzados)} son también lote de origen")
    if errores:
        raise ErrorValidacion(errores)

    detalle = [f"{l['lote']} {l['vencimiento']} -> {nuevos[l['lote']][0]} {nuevos[l['lote']][1]}"
               for l in destino if l["lote"] in nuevos and nuevos[l["lote"]] != (l["lote"], l["vencimiento"])]
    if not detalle:
        return []
    renombre = {viejo: nuevo for viejo, (nuevo, _) in nuevos.items()}  # en una pasada (permite A<->B)
    for a in d["entradas_em"]:
        a["lote"] = renombre.get(a["lote"], a["lote"])
    for l in destino:
        if l["lote"] in nuevos:
            l["lote"], l["vencimiento"] = nuevos[l["lote"]]
    e["huella"] = registro.huella_transformacion(d)
    if em["payload"]:
        em.setdefault("payloads_anteriores", []).append(em["payload"])
        em["payload"] = ""  # el próximo envío arma un JSON nuevo
    em.setdefault("correcciones", []).append(f"{registro.ahora()}: " + "; ".join(detalle))
    em.update(estado="", error="")
    carga.guardar()

    t = carga.transformacion(e)
    avisos = avisos_vencimiento(t.lotes_destino, dias_minimos, f"{t.bodega} / {t.articulo}")
    if gateway is not None:
        avisos += _avisar_lotes_existentes(t, gateway, f"{t.bodega} / {t.articulo}")
    return avisos

def em_enviables(carga: CargaTransformacion, incluir_revisar: bool = False) -> list[str]:
    estados = ("",) + REENVIABLES + ((Estado.REVISAR.value,) if incluir_revisar else ())
    return [k for k, e in carga.em_pendientes() if e["em"]["estado"] in estados]


def crear_em(carga: CargaTransformacion, gateway: Gateway, claves: list[str],
             incluir_revisar: bool = False, on_progreso: Progreso = None) -> list[dict]:
    enviables = set(em_enviables(carga, incluir_revisar))
    claves = [k for k in claves if k in enviables]
    log, detenido = [], False
    for i, clave in enumerate(claves, start=1):
        e = carga.get(clave)
        sm, em = e["sm"], e["em"]
        if not detenido and em["costo_unit"] is None:
            calcular_costo(e, gateway)
            carga.guardar()
        if detenido or em["costo_unit"] is None:
            if detenido and not em["estado"]:
                em["estado"] = Estado.NO_EJECUTADO.value
            fila = carga.fila_log(clave, "EM")
            fila["ESTADO"] = Estado.NO_EJECUTADO.value if detenido else Estado.ERROR.value
            fila["ERROR"] = "" if detenido else em["nota"]
        else:
            if not em["payload"]:
                em["payload"] = gateway.guardar_em(carga.transformacion(e), sm["doc_entry"], sm["doc_num"],
                                                   em["costo_unit"])
            em.update(estado=Estado.ENVIANDO.value, error="", fecha=registro.ahora(), intentos=em["intentos"] + 1)
            carga.guardar()
            estado, r, error, detenido = enviar(gateway, "em", em["payload"])
            em.update(estado=estado, error=error)
            if r:
                em.update(doc_entry=r.doc_entry, doc_num=r.doc_num, doc_date=r.doc_date)
            fila = carga.fila_log(clave, "EM")
        carga.guardar()
        log.append(fila)
        if on_progreso:
            on_progreso(i, len(claves), fila)
    return log


# --------------------------------------------------------------------------- #
# Cerrar SM o EM que no se crearon (dejan de quedar pendientes)
# --------------------------------------------------------------------------- #
def cerrables(carga: CargaTransformacion) -> list[tuple[str, str]]:
    """[(clave, "SM"|"EM")] de los documentos pendientes que se pueden cerrar."""
    return [(k, "SM") for k, _ in carga.sm_pendientes()] + [(k, "EM") for k, _ in carga.em_pendientes()]


def cerrar(carga: CargaTransformacion, documentos: list[tuple[str, str]], usuario: str, motivo: str) -> list[dict]:
    """Cierra SM o EM no creadas. Cerrar una EM con su SM creada deja esa mercadería fuera de stock:
    se debe resolver en SAP. Devuelve las filas para el log."""
    if not motivo.strip():
        raise ValueError("indica el motivo del cierre")
    validos = set(cerrables(carga))
    filas = []
    for clave, etapa in documentos:
        if (clave, etapa) in validos:
            registro.cerrar_documento(carga.get(clave)[etapa.lower()], usuario, motivo)
            filas.append(carga.fila_log(clave, etapa))
    carga.guardar()
    return filas
