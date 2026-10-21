"""App local: reorganización de stock SAP B1 (ubicaciones y lotes).

Ejecutar:  streamlit run app.py   (o doble clic en iniciar_app.bat)

La app solo orquesta: la lógica está en reorg/procesos y el acceso a SAP en reorg/sap.
Cada ejecución es una carga con ID propio (no depende del nombre del Excel).
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date

import pandas as pd
import streamlit as st

from reorg import plantillas
from reorg.config import EMPRESAS, PRODUCTIVA, TEST, leer_config
from reorg.dominio import ErrorSAP, ErrorValidacion, Estado, SesionExpirada
from reorg.procesos import registro, transformacion, traslado
from reorg.procesos.registro import CargaTransformacion, CargaTraslado
from reorg.sap import cliente, payloads
from reorg.sap.gateway import SapGateway

st.set_page_config(page_title="Reorganización de stock SAP", page_icon="📦", layout="wide")
CFG = leer_config()
ss = st.session_state
ss.setdefault("n_ejecuciones", 0)  # cambia la key de casillas y tablas tras cada ejecución
ESTADO_APP = ["sesion", "plan_traslado", "res_traslado", "res_nueva_tr", "plan_transf", "res_transf", "res_nueva_tf",
              "sel_tr", "sel_tf"]


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
_GATEWAYS: list[SapGateway] = []  # gateways usados en esta ejecución del script (se reinicia en cada rerun)

AVISO_EXPIRADA = ("La sesión de SAP expiró o se desconectó. Vuelve a conectar en la barra lateral: lo que no "
                  "alcanzó a enviarse quedó pendiente en la carga y se puede reenviar.")


def gateway(carpeta=None) -> SapGateway:
    """Sin carpeta solo sirve para validar (lecturas); para enviar, la carpeta payloads/ de la carga."""
    gw = SapGateway(ss.sesion, carpeta)
    _GATEWAYS.append(gw)
    return gw


def expirar_sesion(mensaje: str = AVISO_EXPIRADA):
    """Quita la sesión para que la barra lateral vuelva a pedir la conexión. Conserva la carga
    seleccionada y el último resultado, para seguir en el mismo punto al reconectar."""
    ss.pop("sesion", None)
    ss.aviso_sesion = mensaje


def revisar_sesion():
    if ss.get("sesion") and any(gw.sesion_expirada for gw in _GATEWAYS):
        expirar_sesion()


def recargar():
    """st.rerun() revisando antes si alguna consulta detectó la sesión expirada."""
    revisar_sesion()
    st.rerun()


def huella_archivo(archivo, *extra) -> str:
    """Identifica archivo + parámetros: si cambian, el plan validado deja de servir."""
    return "|".join([archivo.name, hashlib.md5(archivo.getvalue()).hexdigest(), *map(str, extra)])


def etiqueta_base(s) -> str:
    return f":red[**PRODUCTIVA {s.company_db}**]" if s.productiva else f"**{s.company_db}** (test)"


def olvidar_resultado_nuevo(carga_id: str):
    """Una acción posterior sobre la carga (reenviar, crear EM, cerrar) deja obsoleto el resultado de su
    primera ejecución: se quita para no mostrar estados viejos."""
    for k in ("res_nueva_tr", "res_nueva_tf"):
        if ss.get(k, (None, None))[1] == carga_id:
            ss.pop(k)


def terminar(clave_res: str, valor: tuple, clave_sel: str, carga_id: str, plan: str | None = None):
    if not clave_res.startswith("res_nueva"):
        olvidar_resultado_nuevo(carga_id)
    ss[clave_res] = valor
    ss[clave_sel] = carga_id
    if plan:
        ss.pop(plan, None)
    ss.n_ejecuciones += 1
    recargar()


def manejar_error(e: Exception):
    if isinstance(e, ErrorValidacion):
        st.error(f"El archivo tiene {len(e.errores)} error(es). Corrígelo y vuelve a validar.")
        st.dataframe(pd.DataFrame({"Error": e.errores}), hide_index=True, width="stretch")
    elif isinstance(e, SesionExpirada):
        expirar_sesion()
        st.rerun()
    elif isinstance(e, ErrorSAP):
        st.error(str(e))
    else:
        st.exception(e)


def ejecutar_con_progreso(fn, etiqueta):
    barra = st.progress(0.0, text="Iniciando...")

    def progreso(i, n, fila):
        barra.progress(i / n, text=f"{i}/{n} · {fila[etiqueta]}: {fila['ESTADO']}")

    return fn(progreso)


def mostrar_resultado(filas: list[dict], ruta):
    df = pd.DataFrame(filas)
    if df.empty:
        st.info("No había nada que enviar.")
        return
    conteo = df["ESTADO"].value_counts()
    for col, (estado, n) in zip(st.columns(len(conteo)), conteo.items()):
        col.metric(estado, n)
    st.dataframe(df.astype(str), hide_index=True, width="stretch")
    st.download_button("Descargar log (CSV)", ruta.read_bytes(), file_name=ruta.name, mime="text/csv",
                       key=f"dl_{ruta.parent.name}_{ruta.name}")


def avisos_estados(estados: pd.Series):
    if (estados == Estado.ERROR.value).any():
        st.warning("Hay documentos rechazados por SAP (no se crearon). Corrige la causa y reenvíalos: "
                   "se envía el mismo JSON.")
    if (estados == Estado.REVISAR.value).any():
        st.warning("Hay envíos sin respuesta: revisa en SAP si el documento se creó antes de reenviarlo.")
    if (estados == Estado.NO_EJECUTADO.value).any():
        st.warning("Hay documentos sin enviar (la sesión expiró). Vuelve a conectar y reenvíalos.")


def resultado_nueva_carga(clave: str, fid: str):
    """Resultado de la última ejecución de una carga nueva, debajo del Excel que se subió."""
    res = ss.get(clave)
    if not res or res[0] != fid:
        return
    _, carga_id, filas, ruta = res
    st.markdown(f"#### Resultado · carga {carga_id}")
    mostrar_resultado(filas, ruta)
    estados = pd.Series([f["ESTADO"] for f in filas])
    if (estados != Estado.OK.value).any():
        avisos_estados(estados)
        st.caption("Los documentos que no se crearon se reenvían, corrigen o cierran en la sección **Cargas**.")


def confirmar(texto: str, clave: str) -> bool:
    return st.checkbox(texto, key=f"{clave}_{ss.n_ejecuciones}")


def incluir_revisar(n: int, clave: str) -> bool:
    if not n:
        return False
    return st.checkbox(f"Incluir {n} envío(s) sin respuesta (REVISAR): confirmo que revisé en SAP y **no** "
                       "se crearon", key=f"{clave}_{ss.n_ejecuciones}")


def elegir_carga(cls, s, clave: str, tiene_pendientes):
    """Selector de cargas de la base (por defecto solo las que tienen pendientes)."""
    todas = st.checkbox("Mostrar también cargas terminadas", key=f"todas_{clave}")
    sel = ss.get(clave)  # la carga con la que se está trabajando sigue visible aunque haya quedado terminada
    cargas = [c for c in cls.listar(s.company_db) if todas or tiene_pendientes(c) or c.id == sel]
    if not cargas:
        st.caption("No hay cargas con pendientes en esta base.")
        return None
    ids = [c.id for c in cargas]
    idx = ids.index(ss[clave]) if ss.get(clave) in ids else 0
    i = st.selectbox("Carga", range(len(cargas)), index=idx,
                     format_func=lambda k: f"{cargas[k].descripcion()} · {cargas[k].datos['fecha']}"
                                           + ("" if tiene_pendientes(cargas[k]) else " · terminada"))
    ss[clave] = ids[i]
    st.caption(f"Carpeta: {cargas[i].dir}")
    return cargas[i]


# --------------------------------------------------------------------------- #
# Barra lateral: conexión
# --------------------------------------------------------------------------- #
def barra_conexion():
    with st.sidebar:
        st.header("Conexión SAP")
        if "aviso_sesion" in ss:
            st.warning(ss.aviso_sesion)

        s = ss.get("sesion")
        if s:
            texto = f"{s.empresa} · {s.company_db}  \nUsuario: {s.usuario}"
            if s.productiva:
                st.error(f"**BASE PRODUCTIVA**  \n{texto}")
            else:
                st.success(f"**Base de test**  \n{texto}")
            st.caption(f"Node: {s.node or '-'}")
            if st.button("Desconectar", width="stretch"):
                cliente.logout(s)
                for k in ESTADO_APP:
                    ss.pop(k, None)
                recargar()
            return

        ult_empresa, ult_tipo = ss.get("ultima_conexion", (None, TEST))
        empresas = list(EMPRESAS)
        empresa = st.selectbox("Empresa", empresas,
                               index=empresas.index(ult_empresa) if ult_empresa in empresas else 0)
        tipo = st.radio("Tipo de base", [TEST, PRODUCTIVA], horizontal=True,
                        index=[TEST, PRODUCTIVA].index(ult_tipo))
        db = EMPRESAS[empresa][tipo]
        if tipo == PRODUCTIVA:
            st.error(f"Base **productiva**: {db}")
        else:
            st.info(f"Base de test: {db}")

        with st.form("login", clear_on_submit=True):
            usuario = st.text_input("Usuario")
            clave = st.text_input("Clave", type="password")
            enviar = st.form_submit_button("Conectar", type="primary", width="stretch")
        if enviar:
            if not (usuario and clave):
                st.error("Completa usuario y clave.")
            else:
                try:
                    with st.spinner("Conectando..."):
                        ss.sesion = cliente.login(CFG.url, db, usuario, clave, CFG.verify_ssl,
                                                  empresa, tipo == PRODUCTIVA)
                    ss.ultima_conexion = (empresa, tipo)
                    ss.pop("aviso_sesion", None)
                    recargar()
                except ErrorSAP as e:
                    st.error(str(e))


# --------------------------------------------------------------------------- #
# Pestaña: traslado (pasos 1 y 3)
# --------------------------------------------------------------------------- #
def tab_traslado():
    st.subheader("Traslado entre ubicaciones (pasos 1 y 3)")
    st.caption("Paso 1: consolidar todo el stock en PISO. Paso 3: redistribuir desde PISO a las ubicaciones "
               f"reales. Un documento por bodega cada {CFG.max_articulos} artículos.")
    s = ss.get("sesion")
    if not s:
        st.info("Conéctate a SAP en la barra lateral para validar y ejecutar.")
        return
    with st.container(border=True):
        nueva_carga_traslado(s)
    with st.container(border=True):
        cargas_traslado(s)


def nueva_carga_traslado(s):
    st.markdown("### Nueva carga")
    archivo = st.file_uploader("Excel de traslado", type=["xlsx"], key="up_traslado")
    comentario = st.text_input("Comentario por defecto (opcional)", max_chars=254, key="com_traslado",
                               help="Se usa en las filas del Excel sin COMENTARIO. Si ambos están vacíos, la app "
                                    "arma uno automático: 'Traslado ubicaciones <bodega> doc N'.")
    if not archivo:
        return
    fid = huella_archivo(archivo, comentario, s.company_db)

    if st.button("Validar y simular", key="val_traslado"):
        ss.pop("plan_traslado", None)
        ss.pop("res_nueva_tr", None)
        try:
            with st.spinner("Validando, resolviendo ubicaciones y revisando cargas anteriores..."):
                plan = traslado.preparar(io.BytesIO(archivo.getvalue()), gateway(), CFG.max_articulos, comentario)
                traslado.avisar_duplicados(plan, s.company_db)
            ss.plan_traslado = (fid, plan)
        except Exception as e:
            manejar_error(e)

    resultado_nueva_carga("res_nueva_tr", fid)
    if ss.get("plan_traslado", (None,))[0] != fid:
        return
    plan: traslado.PlanTraslado = ss.plan_traslado[1]
    for a in plan.avisos:
        st.warning(a)
    c1, c2, c3 = st.columns(3)
    c1.metric("Documentos", len(plan.docs))
    c2.metric("Artículos", sum(len(d.lineas) for d in plan.docs))
    c3.metric("Cantidad total", f"{sum(d.cantidad for d in plan.docs):,.2f}")
    st.dataframe(pd.DataFrame([{"Documento": d.id, "Bodega": d.bodega, "Artículos": len(d.lineas),
                                "Cantidad": d.cantidad, "Comentario": d.comentario,
                                "Detalle": ", ".join(d.articulos)} for d in plan.docs]),
                 hide_index=True, width="stretch")
    with st.expander("Ver JSON que se enviará a SAP"):
        i = st.selectbox("Documento", range(len(plan.docs)), format_func=lambda k: plan.docs[k].id)
        st.json(payloads.traslado(plan.docs[i]), expanded=False)
        todos = {d.id: payloads.traslado(d) for d in plan.docs}
        st.download_button("Descargar todos (JSON)", json.dumps(todos, ensure_ascii=False, indent=2),
                           file_name="traslado.json", mime="application/json")

    if confirmar(f"Confirmo crear {len(plan.docs)} traslado(s) en {etiqueta_base(s)} (nueva carga)", "conf_traslado") \
            and st.button("Ejecutar en SAP", type="primary", key="run_traslado"):
        try:
            carga = CargaTraslado.nueva(s.company_db, s.usuario, archivo.name, archivo.getvalue(),
                                        comentario=comentario)
            filas = ejecutar_con_progreso(
                lambda p: traslado.ejecutar(plan, carga, gateway(carga.payloads), p), "DOC")
        except Exception as e:
            manejar_error(e)
            return
        ruta = carga.guardar_log("traslado", filas)
        terminar("res_nueva_tr", (fid, carga.id, filas, ruta), "sel_tr", carga.id, "plan_traslado")


def cargas_traslado(s):
    st.markdown("### Cargas")
    if "msg_cierre_tr" in ss:
        st.success(ss.pop("msg_cierre_tr"))
    carga = elegir_carga(CargaTraslado, s, "sel_tr", lambda c: bool(c.pendientes()))
    if not carga:
        return
    df = pd.DataFrame([carga.fila_log(d, carga.id) for d in carga.docs])
    st.dataframe(df.drop(columns=["CARGA"]).astype(str), hide_index=True, width="stretch")
    avisos_estados(df["ESTADO"])

    pend = carga.pendientes()
    n_revisar = sum(d["estado"] == Estado.REVISAR.value for d in pend)
    con_revisar = incluir_revisar(n_revisar, "rev_traslado")
    n = sum(d["estado"] in traslado.enviables(con_revisar) for d in pend)
    if n and confirmar(f"Confirmo reenviar {n} documento(s) de {carga.id} en {etiqueta_base(s)}", "conf_reenvio") \
            and st.button(f"Reenviar {n} documento(s) (mismo JSON)", type="primary", key="run_reenvio"):
        try:
            filas = ejecutar_con_progreso(
                lambda p: traslado.reenviar(carga, gateway(carga.payloads), con_revisar, p), "DOC")
        except Exception as e:
            manejar_error(e)
            return
        ruta = carga.guardar_log("traslado_reenvio", filas)
        terminar("res_traslado", (carga.id, filas, ruta), "sel_tr", carga.id)
    cerrar_traslados(s, carga)

    res = ss.get("res_traslado", (None,))
    if res[0] == carga.id:
        st.markdown("#### Resultado del último envío")
        mostrar_resultado(*res[1:])


# --------------------------------------------------------------------------- #
# Pestaña: transformación (paso 2), en dos etapas
# --------------------------------------------------------------------------- #
def tab_transformacion():
    st.subheader("Transformación de lotes (paso 2)")
    st.caption("Por artículo: una SM de una línea (salen los lotes de origen) y luego una EM de una línea, "
               "asociada a la SM, que ingresa los lotes destino en el mismo bin al costo real de la SM.")
    s = ss.get("sesion")
    if not s:
        st.info("Conéctate a SAP en la barra lateral para validar y ejecutar.")
        return
    with st.container(border=True):
        nueva_carga_transformacion(s)
    with st.container(border=True):
        cargas_transformacion(s)


def nueva_carga_transformacion(s):
    st.markdown("### Nueva carga · etapa 1: salidas de mercancía (SM)")
    archivo = st.file_uploader("Excel de transformación", type=["xlsx"], key="up_transf")
    if not archivo:
        return
    fid = huella_archivo(archivo, s.company_db)

    if st.button("Validar y simular", key="val_transf"):
        ss.pop("plan_transf", None)
        ss.pop("res_nueva_tf", None)
        try:
            with st.spinner("Validando, resolviendo ubicaciones y revisando lotes y cargas anteriores..."):
                plan = transformacion.preparar(io.BytesIO(archivo.getvalue()), gateway(), s.company_db,
                                               CFG.dias_minimos_vencimiento)
            ss.plan_transf = (fid, plan)
        except Exception as e:
            manejar_error(e)

    resultado_nueva_carga("res_nueva_tf", fid)
    if ss.get("plan_transf", (None,))[0] != fid:
        return
    plan: transformacion.PlanTransformacion = ss.plan_transf[1]
    for a in plan.avisos:
        st.warning(a)
    st.dataframe(pd.DataFrame([{
        "Artículo": t.articulo, "Bodega": t.bodega, "Cantidad": t.cantidad,
        "Lotes origen": ", ".join(f"{l.lote} ({l.cantidad:g})" for l in t.lotes_origen),
        "Lotes destino": ", ".join(f"{l.lote} ({l.cantidad:g}, vence {l.vencimiento})" for l in t.lotes_destino),
        "Ubicación": ", ".join(dict.fromkeys(a.bin_code for a in t.salidas_sm)),
        "Cuenta": t.cuenta, "Comentario": t.comentario or "(automático)"} for t in plan.items]),
        hide_index=True, width="stretch")
    with st.expander("Ver JSON que se enviará a SAP"):
        k = st.selectbox("Artículo", range(len(plan.items)),
                         format_func=lambda j: f"{plan.items[j].articulo} ({plan.items[j].bodega})")
        t = plan.items[k]
        st.caption("En la EM, BaseEntry y UnitPrice se completan en la etapa 2 con la SM creada y el costo "
                   "de su asiento.")
        c1, c2 = st.columns(2)
        c1.markdown("**SM** (InventoryGenExits)")
        c1.json(payloads.salida_mercancia(t))
        c2.markdown("**EM** (InventoryGenEntries)")
        c2.json(payloads.entrada_mercancia(t, 0, "<SM>", 0.0))

    n = len(plan.items)
    if confirmar(f"Confirmo crear {n} SM en {etiqueta_base(s)} (nueva carga)", "conf_sm") \
            and st.button(f"Crear {n} SM", type="primary", key="run_sm"):
        try:
            carga = CargaTransformacion.nueva(s.company_db, s.usuario, archivo.name, archivo.getvalue())
            filas = ejecutar_con_progreso(
                lambda p: transformacion.crear_sm(plan, carga, gateway(carga.payloads), p), "ARTICULO")
        except Exception as e:
            manejar_error(e)
            return
        ruta = carga.guardar_log("SM", filas)
        terminar("res_nueva_tf", (fid, carga.id, filas, ruta), "sel_tf", carga.id, "plan_transf")


def cargas_transformacion(s):
    st.markdown("### Cargas")
    if "msg_cierre_tf" in ss:
        st.success(ss.pop("msg_cierre_tf"))
    carga = elegir_carga(CargaTransformacion, s, "sel_tf", lambda c: c.tiene_pendientes())
    if not carga:
        return
    con_sm, pend_sm, pend_em = carga.con_sm(), carga.sm_pendientes(), carga.em_pendientes()
    cerrados = sum(e[x]["estado"] == Estado.CERRADO.value for e in carga.articulos.values() for x in ("sm", "em"))
    c = st.columns(5)
    c[0].metric("Artículos", len(carga.articulos))
    c[1].metric("SM creadas", len(con_sm))
    c[2].metric("EM creadas", sum(e["em"]["estado"] == Estado.OK.value for _, e in con_sm))
    c[3].metric("EM pendientes", len(pend_em))
    c[4].metric("Cerrados", cerrados)

    if pend_sm:
        sm_pendientes(s, carga, pend_sm)
    etapa_em(s, carga, pend_em)
    cerrar_transformacion(s, carga)

    res = ss.get("res_transf", (None,))
    if res[0] == carga.id:
        st.markdown("#### Resultado del último envío")
        mostrar_resultado(*res[1:])

    with st.expander("Ver detalle completo de la carga"):
        st.dataframe(pd.DataFrame([{**carga.fila_log(k, "SM"), "ESTADO_EM": e["em"]["estado"]}
                                   for k, e in carga.articulos.items()]).drop(columns=["CARGA", "ETAPA"]).astype(str),
                     hide_index=True, width="stretch")


def sm_pendientes(s, carga: CargaTransformacion, pend: list):
    st.markdown("#### SM no creadas")
    df = pd.DataFrame([carga.fila_log(k, "SM") for k, _ in pend])
    st.dataframe(df[["ARTICULO", "BODEGA", "CANTIDAD", "LOTES_ORIGEN", "PAYLOAD_SM", "ESTADO", "INTENTOS",
                     "ERROR"]].astype(str), hide_index=True, width="stretch")
    avisos_estados(df["ESTADO"])
    n_revisar = int((df["ESTADO"] == Estado.REVISAR.value).sum())
    con_revisar = incluir_revisar(n_revisar, "rev_sm")
    n = len(transformacion.sm_enviables(carga, con_revisar))
    if n and confirmar(f"Confirmo reenviar {n} SM de {carga.id} en {etiqueta_base(s)}", "conf_sm_re") \
            and st.button(f"Reenviar {n} SM (mismo JSON)", type="primary", key="run_sm_re"):
        try:
            filas = ejecutar_con_progreso(
                lambda p: transformacion.reenviar_sm(carga, gateway(carga.payloads), con_revisar, p), "ARTICULO")
        except Exception as e:
            manejar_error(e)
            return
        ruta = carga.guardar_log("SM_reenvio", filas)
        terminar("res_transf", (carga.id, filas, ruta), "sel_tf", carga.id)


def etapa_em(s, carga: CargaTransformacion, pend: list):
    st.markdown("#### Etapa 2 · Entradas de mercancía (EM)")
    if not carga.con_sm():
        st.caption("Se habilita cuando la carga tenga SM creadas.")
        return
    if not pend:
        st.success("No hay EM pendientes en esta carga.")
        return

    dias = CFG.dias_minimos_vencimiento
    df = pd.DataFrame([{
        "CLAVE": k, "Enviar": False, "Artículo": e["datos"]["articulo"], "Bodega": e["datos"]["bodega"],
        "Cantidad": e["datos"]["cantidad"], "SM": e["sm"]["doc_num"],
        "Lotes destino": ", ".join(f"{l['lote']} (vence {l['vencimiento']})" for l in e["datos"]["lotes_destino"]),
        "Costo unit.": e["em"]["costo_unit"], "Total asiento": e["em"]["asiento_total"],
        "Estado EM": e["em"]["estado"] or "PENDIENTE",
        "Alerta": ", ".join(f"{l} vence a < {dias} días" for l, _ in
                            transformacion.vencen_antes(e["datos"]["lotes_destino"], dias)),
        "Observación": " | ".join(x for x in (e["em"]["error"], e["em"]["nota"],
                                              (e["em"].get("correcciones") or [""])[-1]) if x)} for k, e in pend])
    editado = st.data_editor(
        df, hide_index=True, width="stretch", key=f"ed_em_{carga.id}_{ss.n_ejecuciones}",
        column_order=[c for c in df.columns if c != "CLAVE"],
        disabled=[c for c in df.columns if c != "Enviar"],
        column_config={"Enviar": st.column_config.CheckboxColumn("Enviar"),
                       "Costo unit.": st.column_config.NumberColumn(format="%.6f"),
                       "Total asiento": st.column_config.NumberColumn(format="%.2f")})
    seleccion = df.loc[editado["Enviar"], "CLAVE"].tolist()
    avisos_estados(df["Estado EM"])
    if (df["Alerta"] != "").any():
        st.warning(f"Hay lotes destino que vencen a menos de {dias} días: SAP rechazará esas EM. Si la fecha es "
                   "correcta (real físico), solicita la autorización temporal en SAP; si no, corrígela abajo.")
    corregir_em(carga)

    sin_costo = int(df["Costo unit."].isna().sum())
    if sin_costo:
        st.warning(f"{sin_costo} artículo(s) sin costo calculado. Revisa la observación o recalcula.")
        if st.button("Recalcular costos", key="recalc"):
            with st.spinner("Leyendo asientos de las SM..."):
                try:
                    transformacion.recalcular_costos(carga, gateway())
                except Exception as e:
                    manejar_error(e)
            recargar()

    n_revisar = sum(e["em"]["estado"] == Estado.REVISAR.value for _, e in pend)
    con_revisar = incluir_revisar(n_revisar, "rev_em")
    enviables = transformacion.em_enviables(carga, con_revisar)
    sel = [k for k in seleccion if k in enviables]
    if enviables and confirmar(f"Confirmo crear EM de {carga.id} en {etiqueta_base(s)}", "conf_em"):
        b1, b2 = st.columns(2)
        claves = None
        if b1.button(f"Crear EM seleccionadas ({len(sel)})", disabled=not sel, key="run_em_sel"):
            claves = sel
        if b2.button(f"Crear todas las EM pendientes ({len(enviables)})", type="primary", key="run_em_all"):
            claves = enviables
        if claves:
            try:
                filas = ejecutar_con_progreso(
                    lambda p: transformacion.crear_em(carga, gateway(carga.payloads), claves, con_revisar, p),
                    "ARTICULO")
            except Exception as e:
                manejar_error(e)
                return
            ruta = carga.guardar_log("EM", filas)
            terminar("res_transf", (carga.id, filas, ruta), "sel_tf", carga.id)


def corregir_em(carga: CargaTransformacion):
    """Editor de nombre y vencimiento de los lotes destino de las EM pendientes (no creadas)."""
    for a in ss.pop("avisos_corr", []):
        st.warning(a)
    corregibles = transformacion.em_corregibles(carga)
    if not corregibles:
        return
    hay_error = any(carga.get(k)["em"]["estado"] == Estado.ERROR.value for k in corregibles)
    with st.expander("Corregir lote o vencimiento de EM pendientes", expanded=hay_error):
        st.caption("Solo se puede cambiar el nombre y el vencimiento de los lotes destino. Cantidades, bin, cuenta "
                   "y dimensiones deben calzar con la SM ya creada. Al guardar, el próximo envío arma un JSON "
                   "nuevo; el anterior queda guardado en la carga.")
        filas = [{"CLAVE": k, "LOTE_ACTUAL": l["lote"], "Artículo": carga.get(k)["datos"]["articulo"],
                  "Estado EM": carga.get(k)["em"]["estado"] or "PENDIENTE", "Cantidad": l["cantidad"],
                  "Lote destino": l["lote"], "Vencimiento": date.fromisoformat(l["vencimiento"])}
                 for k in corregibles for l in carga.get(k)["datos"]["lotes_destino"]]
        df = pd.DataFrame(filas)
        editado = st.data_editor(
            df, hide_index=True, width="stretch", key=f"corr_{carga.id}_{ss.n_ejecuciones}",
            column_order=["Artículo", "Estado EM", "Cantidad", "Lote destino", "Vencimiento"],
            disabled=["Artículo", "Estado EM", "Cantidad"],
            column_config={"Vencimiento": st.column_config.DateColumn(format="YYYY-MM-DD")})
        cambios: dict[str, dict] = {}
        for (_, o), (_, n) in zip(df.iterrows(), editado.iterrows()):
            venc = pd.Timestamp(n["Vencimiento"]).date() if pd.notna(n["Vencimiento"]) else None
            if n["Lote destino"] != o["Lote destino"] or venc != o["Vencimiento"]:
                cambios.setdefault(o["CLAVE"], {})[o["LOTE_ACTUAL"]] = (
                    n["Lote destino"], venc.isoformat() if venc else "")
        if st.button(f"Guardar correcciones ({sum(len(c) for c in cambios.values())} lote(s))",
                     disabled=not cambios, key="guardar_corr"):
            avisos = []
            try:
                for clave, c in cambios.items():
                    avisos += transformacion.corregir_em(carga, clave, c, gateway(), CFG.dias_minimos_vencimiento)
            except Exception as e:
                manejar_error(e)
                return
            ss.avisos_corr = avisos
            ss.n_ejecuciones += 1
            recargar()


# --------------------------------------------------------------------------- #
# Cerrar documentos que no se crearon
# --------------------------------------------------------------------------- #
AVISO_REVISAR_CIERRE = ("Hay documentos en REVISAR: podrían haberse creado en SAP sin que la app lo sepa. "
                        "Revísalo en SAP antes de cerrarlos; si existen, no aparecerán en Documentos creados.")


def bloque_cierre(s, carga, opciones: list, etiqueta, avisos, cerrar_fn, msg_key: str, clave: str):
    """Selector + motivo + confirmación para cerrar documentos pendientes de una carga."""
    if not opciones:
        return
    with st.expander("Cerrar documentos que no se crearon"):
        st.caption("Un documento cerrado deja de quedar pendiente y no se puede reenviar. Queda registrado quién, "
                   "cuándo y por qué; la carga pasa a terminada cuando no le quedan pendientes.")
        n = ss.n_ejecuciones
        sel = st.multiselect("Documentos a cerrar", range(len(opciones)), format_func=lambda i: etiqueta(opciones[i]),
                             key=f"sel_{clave}_{carga.id}_{n}")
        elegidos = [opciones[i] for i in sel]
        for a in avisos(elegidos):
            st.warning(a)
        motivo = st.text_input("Motivo del cierre (obligatorio)", max_chars=200, key=f"mot_{clave}_{carga.id}_{n}")
        if elegidos and confirmar(f"Confirmo cerrar {len(elegidos)} documento(s) de {carga.id}: no se podrán "
                                  "reenviar", f"conf_{clave}")                 and st.button(f"Cerrar {len(elegidos)} documento(s)", disabled=not motivo.strip(), key=f"run_{clave}"):
            try:
                filas = cerrar_fn(elegidos, motivo)
            except ValueError as e:
                st.error(str(e))
                return
            ruta = carga.guardar_log("cierre", filas)
            olvidar_resultado_nuevo(carga.id)
            ss[msg_key] = f"Se cerraron {len(filas)} documento(s) de la carga {carga.id}. Log: {ruta.name}"
            ss.n_ejecuciones += 1
            recargar()


def cerrar_traslados(s, carga: CargaTraslado):
    pend = carga.pendientes()

    def avisos(elegidos):
        return [AVISO_REVISAR_CIERRE] if any(d["estado"] == Estado.REVISAR.value for d in elegidos) else []

    bloque_cierre(s, carga, pend,
                  lambda d: f"{d['doc']} · {d['estado'] or 'PENDIENTE'} · {d['articulos'][:60]}",
                  avisos, lambda elegidos, motivo: traslado.cerrar(carga, [d["doc"] for d in elegidos], s.usuario, motivo),
                  "msg_cierre_tr", "cierre_tr")


def cerrar_transformacion(s, carga: CargaTransformacion):
    opciones = transformacion.cerrables(carga)

    def etiqueta(o):
        clave, etapa = o
        e = carga.get(clave)
        extra = f" · SM {e['sm']['doc_num']} creada" if etapa == "EM" else ""
        return f"{etapa} · {e['datos']['articulo']} ({e['datos']['bodega']}) · {e[etapa.lower()]['estado'] or 'PENDIENTE'}{extra}"

    def avisos(elegidos):
        out = []
        em = [carga.get(k) for k, etapa in elegidos if etapa == "EM"]
        if em:
            out.append("Cerrar una EM deja la mercadería de su SM fuera de stock (SM "
                       + ", ".join(str(e["sm"]["doc_num"]) for e in em)
                       + "). Deberás resolverlo directamente en SAP (por ejemplo, con una EM manual).")
        if any(carga.get(k)[etapa.lower()]["estado"] == Estado.REVISAR.value for k, etapa in elegidos):
            out.append(AVISO_REVISAR_CIERRE)
        return out

    bloque_cierre(s, carga, opciones, etiqueta, avisos,
                  lambda elegidos, motivo: transformacion.cerrar(carga, elegidos, s.usuario, motivo),
                  "msg_cierre_tf", "cierre_tf")


# --------------------------------------------------------------------------- #
# Pestaña: documentos creados
# --------------------------------------------------------------------------- #
COLUMNAS_DOCS = {"FECHA": "Fecha", "TIPO": "Tipo de documento", "DOCNUM": "DocNum", "DOCENTRY": "DocEntry",
                 "CARGA": "Carga", "BODEGA": "Bodega", "ARTICULOS": "Artículos", "CANTIDAD": "Cantidad",
                 "REFERENCIA": "Referencia", "USUARIO": "Usuario", "CREADO": "Creado (app)"}


def tab_documentos():
    st.subheader("Documentos creados en SAP")
    st.caption("Todos los documentos que la app creó en la base conectada, de todas las cargas. "
               "Fecha = fecha del documento en SAP (o la de envío, en documentos anteriores a la versión 1.1).")
    s = ss.get("sesion")
    if not s:
        st.info("Conéctate a SAP en la barra lateral para ver los documentos de esa base.")
        return
    docs = registro.documentos_creados(s.company_db)
    if not docs:
        st.info(f"Aún no hay documentos creados con la app en {s.company_db}.")
        return
    df = pd.DataFrame(docs)
    df["FECHA"] = df["FECHA_DOC"].where(df["FECHA_DOC"] != "", df["CREADO"].str[:10])
    fechas = pd.to_datetime(df["FECHA"]).dt.date

    c1, c2, c3 = st.columns([2, 2, 2])
    tipos = c1.multiselect("Tipo de documento", registro.TIPOS_DOCUMENTO, default=registro.TIPOS_DOCUMENTO)
    carga = c2.selectbox("Carga", ["Todas"] + list(dict.fromkeys(df["CARGA"])))
    rango = c3.date_input("Fechas", (fechas.min(), fechas.max()), format="YYYY-MM-DD")
    desde, hasta = (rango[0], rango[-1]) if isinstance(rango, (tuple, list)) and rango else (fechas.min(), fechas.max())

    filtro = df["TIPO"].isin(tipos) & fechas.between(desde, hasta)
    if carga != "Todas":
        filtro &= df["CARGA"] == carga
    vista = df.loc[filtro, list(COLUMNAS_DOCS)].rename(columns=COLUMNAS_DOCS)

    cols = st.columns(len(registro.TIPOS_DOCUMENTO) + 1)
    cols[0].metric("Documentos", len(vista))
    for col, tipo in zip(cols[1:], registro.TIPOS_DOCUMENTO):
        col.metric(tipo, int((vista["Tipo de documento"] == tipo).sum()))
    st.dataframe(vista.astype(str), hide_index=True, width="stretch")

    excel = io.BytesIO()
    vista.to_excel(excel, index=False)
    d1, d2 = st.columns(2)
    d1.download_button("Descargar Excel", excel.getvalue(), file_name=f"documentos_{s.company_db}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    d2.download_button("Descargar CSV", vista.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       file_name=f"documentos_{s.company_db}.csv", mime="text/csv", width="stretch")


# --------------------------------------------------------------------------- #
# Pestaña: plantillas
# --------------------------------------------------------------------------- #
def tab_plantillas():
    st.subheader("Plantillas de entrada")
    st.caption("Se lee la primera hoja de cada Excel. Los nombres de columna no distinguen mayúsculas. "
               "Cada ejecución crea una carga nueva con ID propio: para corregir envíos fallidos usa "
               "Reenviar en la carga, no vuelvas a subir el mismo Excel.")
    for nombre, (descripcion, df) in plantillas.PLANTILLAS.items():
        with st.container(border=True):
            st.markdown(f"**{nombre}.xlsx**  \n{descripcion}")
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Descargar plantilla", plantillas.excel_bytes(nombre), file_name=f"{nombre}.xlsx",
                               key=f"pl_{nombre}")


# --------------------------------------------------------------------------- #
barra_conexion()
st.title("Reorganización de stock SAP B1")
if not ss.get("sesion") and "aviso_sesion" in ss:
    st.warning(ss.aviso_sesion)
if ss.get("sesion") and ss.sesion.productiva:
    st.error(f"Conectado a la base **PRODUCTIVA** {ss.sesion.company_db} ({ss.sesion.empresa}). "
             "Los documentos que se ejecuten quedan en producción.")
st.caption("1 · Consolidar en PISO → 2 · Transformar lotes (SM y luego EM) → 3 · Redistribuir desde PISO. "
           "Validar no crea nada en SAP; solo los botones de crear o reenviar envían documentos.")
t1, t2, t3, t4 = st.tabs(["Traslado (pasos 1 y 3)", "Transformación (paso 2)", "Documentos creados", "Plantillas"])
with t1:
    tab_traslado()
with t2:
    tab_transformacion()
with t3:
    tab_documentos()
with t4:
    tab_plantillas()

# si alguna consulta de esta ejecución detectó la sesión expirada (sin pasar por un botón), pedir reconectar
if ss.get("sesion") and any(gw.sesion_expirada for gw in _GATEWAYS):
    expirar_sesion()
    st.rerun()
