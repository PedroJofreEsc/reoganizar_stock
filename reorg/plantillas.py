"""Plantillas Excel de entrada, con filas de ejemplo."""

from __future__ import annotations

import io

import pandas as pd

from reorg.procesos import transformacion, traslado

_T = traslado.COLUMNAS
_X = transformacion.COLUMNAS + transformacion.DIMS
_DIMS_EJ = ["1160104", "B2B-ACU", "OPPMMA01", "SAN-SAN", "CHCEQU01", "OPR"]

PLANTILLAS: dict[str, tuple[str, pd.DataFrame]] = {
    "1_traslado_consolidar_piso": (
        "Paso 1: todo el stock de cada ubicación a PISO. Debe calzar con el saldo en SAP por lote y bin.",
        pd.DataFrame([
            ["110000001", "BDLMP003", "BDLMP003-A01", "BDLMP003-PISO", 40, "SL-01"],
            ["110000001", "BDLMP003", "BDLMP003-A02", "BDLMP003-PISO", 60, "SL-01"],
            ["110000002", "BDLMP003", "BDLMP003-B01", "BDLMP003-PISO", 25, "SL-01"],
        ], columns=_T)),
    "2_transformacion_lotes": (
        "Paso 2: una fila por movimiento de LOTE_ORIGEN a LOTE_DESTINO, en el mismo BIN_ORIGEN. "
        "CUENTA y DIM1..DIM5 iguales en todas las filas del artículo. El lote destino se asume nuevo "
        "(una sola fecha de vencimiento). Con varios lotes de origen, el costo de la EM es el promedio.",
        pd.DataFrame([
            ["110000001", "BDLMP003", "SL-01", "L2401", "2027-12-31", 50, "BDLMP003-PISO", *_DIMS_EJ],
            ["110000001", "BDLMP003", "SL-01", "L2402", "2027-10-31", 45, "BDLMP003-PISO", *_DIMS_EJ],
            ["110000002", "BDLMP003", "SL-01", "L2410", "2028-03-31", 24, "BDLMP003-PISO", *_DIMS_EJ],
        ], columns=_X)),
    "3_traslado_redistribuir": (
        "Paso 3: desde PISO a las ubicaciones reales, con los lotes reales.",
        pd.DataFrame([
            ["110000001", "BDLMP003", "BDLMP003-PISO", "BDLMP003-A01", 50, "L2401"],
            ["110000001", "BDLMP003", "BDLMP003-PISO", "BDLMP003-A02", 45, "L2402"],
            ["110000002", "BDLMP003", "BDLMP003-PISO", "BDLMP003-B01", 24, "L2410"],
        ], columns=_T)),
}


def excel_bytes(nombre: str) -> bytes:
    buf = io.BytesIO()
    PLANTILLAS[nombre][1].to_excel(buf, index=False)
    return buf.getvalue()
