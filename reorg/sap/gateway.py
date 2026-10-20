"""Operaciones de negocio sobre el Service Layer (implementa dominio.Gateway).

Cada documento se guarda primero como JSON en `carpeta_payloads` (la carpeta payloads/ de la
carga) con guardar_*, y luego se envía ese archivo exacto con enviar(). Un reenvío usa el mismo archivo.
Para solo validar (lecturas) no hace falta carpeta.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from reorg.dominio import DocCreado, DocTraslado, LineaAsiento, Transformacion
from reorg.sap import payloads
from reorg.sap.cliente import Sesion, SLClient

ENDPOINTS = {"traslado": "StockTransfers", "sm": "InventoryGenExits", "em": "InventoryGenEntries"}


def _odata(v: str) -> str:
    return v.replace("'", "''")


class SapGateway:
    def __init__(self, sesion: Sesion, carpeta_payloads: Path | None = None):
        self.sl = SLClient(sesion)
        self.carpeta = carpeta_payloads
        self._bins: dict[str, dict[str, int]] = {}

    # ---- lectura ---------------------------------------------------------- #
    def resolver_bin(self, bodega: str, bin_code: str) -> int:
        """BinCode -> AbsEntry (cache por bodega). ValueError si no existe."""
        if bodega not in self._bins:
            filas = self.sl.get_all("BinLocations", {
                "$select": "AbsEntry,BinCode,Warehouse",
                "$filter": f"Warehouse eq '{_odata(bodega)}'",
            })
            self._bins[bodega] = {f["BinCode"].strip().upper(): int(f["AbsEntry"]) for f in filas}
        abs_entry = self._bins[bodega].get(bin_code.strip().upper())
        if abs_entry is None:
            raise ValueError(f"la ubicación '{bin_code}' no existe en la bodega {bodega}")
        return abs_entry

    def lotes_existentes(self, articulo: str) -> dict[str, str]:
        """Lotes que ya existen en SAP para el artículo: {lote: vencimiento AAAA-MM-DD o ''}."""
        filas = self.sl.get_all("BatchNumberDetails", {
            "$select": "Batch,ExpirationDate",
            "$filter": f"ItemCode eq '{_odata(articulo)}'",
        })
        return {f["Batch"]: (f.get("ExpirationDate") or "")[:10] for f in filas}

    def leer_transnum_sm(self, doc_entry: int) -> int | None:
        r = self.sl.get(f"InventoryGenExits({int(doc_entry)})", {"$select": "DocNum,TransNum"})
        return r.get("TransNum") or None

    def leer_asiento(self, trans_num: int) -> list[LineaAsiento]:
        je = self.sl.get(f"JournalEntries({int(trans_num)})", {"$select": "JdtNum,JournalEntryLines"})
        return [LineaAsiento(l.get("AccountCode", ""), float(l.get("Debit") or 0), float(l.get("Credit") or 0))
                for l in je.get("JournalEntryLines", [])]

    # ---- guardar JSON ----------------------------------------------------- #
    def guardar_traslado(self, doc: DocTraslado) -> str:
        return self._guardar(f"traslado_{doc.id}", payloads.traslado(doc))

    def guardar_sm(self, t: Transformacion) -> str:
        return self._guardar(f"SM_{t.bodega}_{t.articulo}", payloads.salida_mercancia(t))

    def guardar_em(self, t: Transformacion, sm_doc_entry: int, sm_doc_num, costo_unitario: float) -> str:
        return self._guardar(f"EM_{t.bodega}_{t.articulo}",
                             payloads.entrada_mercancia(t, sm_doc_entry, sm_doc_num, costo_unitario))

    def _guardar(self, nombre: str, payload: dict) -> str:
        if self.carpeta is None:
            raise ValueError("el gateway no tiene carpeta de payloads (solo sirve para validar)")
        self.carpeta.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now():%Y%m%d_%H%M%S}_{nombre}".replace("/", "-").replace("\\", "-")
        p, n = self.carpeta / f"{base}.json", 1
        while p.exists():
            n += 1
            p = self.carpeta / f"{base}_{n}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p.name

    # ---- envío ------------------------------------------------------------ #
    def leer_json(self, archivo: str) -> dict:
        p = self.carpeta / archivo
        if not p.exists():
            raise ValueError(f"no se encontró el JSON {archivo} en {self.carpeta}")
        return json.loads(p.read_text(encoding="utf-8"))

    def enviar(self, tipo: str, archivo: str) -> DocCreado:
        r = self.sl.post(ENDPOINTS[tipo], self.leer_json(archivo))
        return DocCreado(r["DocEntry"], r["DocNum"], r.get("TransNum") or None)
