"""Lectura y validación básica del Excel de entrada (primera hoja)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from reorg.dominio import ErrorValidacion


def texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def cantidad(v) -> float:
    try:
        q = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"CANTIDAD inválida: {v!r}")
    if pd.isna(q) or q <= 0:
        raise ValueError(f"CANTIDAD debe ser mayor que 0 (hay {v!r})")
    return q


def fecha(v) -> str:
    """Fecha Excel, AAAA-MM-DD, DD-MM-AAAA, DD/MM/AAAA o AAAA/MM/DD -> AAAA-MM-DD."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        raise ValueError("falta fecha de VENCIMIENTO")
    if isinstance(v, (datetime, date, pd.Timestamp)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"fecha de VENCIMIENTO inválida: {v!r}")


def leer_filas(origen, requeridas: list[str], opcionales: list[str] = (),
               crudas: tuple[str, ...] = ("VENCIMIENTO",)) -> list[dict]:
    """Lee la primera hoja. Devuelve una lista de dicts con FILA (número de fila en Excel).

    Las columnas se normalizan a texto, salvo CANTIDAD (float) y las `crudas`.
    Junta todos los errores de columnas y cantidades en un ErrorValidacion.
    """
    df = pd.read_excel(origen, sheet_name=0, dtype=object)
    df.columns = [str(c).strip().upper() for c in df.columns]
    faltan = [c for c in requeridas if c not in df.columns]
    if faltan:
        raise ErrorValidacion([f"Faltan columnas en el Excel: {', '.join(faltan)}"])

    filas, errores = [], []
    for i, r in df.iterrows():
        if r.isna().all():
            continue
        f = {"FILA": int(i) + 2}
        for c in list(requeridas) + list(opcionales):
            v = r[c] if c in df.columns else None
            f[c] = v if c in crudas else texto(v)
        try:
            f["CANTIDAD"] = cantidad(r["CANTIDAD"])
        except ValueError as e:
            errores.append(f"Fila {f['FILA']}: {e}")
        filas.append(f)
    if not filas:
        errores.append("El Excel no tiene filas con datos")
    if errores:
        raise ErrorValidacion(errores)
    return filas


def obligatorias(filas: list[dict], columnas: list[str]) -> list[str]:
    """Errores por celdas vacías en columnas obligatorias."""
    return [f"Fila {f['FILA']}: {c} vacío" for f in filas for c in columnas if not f[c]]


def agrupar(filas: list[dict], *claves: str) -> dict[tuple, list[dict]]:
    """Agrupa filas manteniendo el orden de aparición."""
    out: dict[tuple, list[dict]] = {}
    for f in filas:
        out.setdefault(tuple(f[c] for c in claves), []).append(f)
    return out


def sumar(filas: list[dict], *claves: str) -> dict[tuple, float]:
    """Suma CANTIDAD por claves, manteniendo el orden de aparición."""
    out: dict[tuple, float] = {}
    for f in filas:
        k = tuple(f[c] for c in claves)
        out[k] = out.get(k, 0.0) + f["CANTIDAD"]
    return out
