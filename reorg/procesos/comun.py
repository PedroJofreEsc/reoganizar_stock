"""Piezas compartidas por los procesos de traslado y transformación."""

from __future__ import annotations

from reorg.dominio import AsignacionBin, DocCreado, Estado, Gateway, SesionExpirada, SinRespuesta
from reorg.procesos import excel


def enviar(gateway: Gateway, tipo: str, archivo: str) -> tuple[str, DocCreado | None, str, bool]:
    """Envía un JSON ya guardado. Devuelve (estado, documento, error, detener)."""
    try:
        return Estado.OK.value, gateway.enviar(tipo, archivo), "", False
    except SesionExpirada as e:        # 401: no se creó; no tiene sentido seguir
        return Estado.ERROR.value, None, str(e), True
    except SinRespuesta as e:          # no se sabe si se creó
        return Estado.REVISAR.value, None, str(e), False
    except Exception as e:             # SAP lo rechazó: no se creó
        return Estado.ERROR.value, None, str(e), False


def redondear(x: float):
    """int si es entero; si no, 6 decimales (para logs)."""
    x = round(float(x), 6)
    return int(x) if x.is_integer() else x


def asignaciones(filas: list[dict], col_bin: str, orden_lotes: dict[str, int],
                 col_lote: str = "LOTE") -> list[AsignacionBin]:
    """Suma por bin (y por lote si `orden_lotes` no está vacío), ordenado según el orden de lotes."""
    if orden_lotes:
        g = sorted(excel.sumar(filas, col_lote, col_bin).items(), key=lambda kv: orden_lotes[kv[0][0]])
        return [AsignacionBin(b, q, lote=l) for (l, b), q in g]
    return [AsignacionBin(b, q) for (b,), q in excel.sumar(filas, col_bin).items()]


def resolver_bins(gateway: Gateway, bodega: str, asigs: list[AsignacionBin], contexto: str) -> list[str]:
    """Completa bin_abs en cada asignación. Devuelve los errores (no corta)."""
    errores = []
    for a in asigs:
        try:
            a.bin_abs = gateway.resolver_bin(bodega, a.bin_code)
        except ValueError as e:
            msg = f"{contexto}: {e}"
            if msg not in errores:
                errores.append(msg)
    return errores
