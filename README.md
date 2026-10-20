# Reorganización de stock SAP B1: ubicaciones y lotes

App local (Streamlit) que ordena una bodega de SAP Business One a través del Service Layer.

## Contexto

La bodega tiene muchos artículos en un **lote genérico** (por ejemplo `SL-01`), creado para poder activar el manejo de lotes. Además, el stock por ubicación (bin) de SAP no coincide con el físico. El objetivo es dejar cada artículo con sus lotes reales (con vencimiento) y en sus ubicaciones reales.

## Flujo

| Paso | Qué se hace | Documento SAP | Pestaña / Excel |
|---|---|---|---|
| 1. Consolidar | Todo el stock de cada ubicación pasa a **PISO** | Traslado de stock, misma bodega | Traslado · `1_traslado_consolidar_piso.xlsx` |
| 2. Transformar | Etapa 1: SM que saca los lotes de origen. Etapa 2: EM que ingresa los lotes destino al costo real de la SM | SM (`InventoryGenExits`) + EM (`InventoryGenEntries`, `BaseType 60`), **una línea cada una** | Transformación · `2_transformacion_lotes.xlsx` |
| 3. Redistribuir | Desde PISO a las ubicaciones reales, con los lotes reales | Traslado de stock, misma bodega | Traslado · `3_traslado_redistribuir.xlsx` |
| 4. Merma | El saldo del lote genérico que queda en PISO es merma | — | Fuera de la app |

## Uso

```bash
pip install -r requirements.txt
streamlit run app.py          # o doble clic en iniciar_app.bat → http://localhost:8501
```

1. **Conectar** (barra lateral): elegir la **empresa** y el **tipo de base** (Test o Productiva), que definen la CompanyDB. Luego ingresar usuario y clave. La clave solo se usa para el `Login`; después se trabaja con la sesión (cookie `B1SESSION` + node `ROUTEID`), que vive solo en la memoria de la app. Si la base es productiva, se muestra en rojo.
2. **Subir el Excel** en la pestaña que corresponda.
3. **Validar y simular**: lee el Excel, junta todos los errores, resuelve los bins contra SAP (solo lectura) y muestra el plan y el JSON. No crea nada.
4. **Crear / Reenviar**: requiere marcar la casilla de confirmación, que indica la base. Cada envío deja un log CSV.

| Empresa | Productiva | Test |
|---|---|---|
| Adclean | `CLPRD_ADCLEAN` | `CLTST1ADCLEAN` |
| Adquim | `CLPRDADQUIM` | `CLTSTADQUIM` |
| Adgreen | `CLPRDADGREEN` | `CLTSTADGREEN` |

Las bases se configuran en `reorg/config.py` (`EMPRESAS`). `config.ini`, opcional (copiar de `config.ini.example`), define la URL del Service Layer, `verify_ssl` (verificar el certificado HTTPS; desactivar solo si falla por certificado autofirmado) y `max_articulos_traslado`.

## Cargas

Cada ejecución es una **carga con ID propio**: `TR-AAAAMMDD-HHMMSS` en traslados y `TF-AAAAMMDD-HHMMSS` en transformaciones. Nace al presionar **Ejecutar** o **Crear SM**; validar no crea carga. **El nombre del Excel no se usa para nada**: subir el mismo archivo, un archivo editado o uno con otro nombre siempre crea una carga nueva.

```
salidas/<CompanyDB>/cargas/<ID>/
├── carga.json          # estado de cada documento; se reescribe después de cada envío
├── excel_<nombre>      # copia del Excel subido
├── payloads/           # JSON enviados a SAP
└── log_<etapa>_<fecha>.csv
```

Cada pestaña tiene una sección **Cargas**. Por defecto lista las cargas de la base con pendientes; con *Mostrar también cargas terminadas* aparecen todas. Desde ahí se reenvían los fallidos y se hace la etapa EM, **sin volver a subir el Excel**.

> Para el usuario: si un envío falla, corrígelo desde la carga con **Reenviar**. Volver a subir el Excel crea una carga nueva y crearía otra vez los documentos que ya quedaron OK.

**Aviso de duplicados (no bloquea).** Al validar, la app compara el contenido con **todas las cargas guardadas de la base**, de cualquier fecha, terminadas o no. Avisa si un traslado es idéntico a uno ya creado, si una transformación es idéntica a una ya hecha, o si el artículo tiene una EM pendiente en otra carga. Solo conoce lo hecho con esta app en esta carpeta, no documentos creados directamente en SAP.

## Envíos y JSON

Cada documento se **guarda primero como JSON** en `payloads/` de la carga y se envía **ese archivo exacto**. El log registra el nombre del archivo: `PAYLOAD` en traslado, y `PAYLOAD_SM` y `PAYLOAD_EM` en transformación. **Un reenvío usa el mismo JSON**, sin crear uno nuevo.

| Estado | Significado | ¿Se reenvía? |
|---|---|---|
| OK | Documento creado | — |
| ERROR | SAP lo rechazó: no se creó | Sí |
| NO_EJECUTADO | No se envió (la sesión expiró antes) | Sí |
| REVISAR | Sin respuesta (timeout, corte, o la app se cerró durante el envío): no se sabe si se creó | Solo marcando "revisé en SAP y no se creó" |

`carga.json` se guarda con reintentos, porque OneDrive puede bloquear el archivo por un instante mientras lo sincroniza.

## Excel de traslado (pasos 1 y 3), primera hoja

Una fila = un movimiento: `ARTICULO | BODEGA | BIN_ORIGEN | BIN_DESTINO | CANTIDAD | LOTE`

- Se arma **un traslado por bodega cada 20 artículos** como máximo. Cada artículo es una línea y todas sus filas van al mismo documento.
- En cada artículo, los lotes se suman y se **ordenan por cantidad descendente**. Las asignaciones `BinActionType 2` (sale) y `1` (entra) se agrupan por lote + bin.
- Se omiten las filas con origen = destino, y se avisa.
- Paso 1: el Excel debe calzar exactamente con el saldo **en SAP** por lote y bin (no con el físico); si no, SAP rechaza el documento.
- **Reenvío:** en la sección **Cargas**, elegir la carga y presionar **Reenviar**. Se envían solo los documentos que no quedaron OK, con su mismo JSON.

## Excel de transformación (paso 2), primera hoja

Una fila = un movimiento de lote: `ARTICULO | BODEGA | LOTE_ORIGEN | LOTE_DESTINO | VENCIMIENTO | CANTIDAD | BIN_ORIGEN | CUENTA | DIM1..DIM5`

- **Una SM y una EM por artículo, de una línea cada una.**
- SM: salen los `LOTE_ORIGEN` desde `BIN_ORIGEN`. **Puede haber varios lotes de origen por artículo.**
- EM: entran los `LOTE_DESTINO` en el **mismo** `BIN_ORIGEN`, con `ExpiryDate` = `VENCIMIENTO`.
- `CUENTA` (contrapartida) y `DIM1..DIM5` (`CostingCode..CostingCode5`) deben ser iguales en todas las filas del artículo, y se repiten en la SM y la EM.
- Un lote no puede ser de origen y de destino en el mismo artículo.

**Suposiciones (para el usuario de la app):**
- El **lote destino es nuevo**: se informa una sola fecha de vencimiento por lote. Si ya existe en SAP, **Validar** avisa, porque SAP conserva su fecha y no usa la del Excel.
- El bin de entrada es el mismo bin de salida (`BIN_ORIGEN`).
- Con varios lotes de origen, la EM lleva un solo costo unitario: el **promedio** de lo que salió en la SM.

**Etapa 1, SM (nueva carga).** Validar → **Crear SM**. Se crea la carga y se envían las SM. Después de cada SM se lee su asiento y se calcula el costo de la EM. Las SM que no se crearon se reenvían desde la carga, con el mismo JSON.

**Etapa 2, EM (en la carga).** Tabla con los artículos de la carga que tienen SM y aún no tienen EM. Cada fila muestra la SM, el costo unitario, el total del asiento, el estado y una casilla **Enviar**. Hay dos botones: **Crear EM seleccionadas** y **Crear todas las EM pendientes**. Las EM rechazadas siguen en la tabla para reenviarlas con el mismo JSON.

**Vencimiento mínimo (aviso, no bloquea).** SAP rechaza lotes nuevos que vencen a menos de 90 días (`dias_minimos_vencimiento` en `config.ini`). La app lo avisa al validar y en la tabla de EM, pero deja crear la SM. Si la fecha es real (físico), hay que solicitar la autorización temporal en SAP antes de crear la EM. Si es un error, se corrige en la carga.

**Corregir una EM pendiente.** En la etapa 2, *Corregir lote o vencimiento de EM pendientes* permite cambiar **el nombre y el vencimiento de los lotes destino** de las EM no creadas (pendientes, con error o no ejecutadas). Las cantidades, el bin, la cuenta y las dimensiones no se pueden cambiar, porque deben calzar con la SM ya creada. Al guardar:
- el JSON anterior queda en la carga (`PAYLOADS_EM_ANTERIORES` en el log);
- el próximo envío arma uno nuevo;
- la corrección queda anotada (`CORRECCIONES`).

Una EM en REVISAR no se puede corregir hasta confirmar en SAP que no se creó.

Un mismo artículo puede transformarse varias veces (por ejemplo, `SL-01 → A` y luego `A → B`): cada vez es una carga nueva.

**Costo unitario.** El asiento de la SM debe tener débito a `CUENTA` = crédito a inventario. El costo se calcula como ese monto dividido por la cantidad. Si el asiento no tiene esa forma, el costo queda vacío con el motivo en *Observación* y esa EM no se envía. Si la SM no generó asiento, la EM va con costo 0 y queda anotado.


## Estructura

```
reorg_stock_sap/
├── app.py                     # interfaz Streamlit: solo orquesta
├── iniciar_app.bat
├── config.ini.example
├── requirements.txt
├── plantillas/                # Excel de ejemplo (también descargables desde la app)
├── reorg/
│   ├── config.py              # EMPRESAS (bases) y config.ini
│   ├── dominio.py             # modelos, estados y errores compartidos (sin SAP ni pandas)
│   ├── plantillas.py
│   ├── sap/                   # ── CAPA API: único lugar que habla con el Service Layer ──
│   │   ├── cliente.py         #   login/logout, Sesion (cookie + node), GET/POST
│   │   ├── payloads.py        #   dominio → JSON de SAP (funciones puras)
│   │   └── gateway.py         #   bins, lotes, asiento; guardar JSON y enviar ese archivo
│   └── procesos/              # ── LÓGICA LOCAL: no importa requests ──
│       ├── excel.py           #   lectura y validación del Excel
│       ├── traslado.py        #   preparar / ejecutar / reenviar
│       ├── transformacion.py  #   preparar / crear_sm / crear_em / costos
│       ├── costo.py           #   costo unitario desde el asiento de la SM
│       ├── comun.py
│       └── registro.py        #   cargas: carga.json, copia del Excel, payloads y logs
└── salidas/<CompanyDB>/cargas/<ID>/   # una carpeta por carga, separado por base
```

Los CSV usan `;` y UTF-8 con BOM, así que se abren directo en Excel.

## Pendientes / riesgos conocidos

- Posible diferencia de pesos por redondear el `UnitPrice` a 6 decimales.
- No se consulta el saldo por bin o lote antes de mover (OIBQ/OBBQ no están expuestos por el Service Layer).
- Merma (lote genérico remanente en PISO): proceso posterior, no incluido.

## Historial

- 2026-10-05: v1 y v2, script de línea de comandos.
- 2026-10-06: v3. Paquete con capas `sap/` (API) y `procesos/` (lógica local), y app Streamlit con login en la app (cookie + node). Pasos 1 y 3 probados en `CLTST1ADCLEAN`.
- 2026-10-06: v4. Conexión por empresa + tipo de base, con aviso en rojo para productiva. SSL y URL quedan solo en `config.ini`. Cada documento se guarda como JSON y se envía ese archivo; el log registra su nombre. Reenvío de fallidos en traslados y aviso de duplicados por estado. Paso 2 con `LOTE_ORIGEN`/`LOTE_DESTINO` (varios orígenes, costo promedio), aviso si el lote destino ya existe, y dos etapas (SM, luego EM seleccionadas o todas) con registro JSON. Probado con un SAP simulado.
- 2026-10-07: v5. Cargas con ID propio (`TR-…`, `TF-…`): ya no se usa el nombre del Excel. Cada carga guarda su estado, una copia del Excel, los JSON y los logs. Sección **Cargas** para reenviar y hacer la etapa EM sin el Excel. Aviso de duplicados por contenido contra todas las cargas de la base. Guardado con reintentos ante bloqueos de OneDrive. Los registros de las pruebas anteriores en `CLTST1ADCLEAN` no se migran: la app no los lee.
- 2026-10-07: v6. Aviso (no bloqueo) de lotes destino que vencen a menos de 90 días, al validar y en la etapa EM. Corrección del nombre y el vencimiento de los lotes destino de EM pendientes, con JSON nuevo y el anterior conservado.
