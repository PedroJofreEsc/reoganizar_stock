"""Modelos y errores compartidos entre procesos/ (lógica local) y sap/ (Service Layer).

No dependen de pandas ni de requests: procesos/ arma estos objetos y sap/ los
traduce a JSON del Service Layer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Protocol


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #
class ErrorValidacion(Exception):
    """Errores del Excel o de datos maestros. Se juntan todos para mostrarlos de una vez."""

    def __init__(self, errores: list[str]):
        self.errores = errores
        super().__init__(f"{len(errores)} error(es) de validación")


class ErrorSAP(Exception):
    """El Service Layer respondió con error: el documento NO se creó."""


class SesionExpirada(ErrorSAP):
    """La sesión (cookie B1SESSION) ya no es válida: hay que volver a conectar."""


class SinRespuesta(ErrorSAP):
    """Un envío quedó sin respuesta (timeout, conexión cortada): no se sabe si SAP creó el documento."""


# --------------------------------------------------------------------------- #
# Estados de cada envío
# --------------------------------------------------------------------------- #
class Estado(str, Enum):
    OK = "OK"                      # documento creado
    ERROR = "ERROR"                # SAP lo rechazó: no se creó, se puede reenviar
    REVISAR = "REVISAR"            # sin respuesta: revisar en SAP antes de reenviar
    NO_EJECUTADO = "NO_EJECUTADO"  # no se envió (la sesión expiró antes)
    ENVIANDO = "ENVIANDO"          # solo en el registro mientras se espera la respuesta


REENVIABLES = (Estado.ERROR.value, Estado.NO_EJECUTADO.value)


# --------------------------------------------------------------------------- #
# Piezas de documentos
# --------------------------------------------------------------------------- #
@dataclass
class Lote:
    lote: str
    cantidad: float
    vencimiento: str | None = None  # AAAA-MM-DD


@dataclass
class AsignacionBin:
    bin_code: str
    cantidad: float
    lote: str | None = None      # None si el artículo no maneja lotes
    bin_abs: int | None = None   # AbsEntry, se completa al preparar


@dataclass
class LineaTraslado:
    articulo: str
    cantidad: float
    lotes: list[Lote]               # ordenados; vacío si no maneja lotes
    salidas: list[AsignacionBin]
    entradas: list[AsignacionBin]


@dataclass
class DocTraslado:
    id: str
    bodega: str
    comentario: str
    lineas: list[LineaTraslado]

    @property
    def articulos(self) -> list[str]:
        return [l.articulo for l in self.lineas]

    @property
    def cantidad(self) -> float:
        return sum(l.cantidad for l in self.lineas)


@dataclass
class Transformacion:
    """Un artículo: SM de una línea (salen los lotes de origen) + EM de una línea (entran los lotes destino),
    en los mismos bins."""

    articulo: str
    bodega: str
    cantidad: float
    cuenta: str                      # contrapartida, igual en SM y EM
    dims: list[str]                  # DIM1..DIM5, "" = sin dimensión
    lotes_origen: list[Lote]         # salen en la SM, ordenados
    lotes_destino: list[Lote]        # entran en la EM, ordenados, con vencimiento
    salidas_sm: list[AsignacionBin]  # por lote de origen y bin
    entradas_em: list[AsignacionBin]  # por lote destino y bin

    @property
    def clave(self) -> str:
        return f"{self.bodega}|{self.articulo}"

    def to_dict(self) -> dict:
        """Forma JSON (sirve para guardar en el registro y para comparar)."""
        return json.loads(json.dumps(asdict(self)))

    @classmethod
    def from_dict(cls, d: dict) -> "Transformacion":
        lotes = lambda xs: [Lote(**x) for x in xs]
        bins = lambda xs: [AsignacionBin(**x) for x in xs]
        return cls(d["articulo"], d["bodega"], d["cantidad"], d["cuenta"], list(d["dims"]),
                   lotes(d["lotes_origen"]), lotes(d["lotes_destino"]),
                   bins(d["salidas_sm"]), bins(d["entradas_em"]))


@dataclass
class LineaAsiento:
    cuenta: str
    debe: float
    haber: float


@dataclass
class DocCreado:
    doc_entry: int
    doc_num: int
    trans_num: int | None = None


# --------------------------------------------------------------------------- #
# Lo que procesos/ necesita de SAP (lo implementa sap.gateway.SapGateway)
# --------------------------------------------------------------------------- #
class Gateway(Protocol):
    # lecturas
    def resolver_bin(self, bodega: str, bin_code: str) -> int: ...
    def lotes_existentes(self, articulo: str) -> dict[str, str]: ...
    def leer_transnum_sm(self, doc_entry: int) -> int | None: ...
    def leer_asiento(self, trans_num: int) -> list[LineaAsiento]: ...
    # cada documento se guarda primero como JSON y luego se envía ese archivo exacto
    def guardar_traslado(self, doc: DocTraslado) -> str: ...
    def guardar_sm(self, t: Transformacion) -> str: ...
    def guardar_em(self, t: Transformacion, sm_doc_entry: int, sm_doc_num, costo_unitario: float) -> str: ...
    def enviar(self, tipo: str, archivo: str) -> DocCreado: ...


def ordenar_lotes(cantidades: dict[str, float]) -> list[Lote]:
    """Lotes por cantidad descendente (estable). Regla usada en traslados, SM y EM."""
    return [Lote(l, q) for l, q in sorted(cantidades.items(), key=lambda kv: -kv[1])]
