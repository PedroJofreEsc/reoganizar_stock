"""Configuración local. Usuario y clave no se guardan: se ingresan en la app."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

RUTA = Path(__file__).resolve().parents[1] / "config.ini"

TEST, PRODUCTIVA = "Test", "Productiva"

# Empresa -> tipo de base -> CompanyDB
EMPRESAS: dict[str, dict[str, str]] = {
    "Adclean": {PRODUCTIVA: "CLPRD_ADCLEAN", TEST: "CLTST1ADCLEAN"},
    "Adquim": {PRODUCTIVA: "CLPRDADQUIM", TEST: "CLTSTADQUIM"},
    "Adgreen": {PRODUCTIVA: "CLPRDADGREEN", TEST: "CLTSTADGREEN"},
}


@dataclass
class Config:
    url: str = "https://cl-cloud-sl1.sapparapymes.cloud:50000/b1s/v1/"
    verify_ssl: bool = True
    max_articulos: int = 20  # artículos (líneas) por documento de traslado
    dias_minimos_vencimiento: int = 90  # SAP rechaza lotes que vencen antes (se avisa, no bloquea; 0 = no avisar)


def leer_config() -> Config:
    c = Config()
    if not RUTA.exists():
        return c
    cfg = configparser.ConfigParser()
    cfg.read(RUTA, encoding="utf-8")
    if cfg.has_section("service_layer"):
        s = cfg["service_layer"]
        c.url = s.get("url", c.url)
        c.verify_ssl = s.getboolean("verify_ssl", c.verify_ssl)
    if cfg.has_section("proceso"):
        c.max_articulos = cfg["proceso"].getint("max_articulos_traslado", c.max_articulos)
        c.dias_minimos_vencimiento = cfg["proceso"].getint("dias_minimos_vencimiento", c.dias_minimos_vencimiento)
    return c
