"""Traducción de objetos del dominio a JSON del Service Layer.

Funciones puras: no llaman a SAP. Se usan para crear los documentos y para la
vista previa de la app.
"""

from __future__ import annotations

from reorg.dominio import AsignacionBin, DocTraslado, Transformacion

TOL = 1e-6

# BinActionType (StockTransfers)
BIN_SALE = 2   # batFromWarehouse
BIN_ENTRA = 1  # batToWarehouse

COSTING = ["CostingCode", "CostingCode2", "CostingCode3", "CostingCode4", "CostingCode5"]


def num(x: float):
    """int si la cantidad es entera (JSON más limpio)."""
    x = round(float(x), 6)
    return int(x) if abs(x - round(x)) < TOL else x


def _alloc(a: AsignacionBin, linea: int, idx_lote: dict[str, int], tipo: int | None = None) -> dict:
    d = {"BinAbsEntry": a.bin_abs, "Quantity": num(a.cantidad)}
    if tipo is not None:
        d["BinActionType"] = tipo
    d["BaseLineNumber"] = linea
    if a.lote is not None:
        d["SerialAndBatchNumbersBaseLine"] = idx_lote[a.lote]
    return d


# --------------------------------------------------------------------------- #
# Traslado de stock (misma bodega)
# --------------------------------------------------------------------------- #
def traslado(doc: DocTraslado) -> dict:
    lineas = []
    for i, l in enumerate(doc.lineas):
        idx = {lt.lote: j for j, lt in enumerate(l.lotes)}
        linea = {"ItemCode": l.articulo, "Quantity": num(l.cantidad)}
        if l.lotes:
            linea["BatchNumbers"] = [{"BatchNumber": lt.lote, "Quantity": num(lt.cantidad), "BaseLineNumber": i}
                                     for lt in l.lotes]
        linea["StockTransferLinesBinAllocations"] = (
            [_alloc(a, i, idx, BIN_SALE) for a in l.salidas]
            + [_alloc(a, i, idx, BIN_ENTRA) for a in l.entradas])
        lineas.append(linea)
    return {"FromWarehouse": doc.bodega, "ToWarehouse": doc.bodega,
            "Comments": doc.comentario[:254], "StockTransferLines": lineas}


# --------------------------------------------------------------------------- #
# Transformación: SM (InventoryGenExits) + EM (InventoryGenEntries), una línea cada una
# --------------------------------------------------------------------------- #
def _cuenta_dims(t: Transformacion) -> dict:
    d = {"AccountCode": t.cuenta}
    d.update({campo: v for campo, v in zip(COSTING, t.dims) if v})
    return d


def salida_mercancia(t: Transformacion) -> dict:
    idx = {lt.lote: j for j, lt in enumerate(t.lotes_origen)}
    linea = {"ItemCode": t.articulo, "Quantity": num(t.cantidad), "WarehouseCode": t.bodega,
             **_cuenta_dims(t),
             "BatchNumbers": [{"BatchNumber": lt.lote, "Quantity": num(lt.cantidad), "BaseLineNumber": 0}
                              for lt in t.lotes_origen],
             "DocumentLinesBinAllocations": [_alloc(a, 0, idx) for a in t.salidas_sm]}
    origen = ", ".join(lt.lote for lt in t.lotes_origen)
    return {"Comments": f"SM transformacion lote {origen} art {t.articulo}"[:254],
            "DocumentLines": [linea]}


def entrada_mercancia(t: Transformacion, sm_doc_entry: int, sm_doc_num, costo_unitario: float) -> dict:
    idx = {lt.lote: j for j, lt in enumerate(t.lotes_destino)}
    linea = {"BaseType": 60, "BaseEntry": int(sm_doc_entry), "BaseLine": 0,
             "WarehouseCode": t.bodega, "Quantity": num(t.cantidad),
             "UnitPrice": round(costo_unitario, 6), **_cuenta_dims(t),
             "BatchNumbers": [{"BatchNumber": lt.lote, "Quantity": num(lt.cantidad),
                               "BaseLineNumber": 0, "ExpiryDate": lt.vencimiento} for lt in t.lotes_destino],
             "DocumentLinesBinAllocations": [_alloc(a, 0, idx) for a in t.entradas_em]}
    return {"Comments": f"EM desde SM {sm_doc_num}, lotes reales art {t.articulo}"[:254],
            "DocumentLines": [linea]}
