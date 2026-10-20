"""Cliente HTTP del Service Layer.

El usuario y la clave solo se usan en login(); desde ahí todo se hace con la
sesión (cookie B1SESSION + node ROUTEID), que es lo único que se guarda.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests

from reorg.dominio import ErrorSAP, SesionExpirada, SinRespuesta


@dataclass(frozen=True)
class Sesion:
    base_url: str
    company_db: str
    usuario: str
    cookie: str   # B1SESSION
    node: str     # ROUTEID
    verify: bool = True
    empresa: str = ""
    productiva: bool = False


def _base(url: str) -> str:
    return url.rstrip("/") + "/"


def _mensaje(r: requests.Response) -> str:
    try:
        e = r.json()["error"]
        m = e.get("message")
        return f"[{r.status_code}] {e.get('code')}: {m.get('value') if isinstance(m, dict) else m}"
    except Exception:
        return f"[{r.status_code}] {r.text[:500]}"


def login(base_url: str, company_db: str, usuario: str, clave: str, verify: bool = True,
          empresa: str = "", productiva: bool = False) -> Sesion:
    try:
        r = requests.post(_base(base_url) + "Login", verify=verify, timeout=60,
                          json={"CompanyDB": company_db, "UserName": usuario, "Password": clave})
    except requests.RequestException as e:
        raise ErrorSAP(f"No se pudo conectar al Service Layer: {e}") from e
    if r.status_code != 200:
        raise ErrorSAP(f"Login fallido: {_mensaje(r)}")
    return Sesion(base_url, company_db, usuario,
                  r.cookies.get("B1SESSION"), r.cookies.get("ROUTEID", ""), verify, empresa, productiva)


def logout(sesion: Sesion):
    try:
        SLClient(sesion, timeout=30).post("Logout")
    except Exception:
        pass


class SLClient:
    def __init__(self, sesion: Sesion, timeout: int = 120):
        self.base = _base(sesion.base_url)
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = sesion.verify
        self.s.headers.update({
            "Content-Type": "application/json",
            "Cookie": f"B1SESSION={sesion.cookie}; ROUTEID={sesion.node}",
        })

    def _revisar(self, r: requests.Response, verbo: str, path: str):
        if r.status_code == 401:
            raise SesionExpirada("La sesión de SAP expiró o no es válida. Vuelve a conectar.")
        if r.status_code not in (200, 201, 204):
            raise ErrorSAP(f"{verbo} {path}: {_mensaje(r)}")

    def _request(self, verbo: str, path: str, **kw) -> requests.Response:
        try:
            r = self.s.request(verbo, self.base + path, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            if verbo == "POST":  # no se sabe si SAP alcanzó a crear el documento
                raise SinRespuesta(f"POST {path}: sin respuesta del Service Layer ({e}). "
                                   "Revisa en SAP si el documento se creó antes de reenviar.") from e
            raise ErrorSAP(f"{verbo} {path}: sin respuesta del Service Layer ({e})") from e
        self._revisar(r, verbo, path)
        return r

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params).json()

    def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        out, data = [], self.get(path, params)
        while True:
            out += data.get("value", [])
            nxt = data.get("odata.nextLink") or data.get("@odata.nextLink")
            if not nxt:
                return out
            data = self.get(nxt.split("/b1s/v1/")[-1] if "/b1s/v1/" in nxt else nxt)

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self._request("POST", path, data=json.dumps(body) if body is not None else None)
        return r.json() if r.content else {}
