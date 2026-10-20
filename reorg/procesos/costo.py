"""Costo unitario de la EM a partir del asiento de la SM.

La SM tiene una sola línea, así que su asiento debe ser:
    débito  a la cuenta de contrapartida (CUENTA)  = valor de la salida
    crédito a la cuenta de inventario del artículo = mismo valor
Si el asiento no tiene esa forma no se calcula costo (y no se crea la EM).
"""

from __future__ import annotations

from reorg.dominio import LineaAsiento

TOL = 0.01  # pesos


def costo_unitario(asiento: list[LineaAsiento], cuenta_contrapartida: str,
                   cantidad: float) -> tuple[float, str]:
    """Devuelve (costo unitario, nota). ValueError si el asiento no cuadra."""
    if not asiento:
        return 0.0, "SM sin asiento contable: EM con costo 0"

    debe_contra = sum(l.debe for l in asiento if l.cuenta == cuenta_contrapartida)
    haber_inv = sum(l.haber for l in asiento if l.cuenta != cuenta_contrapartida)
    if debe_contra <= TOL:
        raise ValueError(f"el asiento de la SM no tiene débito a la cuenta {cuenta_contrapartida}")

    raras = [l for l in asiento
             if (l.cuenta == cuenta_contrapartida and l.haber > TOL)
             or (l.cuenta != cuenta_contrapartida and l.debe > TOL)]
    if raras or abs(debe_contra - haber_inv) > TOL:
        detalle = "; ".join(f"{l.cuenta} D {l.debe:,.2f} H {l.haber:,.2f}" for l in asiento)
        raise ValueError(f"el asiento de la SM no tiene la forma esperada (débito contrapartida = "
                         f"crédito inventario): {detalle}")
    return haber_inv / cantidad, ""
