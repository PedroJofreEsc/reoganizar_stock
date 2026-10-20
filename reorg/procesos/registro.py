"""Persistencia local por carga.

Cada ejecución (traslado o transformación) es una carga con ID propio, independiente del
nombre del Excel:

    salidas/<CompanyDB>/cargas/<ID>/
        carga.json        estado de cada documento (se reescribe después de cada envío)
        excel_<nombre>    copia del Excel subido
        payloads/         JSON enviados a SAP (un reenvío usa el mismo archivo)
        log_<etapa>_<fecha>.csv

Los CSV usan ';' y UTF-8 con BOM para abrirse directo en Excel.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from reorg.dominio import Estado, Transformacion

RAIZ_SALIDAS = Path(__file__).resolve().parents[2] / "salidas"
SEP = ";"
ENC = "utf-8-sig"

TRASLADO, TRANSFORMACION = "traslado", "transformacion"
PREFIJO = {TRASLADO: "TR", TRANSFORMACION: "TF"}


def carpeta_cargas(company_db: str) -> Path:
    d = RAIZ_SALIDAS / company_db / "cargas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ahora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def huella(d) -> str:
    """Huella del contenido de un documento (para detectar duplicados entre cargas)."""
    return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _escribir_seguro(ruta: Path, texto: str):
    """Escribe vía archivo temporal + reemplazo. Reintenta si el archivo está bloqueado
    por un momento (p. ej. OneDrive sincronizando); como último recurso escribe directo."""
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    for intento in range(15):
        try:
            os.replace(tmp, ruta)
            return
        except PermissionError:
            time.sleep(0.1 * (intento + 1))
    ruta.write_text(texto, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def escribir_csv(ruta: Path, filas: list[dict]):
    campos = list(dict.fromkeys(k for f in filas for k in f)) or ["SIN_FILAS"]
    with open(ruta, "w", newline="", encoding=ENC) as fh:
        w = csv.DictWriter(fh, fieldnames=campos, delimiter=SEP, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)


# --------------------------------------------------------------------------- #
# Carga (base común)
# --------------------------------------------------------------------------- #
class Carga:
    proceso = ""

    def __init__(self, directorio: Path, datos: dict):
        self.dir = directorio
        self.datos = datos

    # ---- creación / apertura --------------------------------------------- #
    @classmethod
    def nueva(cls, company_db: str, usuario: str, excel_nombre: str, excel_bytes: bytes, **extra) -> "Carga":
        base = f"{PREFIJO[cls.proceso]}-{datetime.now():%Y%m%d-%H%M%S}"
        raiz, cid, n = carpeta_cargas(company_db), base, 1
        while (raiz / cid).exists():
            n += 1
            cid = f"{base}-{n}"
        d = raiz / cid
        (d / "payloads").mkdir(parents=True)
        (d / f"excel_{Path(excel_nombre).name}").write_bytes(excel_bytes)
        c = cls(d, {"id": cid, "proceso": cls.proceso, "company_db": company_db, "fecha": ahora(),
                    "usuario": usuario, "excel": excel_nombre, **extra, **cls._vacio()})
        c.guardar()
        return c

    @classmethod
    def abrir(cls, company_db: str, cid: str) -> "Carga":
        d = carpeta_cargas(company_db) / cid
        c = cls(d, json.loads((d / "carga.json").read_text(encoding="utf-8")))
        c._marcar_interrumpidos()
        return c

    @classmethod
    def listar(cls, company_db: str) -> list["Carga"]:
        """Cargas de este proceso en la base, de la más reciente a la más antigua."""
        out = []
        for d in sorted(carpeta_cargas(company_db).glob(f"{PREFIJO[cls.proceso]}-*"), reverse=True):
            if (d / "carga.json").exists():
                out.append(cls.abrir(company_db, d.name))
        return out

    @staticmethod
    def _vacio() -> dict:
        return {}

    # ---- propiedades ------------------------------------------------------ #
    @property
    def id(self) -> str:
        return self.datos["id"]

    @property
    def payloads(self) -> Path:
        return self.dir / "payloads"

    def descripcion(self) -> str:
        return f"{self.id} · {self.datos['excel']} · {self.datos['usuario']}"

    # ---- persistencia ----------------------------------------------------- #
    def guardar(self):
        _escribir_seguro(self.dir / "carga.json", json.dumps(self.datos, ensure_ascii=False, indent=2))

    def guardar_log(self, etapa: str, filas: list[dict]) -> Path:
        ruta = self.dir / f"log_{etapa}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        escribir_csv(ruta, filas)
        return ruta

    def _documentos(self) -> list[dict]:
        """Diccionarios de estado de cada documento (para marcar envíos interrumpidos)."""
        return []

    def _marcar_interrumpidos(self):
        # un envío que quedó a medias (la app se cerró esperando respuesta) no se sabe si se creó
        for doc in self._documentos():
            if doc["estado"] == Estado.ENVIANDO.value:
                doc.update(estado=Estado.REVISAR.value,
                           error="La app se cerró durante el envío: revisar en SAP si se creó")


# --------------------------------------------------------------------------- #
# Carga de traslado
# --------------------------------------------------------------------------- #
class CargaTraslado(Carga):
    proceso = TRASLADO

    @staticmethod
    def _vacio() -> dict:
        return {"docs": []}

    @property
    def docs(self) -> list[dict]:
        return self.datos["docs"]

    def _documentos(self) -> list[dict]:
        return self.docs

    def pendientes(self) -> list[dict]:
        return [d for d in self.docs if d["estado"] != Estado.OK.value]

    @staticmethod
    def fila_log(doc: dict, carga_id: str) -> dict:
        return {"CARGA": carga_id, "DOC": doc["doc"], "BODEGA": doc["bodega"], "N_ARTICULOS": doc["n_articulos"],
                "ARTICULOS": doc["articulos"], "CANTIDAD": doc["cantidad"], "PAYLOAD": doc["payload"],
                "DOCENTRY": doc["doc_entry"] or "", "DOCNUM": doc["doc_num"] or "", "ESTADO": doc["estado"],
                "INTENTOS": doc["intentos"], "ERROR": doc["error"]}


def traslados_creados(company_db: str) -> dict[str, list[tuple[str, dict]]]:
    """{huella: [(carga, doc)]} de los traslados creados (OK) en cualquier carga de la base."""
    out: dict[str, list] = {}
    for c in CargaTraslado.listar(company_db):
        for d in c.docs:
            if d["estado"] == Estado.OK.value:
                out.setdefault(d["huella"], []).append((c.id, d))
    return out


# --------------------------------------------------------------------------- #
# Carga de transformación
# --------------------------------------------------------------------------- #
def _sm_vacia() -> dict:
    return {"estado": "", "payload": "", "doc_entry": None, "doc_num": None, "trans_num": None,
            "error": "", "fecha": "", "intentos": 0}


def _em_vacia() -> dict:
    return {"estado": "", "payload": "", "doc_entry": None, "doc_num": None, "costo_unit": None,
            "asiento_total": None, "nota": "", "error": "", "fecha": "", "intentos": 0,
            "payloads_anteriores": [], "correcciones": []}


class CargaTransformacion(Carga):
    """articulos: {"BODEGA|ARTICULO": {"datos": Transformacion, "huella", "sm": {...}, "em": {...}}}"""

    proceso = TRANSFORMACION

    @staticmethod
    def _vacio() -> dict:
        return {"articulos": {}}

    @property
    def articulos(self) -> dict[str, dict]:
        return self.datos["articulos"]

    def _documentos(self) -> list[dict]:
        return [doc for e in self.articulos.values() for doc in (e["sm"], e["em"])]

    def get(self, clave: str) -> dict | None:
        return self.articulos.get(clave)

    def registrar(self, t: Transformacion) -> dict:
        datos = t.to_dict()
        return self.articulos.setdefault(t.clave, {"datos": datos, "huella": huella(datos),
                                                   "sm": _sm_vacia(), "em": _em_vacia()})

    @staticmethod
    def transformacion(e: dict) -> Transformacion:
        return Transformacion.from_dict(e["datos"])

    # ---- consultas -------------------------------------------------------- #
    def sm_pendientes(self) -> list[tuple[str, dict]]:
        """Artículos cuya SM no se creó (error, sin enviar o por revisar)."""
        return [(k, e) for k, e in self.articulos.items() if e["sm"]["estado"] != Estado.OK.value]

    def con_sm(self) -> list[tuple[str, dict]]:
        return [(k, e) for k, e in self.articulos.items() if e["sm"]["estado"] == Estado.OK.value]

    def em_pendientes(self) -> list[tuple[str, dict]]:
        """Artículos con SM creada y EM no creada (pendiente, con error o por revisar)."""
        return [(k, e) for k, e in self.con_sm() if e["em"]["estado"] != Estado.OK.value]

    def tiene_pendientes(self) -> bool:
        return bool(self.sm_pendientes() or self.em_pendientes())

    def fila_log(self, clave: str, etapa: str) -> dict:
        e = self.articulos[clave]
        d, sm, em = e["datos"], e["sm"], e["em"]
        doc = sm if etapa == "SM" else em
        return {"CARGA": self.id, "CLAVE": clave, "ARTICULO": d["articulo"], "BODEGA": d["bodega"],
                "CANTIDAD": d["cantidad"],
                "LOTES_ORIGEN": ", ".join(l["lote"] for l in d["lotes_origen"]),
                "LOTES_DESTINO": ", ".join(l["lote"] for l in d["lotes_destino"]),
                "ETAPA": etapa, "ESTADO": doc["estado"],
                "SM_DOCENTRY": sm["doc_entry"] or "", "SM_DOCNUM": sm["doc_num"] or "",
                "SM_TRANSNUM": sm["trans_num"] or "", "PAYLOAD_SM": sm["payload"],
                "COSTO_UNIT": "" if em["costo_unit"] is None else em["costo_unit"],
                "ASIENTO_TOTAL": "" if em["asiento_total"] is None else em["asiento_total"],
                "EM_DOCENTRY": em["doc_entry"] or "", "EM_DOCNUM": em["doc_num"] or "",
                "PAYLOAD_EM": em["payload"],
                "PAYLOADS_EM_ANTERIORES": ", ".join(em.get("payloads_anteriores", [])),
                "CORRECCIONES": " | ".join(em.get("correcciones", [])),
                "NOTA": em["nota"], "INTENTOS": doc["intentos"], "ERROR": doc["error"]}


def transformaciones_registradas(company_db: str) -> list[tuple[str, str, dict]]:
    """[(carga, clave, entrada)] de todas las transformaciones con SM creada en la base."""
    return [(c.id, k, e) for c in CargaTransformacion.listar(company_db) for k, e in c.con_sm()]
