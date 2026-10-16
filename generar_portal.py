"""
=====================================================================
 PORTAL DE PROVEEDORES - DRIVIN  ·  Generador de HTML   ·  v4
=====================================================================
 Qué hace:
   1) Baja de Drivin (endpoint pods) los últimos 6 meses, día por día.
   2) Se queda SOLO con las rutas del proveedor configurado.
   3) Por cada escenario del día consulta el endpoint results para
      sacar los KM PLANIFICADOS de cada camión (incluye la vuelta al CeVe).
   4) Guarda el histórico en data/<proveedor>/AAAA-MM-DD.json
      (la próxima vez solo vuelve a bajar hoy y ayer).
   5) Arma un HTML estático en docs/<LINK>/index.html

 La API key NUNCA se escribe en el HTML. Se lee de:
   - la variable de entorno DRIVIN_API_KEY  (GitHub Actions), o
   - .streamlit/secrets.toml                (tu compu)

 Logo: guardá la imagen como "logo.png" en esta misma carpeta y se
 incrusta sola en el HTML (si no está, el portal sale sin logo).

 No se publican bultos, direcciones ni coordenadas.

 Uso:
   python generar_portal.py          -> datos reales
   python generar_portal.py --demo   -> datos inventados, para ver el diseño
=====================================================================
"""

import base64
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------
PROVEEDOR = "ECOLUB S.A"            # tal cual viene en employer_name
SLUG = "ecolub"                     # carpeta del histórico (data/ecolub)
LINK = "ecolub-q16srz3tr948"        # nombre del link publicado (difícil de adivinar)
                                    # ⚠️ si lo cambiás, cambia el link del proveedor
DIAS_HISTORIA = 180                 # 6 meses
DIAS_A_REFRESCAR = 2                # hoy y ayer se vuelven a bajar siempre

COLOR_FRANJA = "#0A2C6B"            # azul marino de la franja superior
ARCHIVO_LOGO = "logo.png"           # imagen del logo (opcional)

BASE_URL = "https://external.driv.in"
TZ_AR = timezone(timedelta(hours=-3))   # Argentina

RAIZ = Path(__file__).resolve().parent
CARPETA_DATOS = RAIZ / "data" / SLUG
CARPETA_SALIDA = RAIZ / "docs" / LINK


# ---------------------------------------------------------------
# API KEY
# ---------------------------------------------------------------
def leer_api_key():
    key = os.environ.get("DRIVIN_API_KEY")
    if key:
        return key.strip()
    secrets = RAIZ / ".streamlit" / "secrets.toml"
    if secrets.exists():
        import tomllib
        with open(secrets, "rb") as f:
            return tomllib.load(f)["drivin"]["api_key"]
    sys.exit("❌ No encontré la API key. Copiá la carpeta .streamlit con tu secrets.toml "
             "dentro de esta carpeta, o definí la variable DRIVIN_API_KEY.")


# ---------------------------------------------------------------
# DESCARGA
# ---------------------------------------------------------------
def _get(api_key, endpoint, params):
    headers = {"X-API-Key": api_key, "Content-Type": "application/json",
               "Accept": "application/json"}
    for intento in range(3):
        try:
            r = requests.get(f"{BASE_URL}/api/external/v2/{endpoint}",
                             headers=headers, params=params, timeout=180)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"   ⚠️ intento {intento + 1} falló ({endpoint} {params}): {e}")
            time.sleep(5 * (intento + 1))
    return None


def bajar_dia(api_key, dia):
    """Lista de registros de pods de un día (todos los transportes). None si falló."""
    data = _get(api_key, "pods", {"start_date": dia.isoformat(), "end_date": dia.isoformat()})
    if data is None:
        return None
    regs = data.get("response", data) if isinstance(data, dict) else data
    return regs if isinstance(regs, list) else []


def km_de_viaje(trip):
    """Km planificados de un viaje = planned_distance del regreso al depósito (última parada)."""
    paradas = trip.get("results") or []
    depositos = [p for p in paradas if p.get("deposit")]
    ultima = depositos[-1] if depositos else (paradas[-1] if paradas else {})
    try:
        return float(ultima.get("planned_distance") or 0) / 1000
    except (TypeError, ValueError):
        return 0.0


def km_planificados(api_key, tokens, camiones):
    """Devuelve {camion: km planificados del día} sumando todos sus viajes y escenarios.
       None si algún escenario no se pudo bajar."""
    km = {}
    for token in tokens:
        data = _get(api_key, "results", {"token": token})
        if data is None:
            return None
        rutas = data.get("response", []) if isinstance(data, dict) else []
        for ruta in rutas or []:
            camion = str(ruta.get("vehicle"))
            if camion not in camiones:
                continue
            total = sum(km_de_viaje(t) for t in (ruta.get("trips") or []))
            km[camion] = round(km.get(camion, 0) + total, 1)
    return km


def a_bool(x):
    if isinstance(x, str):
        return x.strip().lower() in ("true", "1", "si", "sí", "yes")
    return bool(x)


def es_del_proveedor(reg):
    return str(reg.get("employer_name") or "").strip() == PROVEEDOR


def achicar(reg):
    """Se queda solo con los campos necesarios (sin bultos, direcciones ni coordenadas)."""
    ordenes = []
    for o in reg.get("orders") or []:
        if isinstance(o, dict):
            ordenes.append({"s": o.get("status"), "r": o.get("reason")})
    return {
        "camion": str(reg.get("vehicle_code") or "s/d"),
        "chofer": str(reg.get("driver_name") or "Sin chofer"),
        "ini": a_bool(reg.get("route_is_started")),
        "fin": a_bool(reg.get("route_is_finished")),
        "pos": reg.get("position"),
        "suc": str(reg.get("address_customer_name") or ""),
        "lleg": reg.get("tracked_arrival"),
        "ords": ordenes,
    }


def archivo_completo(archivo):
    """True si el archivo ya tiene el formato nuevo (visitas + km)."""
    try:
        d = json.loads(archivo.read_text(encoding="utf-8"))
        return isinstance(d, dict) and "km" in d
    except (OSError, ValueError):
        return False


def actualizar_historico(api_key, hoy):
    CARPETA_DATOS.mkdir(parents=True, exist_ok=True)
    for i in range(DIAS_HISTORIA):
        dia = hoy - timedelta(days=i)
        archivo = CARPETA_DATOS / f"{dia.isoformat()}.json"
        if i >= DIAS_A_REFRESCAR and archivo.exists() and archivo_completo(archivo):
            continue
        regs = bajar_dia(api_key, dia)
        if regs is None:
            print(f"   ❌ {dia}: no se pudo bajar, queda para la próxima")
            continue
        propios = [r for r in regs if es_del_proveedor(r)]
        camiones = {str(r.get("vehicle_code")) for r in propios}
        tokens = {r.get("scenario_token") for r in propios if r.get("scenario_token")}
        km = km_planificados(api_key, tokens, camiones) if propios else {}
        if km is None:
            print(f"   ❌ {dia}: no se pudieron bajar los km, queda para la próxima")
            continue
        contenido = {"visitas": [achicar(r) for r in propios], "km": km}
        archivo.write_text(json.dumps(contenido, ensure_ascii=False), encoding="utf-8")
        print(f"   ✅ {dia}: {len(propios)} visitas · {len(camiones)} camiones · "
              f"{sum(km.values()):.0f} km planificados")

    # borrar lo que quedó fuera de los 6 meses
    limite = (hoy - timedelta(days=DIAS_HISTORIA)).isoformat()
    for f in CARPETA_DATOS.glob("*.json"):
        if f.stem < limite:
            f.unlink()


def leer_historico():
    datos = {}
    for f in sorted(CARPETA_DATOS.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, list):              # formato viejo, sin km
            d = {"visitas": d, "km": {}}
        datos[f.stem] = d
    return datos


# ---------------------------------------------------------------
# PROCESAMIENTO
# ---------------------------------------------------------------
def clasificar(status):
    s = str(status).lower().strip()
    return {"approved": "entregada", "rejected": "rechazada",
            "partial": "parcial"}.get(s, "pendiente")


def estado_cliente(lista_status):
    c = [clasificar(x) for x in lista_status]
    if not c:
        return "pendiente"
    if all(x == "rechazada" for x in c):
        return "rechazo_total"
    if "rechazada" in c:
        return "rechazo_parcial"
    if all(x == "entregada" for x in c):
        return "entregado"
    if all(x == "pendiente" for x in c):
        return "pendiente"
    return "parcial"


def procesar(historico):
    rutas, rechazos = [], []
    for dia_str, d in sorted(historico.items()):
        visitas, km_dia = d["visitas"], d.get("km") or {}
        grupos = {}
        for v in visitas:
            grupos.setdefault((v["camion"], v["chofer"]), []).append(v)

        km_usado = set()   # si un camión tuvo 2 choferes, los km se cuentan una sola vez
        for (camion, chofer), vs in sorted(grupos.items()):
            estados = [estado_cliente([o["s"] for o in v["ords"]]) for v in vs]
            km = None
            if camion in km_dia and camion not in km_usado:
                km = km_dia[camion]
                km_usado.add(camion)
            rutas.append({
                "f": dia_str, "chofer": chofer, "camion": camion,
                "ini": any(v["ini"] for v in vs),
                "fin": any(v["fin"] for v in vs),
                "cli": len(vs),
                "gest": sum(e != "pendiente" for e in estados),
                "ent": sum(e == "entregado" for e in estados),
                "rt": sum(e == "rechazo_total" for e in estados),
                "rp": sum(e == "rechazo_parcial" for e in estados),
                "marc": sum(1 for v in vs if v.get("lleg")),
                "km": km,
            })

            for v, est in zip(vs, estados):
                if not est.startswith("rechazo"):
                    continue
                rech = [o for o in v["ords"] if clasificar(o["s"]) == "rechazada"]
                rechazos.append({
                    "f": dia_str, "chofer": chofer, "camion": camion,
                    "suc": v["suc"],
                    "tipo": "Total" if est == "rechazo_total" else "Parcial",
                    "ords": f"{len(rech)} de {len(v['ords'])}",
                    "mot": sorted({o["r"] for o in rech if o["r"]}) or ["Sin motivo informado"],
                })
    return rutas, rechazos


# ---------------------------------------------------------------
# DATOS DE EJEMPLO (--demo)
# ---------------------------------------------------------------
def historico_demo(hoy):
    random.seed(7)
    choferes = [("AB123CD", "Juan Pérez"), ("AC456EF", "Carlos Gómez"),
                ("AD789GH", "Luis Fernández"), ("AE012IJ", "Martín Sosa"),
                ("AF345KL", "Diego Ramírez"), ("AG678MN", "Pablo Torres")]
    sucursales = [f"Dia Suc. {n:04d}" for n in range(200, 260)] + \
                 [f"Cencosud Suc. {n}" for n in range(500, 530)]
    motivos = ["Producto vencido", "Sucursal cerrada", "Sin espacio en depósito",
               "Pedido duplicado", "Fuera de horario"]
    hist = {}
    for i in range(DIAS_HISTORIA):
        dia = hoy - timedelta(days=i)
        if dia.weekday() == 6:
            hist[dia.isoformat()] = {"visitas": [], "km": {}}
            continue
        visitas, km = [], {}
        for k, (cam, chof) in enumerate(choferes):
            prende = random.random() > (0.25 if k == 3 else 0.05)
            marca = 0.3 if k == 4 else 0.9
            km[cam] = round(random.uniform(35, 140), 1)
            for p in range(1, random.randint(8, 15)):
                ords = []
                for _ in range(random.randint(1, 3)):
                    st = "pending" if not prende else (
                        "rejected" if random.random() < (0.08 if k == 1 else 0.03) else "approved")
                    ords.append({"s": st, "r": random.choice(motivos) if st == "rejected" else None})
                lleg = "x" if prende and random.random() < marca else None
                visitas.append({"camion": cam, "chofer": chof, "ini": prende, "fin": prende,
                                "pos": p, "suc": random.choice(sucursales), "lleg": lleg,
                                "ords": ords})
        hist[dia.isoformat()] = {"visitas": visitas, "km": km}
    return hist


# ---------------------------------------------------------------
# HTML
# ---------------------------------------------------------------
def logo_html():
    ruta = RAIZ / ARCHIVO_LOGO
    if not ruta.exists():
        return ""
    ext = ruta.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "svg": "image/svg+xml", "webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(ruta.read_bytes()).decode("ascii")
    return f'<img class="logo" src="data:{mime};base64,{b64}" alt="Grupo Bimbo">'


def generar_html(rutas, rechazos, ahora, demo=False):
    fechas = sorted({r["f"] for r in rutas})
    # Si se actualiza a la mañana, por defecto mostramos AYER (hoy todavía no arrancó)
    dia_def = ahora.date().isoformat()
    if ahora.hour < 11:
        dia_def = (ahora.date() - timedelta(days=1)).isoformat()
    if fechas and dia_def not in fechas:
        dia_def = max([f for f in fechas if f <= dia_def] or fechas)

    data = {"rutas": rutas, "rechazos": rechazos, "meta": {"dia_def": dia_def, "demo": demo}}
    data_js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = PLANTILLA.replace("__PROVEEDOR__", PROVEEDOR)
    html = html.replace("__ACTUALIZADO__", ahora.strftime("%d/%m/%Y %H:%M"))
    html = html.replace("__FRANJA__", COLOR_FRANJA)
    html = html.replace("__LOGO__", logo_html())
    html = html.replace("__DATA__", data_js)
    return html


PLANTILLA = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>__PROVEEDOR__ · Portal de transporte</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root { --azul:#0072CE; --marino:__FRANJA__; --rojo:#D32F2F; --verde:#2E7D32; --ambar:#ED6C02; --gris:#607D8B;
          --fondo:#F3F5F8; --texto:#1F2937; --suave:#6B7280; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Segoe UI",Roboto,Arial,sans-serif; background:var(--fondo); color:var(--texto); }
  header { background:var(--marino); color:#fff; padding:14px 28px; display:flex;
           justify-content:space-between; align-items:center; gap:20px; }
  header h1 { margin:0; font-size:1.35rem; font-weight:600; }
  header .sub { font-size:.82rem; opacity:.85; margin-top:4px; }
  header .logo { height:56px; width:auto; background:#fff; border-radius:8px; padding:4px 8px; flex-shrink:0; }
  .demo { background:#FFF3CD; color:#7A5A00; padding:8px 28px; font-size:.85rem; font-weight:600; }
  .filtros { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; padding:14px 28px;
             background:#fff; border-bottom:1px solid #E5E7EB; }
  .filtros label { display:flex; flex-direction:column; font-size:.72rem; color:var(--suave);
                   text-transform:uppercase; letter-spacing:.06em; font-weight:600; gap:4px; }
  .filtros input { font:inherit; font-size:.9rem; padding:6px 8px; border:1px solid #D1D5DB;
                   border-radius:6px; color:var(--texto); text-transform:none; letter-spacing:0; font-weight:400; }
  .filtros .sep { width:1px; align-self:stretch; background:#E5E7EB; }
  .chip { font:inherit; font-size:.8rem; padding:6px 10px; border:1px solid #D1D5DB; background:#fff;
          border-radius:16px; cursor:pointer; color:var(--texto); }
  .chip:hover { border-color:var(--azul); color:var(--azul); }
  nav.tabs { display:flex; gap:4px; padding:0 28px; background:#fff; border-bottom:1px solid #E5E7EB; overflow-x:auto; }
  nav.tabs button { font:inherit; font-size:.92rem; padding:12px 16px; border:none; background:none;
                    cursor:pointer; color:var(--suave); border-bottom:3px solid transparent; white-space:nowrap; }
  nav.tabs button.activa { color:var(--marino); border-bottom-color:var(--marino); font-weight:600; }
  main { padding:20px 28px 40px; }
  .tab { display:none; } .tab.activa { display:block; }
  h2 { font-size:1.05rem; margin:26px 0 10px; } h2:first-child { margin-top:4px; }
  .nota { font-size:.8rem; color:var(--suave); margin:-4px 0 10px; }
  .kpi-row { display:flex; gap:14px; flex-wrap:wrap; margin:6px 0 14px; }
  .kpi { flex:1; min-width:140px; background:#fff; border-radius:10px; padding:14px 18px;
         border-left:6px solid var(--azul); box-shadow:0 2px 6px rgba(0,0,0,.08); }
  .kpi .lbl { font-size:.7rem; letter-spacing:.09em; text-transform:uppercase; color:var(--suave); font-weight:600; margin-bottom:6px; }
  .kpi .val { font-size:1.7rem; font-weight:700; line-height:1.1; color:var(--azul); }
  .kpi .sub { font-size:.74rem; color:#9CA3AF; margin-top:4px; }
  .kpi.rojo{border-left-color:var(--rojo)} .kpi.rojo .val{color:var(--rojo)}
  .kpi.verde{border-left-color:var(--verde)} .kpi.verde .val{color:var(--verde)}
  .kpi.ambar{border-left-color:var(--ambar)} .kpi.ambar .val{color:var(--ambar)}
  .kpi.gris{border-left-color:var(--gris)} .kpi.gris .val{color:#455A64}
  .card { background:#fff; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,.08); padding:14px; margin-bottom:16px; }
  .card-tit { font-weight:600; margin-bottom:4px; }
  .split { display:grid; grid-template-columns:270px 1fr; gap:18px; align-items:start; }
  @media (max-width:900px) { .split { grid-template-columns:1fr; } header .logo { height:40px; } }
  .lista { background:#fff; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,.08); padding:10px;
           max-height:720px; overflow-y:auto; }
  .lista .busca { width:100%; font:inherit; font-size:.88rem; padding:7px 9px; border:1px solid #D1D5DB;
                  border-radius:6px; margin-bottom:8px; }
  .item { display:flex; justify-content:space-between; align-items:center; gap:8px; padding:8px 10px;
          border-radius:6px; cursor:pointer; font-size:.88rem; }
  .item:hover { background:#EEF5FD; }
  .item .s { font-size:.75rem; color:var(--suave); white-space:nowrap; }
  .item.total { font-weight:600; border-bottom:1px solid #EEF0F2; border-radius:6px 6px 0 0; margin-bottom:4px; }
  .item.sel { background:var(--marino); color:#fff; } .item.sel .s { color:#D6E8FA; }
  .detalle-tit { font-size:1.05rem; font-weight:600; margin:2px 0 10px; }
  .tabla-wrap { overflow-x:auto; max-height:460px; overflow-y:auto; }
  table { width:100%; border-collapse:collapse; font-size:.86rem; }
  th { position:sticky; top:0; background:#F9FAFB; text-align:left; padding:9px 10px; font-size:.72rem;
       text-transform:uppercase; letter-spacing:.05em; color:var(--suave); border-bottom:2px solid #E5E7EB;
       white-space:nowrap; cursor:pointer; user-select:none; }
  th:hover { color:var(--azul); }
  th .fl { font-size:.7rem; margin-left:4px; color:var(--azul); }
  td { padding:8px 10px; border-bottom:1px solid #F0F1F3; }
  td.n { text-align:right; font-variant-numeric:tabular-nums; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:.75rem; font-weight:600; }
  .b-ok { background:#E8F5E9; color:var(--verde); } .b-no { background:#FDECEA; color:var(--rojo); }
  .b-warn { background:#FFF3E0; color:var(--ambar); }
  .alerta { background:#fff; border-left:5px solid var(--rojo); border-radius:8px; padding:10px 14px;
            margin-bottom:8px; box-shadow:0 1px 4px rgba(0,0,0,.06); font-size:.9rem; }
  .ok-msg { background:#E8F5E9; color:var(--verde); border-radius:8px; padding:10px 14px; font-size:.9rem; }
  .vacio-msg { color:var(--suave); font-size:.88rem; padding:12px; }
  .acciones { display:flex; justify-content:space-between; align-items:center; gap:10px; margin:26px 0 10px; }
  .acciones h2 { margin:0; }
  .btn { font:inherit; font-size:.82rem; padding:7px 12px; border-radius:6px; border:1px solid var(--azul);
         background:#fff; color:var(--azul); cursor:pointer; }
  .btn:hover { background:var(--azul); color:#fff; }
  footer { text-align:center; font-size:.75rem; color:var(--suave); padding:20px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Portal de transporte · __PROVEEDOR__</h1>
    <div class="sub">Datos actualizados al __ACTUALIZADO__ hs · Fuente: Drivin · Se actualiza automáticamente a las 7, 14 y 17 hs</div>
  </div>
  __LOGO__
</header>
<div class="demo" id="bannerDemo" style="display:none">⚠️ DATOS DE EJEMPLO — esta versión es solo para ver el diseño</div>

<div class="filtros">
  <label>Día <input type="date" id="fDia"></label>
  <div class="sep"></div>
  <label>Período desde <input type="date" id="fDesde"></label>
  <label>Hasta <input type="date" id="fHasta"></label>
  <button class="chip" data-dias="7">7 días</button>
  <button class="chip" data-dias="30">30 días</button>
  <button class="chip" data-dias="90">3 meses</button>
  <button class="chip" data-dias="0">Todo</button>
</div>

<nav class="tabs">
  <button data-tab="resumen" class="activa">📋 Resumen del día</button>
  <button data-tab="alertas">🚨 Alertas</button>
  <button data-tab="util">🚦 Utilización</button>
  <button data-tab="rech">❌ Rechazos</button>
</nav>

<main>
  <div id="sinDatos" class="vacio-msg" style="display:none">Todavía no hay datos para mostrar.</div>

  <section class="tab activa" id="tab-resumen">
    <h2>Resumen del <span id="tDia"></span></h2>
    <p class="nota">Usa el filtro <b>Día</b>. Tocá el título de una columna para ordenar.</p>
    <div class="kpi-row" id="kResumen"></div>
    <div class="acciones"><h2>Viajes del día</h2><button class="btn" id="csvViajes">⬇ Descargar CSV</button></div>
    <p class="nota"><b>Km planificados</b>: distancia del plan de Drivin, incluye la vuelta al centro de venta.</p>
    <div class="card"><div class="tabla-wrap" id="tResumen"></div></div>
    <div class="acciones"><h2>Rechazos del día</h2><button class="btn" id="csvRechDia">⬇ Descargar CSV</button></div>
    <div class="card"><div class="tabla-wrap" id="tRechDia"></div></div>
  </section>

  <section class="tab" id="tab-alertas">
    <h2>Alertas del <span id="tDiaA"></span></h2>
    <p class="nota">Usa el filtro <b>Día</b>.</p>
    <div class="kpi-row" id="kAlertas"></div>
    <h2>🔴 Choferes que no iniciaron la ruta</h2>
    <div id="aNoInicio"></div>
  </section>

  <section class="tab" id="tab-util">
    <div class="acciones" style="margin-top:4px"><h2>Utilización del período</h2>
      <button class="btn" id="csvUtil">⬇ Descargar CSV por chofer</button></div>
    <p class="nota">Elegí un chofer de la lista para ver su detalle. <b>Total general</b> muestra a todos.</p>
    <div class="split">
      <div class="lista" id="lUtil"></div>
      <div>
        <div class="detalle-tit" id="tituloUtil"></div>
        <div class="kpi-row" id="kUtil"></div>
        <div class="card"><div class="card-tit" id="tituloGUtil"></div>
          <p class="nota" id="notaGUtil"></p><div id="gUtil"></div></div>
        <div id="bloqueRanking">
          <h2>Ranking del período</h2>
          <p class="nota">Ordenado por días sin iniciar. Tocá el título de una columna para ordenar.</p>
          <div class="card"><div class="tabla-wrap" id="tRanking"></div></div>
        </div>
      </div>
    </div>
  </section>

  <section class="tab" id="tab-rech">
    <h2>Rechazos del período</h2>
    <p class="nota">Elegí un chofer de la lista para ver su detalle. <b>Total general</b> muestra a todos.</p>
    <div class="split">
      <div class="lista" id="lRech"></div>
      <div>
        <div class="detalle-tit" id="tituloRech"></div>
        <div class="kpi-row" id="kRech"></div>
        <div class="card"><div class="card-tit">Evolución de rechazos</div><div id="gRechLinea"></div></div>
        <div class="card"><div class="card-tit">Motivos</div><div id="gMotivos"></div></div>
      </div>
    </div>
    <div class="acciones"><h2 id="tituloDetalle"></h2><button class="btn" id="csvRech">⬇ Descargar CSV</button></div>
    <div class="card"><div class="tabla-wrap" id="tRechDet"></div></div>
  </section>
</main>
<footer>Grupo Bimbo · Transporte · Información de uso exclusivo de __PROVEEDOR__</footer>

<script>const DATA = __DATA__;</script>
<script>
(function () {
  const RUTAS = DATA.rutas, RECH = DATA.rechazos, META = DATA.meta;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const suma = (arr, k) => arr.reduce((a, r) => a + (Number(r[k]) || 0), 0);
  const pct = (a, b) => b ? a / b * 100 : null;
  const fPct = x => x == null ? '—' : x.toFixed(1).replace('.', ',') + '%';
  const fNum = x => Number(x || 0).toLocaleString('es-AR');
  const fKm = x => x == null ? '—' : Number(x).toLocaleString('es-AR', {minimumFractionDigits: 1, maximumFractionDigits: 1});
  const fFecha = f => f ? f.split('-').reverse().join('/') : '';
  const restar = (f, n) => { const d = new Date(f + 'T12:00:00'); d.setDate(d.getDate() - n); return d.toISOString().slice(0, 10); };
  const agrupar = (arr, k) => arr.reduce((m, r) => ((m[r[k]] = m[r[k]] || []).push(r), m), {});
  const porNombre = (a, b) => a.localeCompare(b, 'es');
  const UN_DIA_MS = 24 * 3600 * 1000;

  const fechas = [...new Set(RUTAS.map(r => r.f))].sort();
  let tabActiva = 'resumen';
  let chofUtil = null, chofRech = null;
  const tablas = {};       // columnas y filas de cada tabla (para ordenar y para el CSV)
  const orden = {};        // orden elegido por tabla: {i: columna, dir: 1 | -1}
  const busqueda = {};     // texto del buscador de cada lista

  const LAYOUT = { margin: {t: 10, r: 10, b: 40, l: 50}, paper_bgcolor: '#fff', plot_bgcolor: '#fff',
                   font: {family: 'Segoe UI, Roboto, Arial', size: 12, color: '#1F2937'}, height: 300 };
  const CONFIG = { displayModeBar: false, responsive: true };
  const graficar = (id, trazas, layout) => {
    if (window.Plotly) Plotly.react(id, trazas, Object.assign({}, LAYOUT, layout || {}), CONFIG);
    else $(id).innerHTML = '<div class="vacio-msg">No se pudieron cargar los gráficos (revisá la conexión a internet).</div>';
  };
  const sinGrafico = (id, msg) => { if (window.Plotly) Plotly.purge(id); $(id).innerHTML = `<div class="vacio-msg">${msg}</div>`; };

  // ---------- filtros ----------
  const dia = () => $('fDia').value;
  const enRango = r => r.f >= $('fDesde').value && r.f <= $('fHasta').value;

  // ---------- componentes ----------
  function tarjetas(id, items) {
    $(id).innerHTML = items.map(([l, v, c, s]) =>
      `<div class="kpi ${c}"><div class="lbl">${esc(l)}</div><div class="val">${esc(v)}</div>${s ? `<div class="sub">${esc(s)}</div>` : ''}</div>`).join('');
  }

  const valorOrden = (c, f) => c.v ? c.v(f) : c.k ? f[c.k] : c.csv ? c.csv(f) : c.fmt ? c.fmt(f) : '';

  function tabla(id, cols, filas, opts = {}) {
    tablas[id] = {cols, filas, opts};
    if (!filas.length) { tablas[id].vista = []; $(id).innerHTML = `<div class="vacio-msg">${esc(opts.vacio || 'Sin datos para este filtro.')}</div>`; return; }
    let vista = filas;
    const o = orden[id];
    if (o && cols[o.i]) {
      const c = cols[o.i];
      vista = [...filas].sort((a, b) => {
        const x = valorOrden(c, a), y = valorOrden(c, b);
        if (x == null && y == null) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'es', {numeric: true})) * o.dir;
      });
    }
    tablas[id].vista = vista;
    const th = cols.map((c, i) => `<th data-c="${i}">${esc(c.t)}${o && o.i === i ? `<span class="fl">${o.dir > 0 ? '▲' : '▼'}</span>` : ''}</th>`).join('');
    const tb = vista.map(f => '<tr>' + cols.map(c => {
      const v = c.html ? c.html(f) : esc(c.fmt ? c.fmt(f) : f[c.k]);
      return `<td class="${c.n ? 'n' : ''}">${v}</td>`;
    }).join('') + '</tr>').join('');
    $(id).innerHTML = `<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`;
    $(id).querySelectorAll('th').forEach(h => h.addEventListener('click', () => {
      const i = +h.dataset.c, act = orden[id];
      orden[id] = {i, dir: act && act.i === i ? -act.dir : (cols[i].n ? -1 : 1)};
      tabla(id, cols, filas, opts);
    }));
  }

  function csvDesde(cols, filas, nombre) {
    if (!filas.length) return;
    const celda = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const lineas = [cols.map(c => celda(c.t)).join(';')]
      .concat(filas.map(f => cols.map(c => celda(c.csv ? c.csv(f) : (c.fmt ? c.fmt(f) : f[c.k]))).join(';')));
    const blob = new Blob(['\ufeff' + lineas.join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = nombre + '.csv'; a.click();
  }
  const csv = (idTabla, nombre) => { const t = tablas[idTabla]; if (t) csvDesde(t.cols, t.vista || t.filas, nombre); };

  function listaChoferes(id, items, sel, subTotal, onPick) {
    const cont = $(id);
    const fila = (c, sub, cls) => `<div class="item ${cls}" data-c="${esc(c)}"><span>${esc(c || 'Total general')}</span><span class="s">${esc(sub)}</span></div>`;
    cont.innerHTML = `<input type="search" class="busca" placeholder="🔎 Buscar chofer…">` +
      fila('', subTotal, 'total' + (sel ? '' : ' sel')) +
      items.map(it => fila(it.c, it.sub, it.c === sel ? 'sel' : '')).join('');
    const inp = cont.querySelector('.busca');
    const filtrar = () => {
      const q = inp.value.trim().toLowerCase();
      busqueda[id] = inp.value;
      cont.querySelectorAll('.item:not(.total)').forEach(el =>
        el.style.display = el.dataset.c.toLowerCase().includes(q) ? '' : 'none');
    };
    inp.value = busqueda[id] || '';
    filtrar();
    inp.addEventListener('input', filtrar);
    cont.querySelectorAll('.item').forEach(el => el.addEventListener('click', () => onPick(el.dataset.c || null)));
  }

  const badge = (ok, si = 'Sí', no = 'No') => `<span class="badge ${ok ? 'b-ok' : 'b-no'}">${ok ? si : no}</span>`;
  const colorPct = (x, bueno, malo) => x == null ? 'gris' : x >= bueno ? 'verde' : x >= malo ? 'ambar' : 'rojo';
  const colorPctInv = (x, bueno, malo) => x == null ? 'gris' : x <= bueno ? 'verde' : x <= malo ? 'ambar' : 'rojo';

  // ---------- RESUMEN ----------
  function renderResumen() {
    const rs = RUTAS.filter(r => r.f === dia());
    $('tDia').textContent = fFecha(dia());
    const viajes = rs.length, ini = suma(rs, 'ini'), fin = suma(rs, 'fin'), cli = suma(rs, 'cli');
    const ent = suma(rs, 'ent'), rech = suma(rs, 'rt') + suma(rs, 'rp'), km = suma(rs, 'km');
    tarjetas('kResumen', [
      ['Viajes', fNum(viajes), '', 'rutas asignadas'],
      ['% Utilización', fPct(pct(ini, viajes)), colorPct(pct(ini, viajes), 95, 85), `${ini} de ${viajes} rutas iniciadas`],
      ['Rutas finalizadas', `${fin}/${viajes}`, 'gris', ''],
      ['Clientes entregados', `${fNum(ent)}/${fNum(cli)}`, 'verde', ''],
      ['% Rechazo', fPct(pct(rech, cli)), colorPctInv(pct(rech, cli), 2, 5), `${rech} clientes con rechazo`],
      ['Km planificados', fKm(km), 'gris', 'total del día'],
    ]);
    tabla('tResumen', [
      {t: 'Chofer', k: 'chofer'}, {t: 'Camión', k: 'camion'},
      {t: 'Inició', html: r => badge(r.ini), csv: r => r.ini ? 'Sí' : 'No', v: r => r.ini ? 1 : 0},
      {t: 'Finalizó', html: r => badge(r.fin), csv: r => r.fin ? 'Sí' : 'No', v: r => r.fin ? 1 : 0},
      {t: 'Clientes', k: 'cli', n: 1}, {t: 'Gestionados', k: 'gest', n: 1},
      {t: 'Rechazos', fmt: r => r.rt + r.rp, v: r => r.rt + r.rp, n: 1},
      {t: 'Km planificados', fmt: r => fKm(r.km), v: r => r.km, n: 1},
    ], [...rs].sort((a, b) => porNombre(a.chofer, b.chofer)), {vacio: 'No hubo viajes este día.'});
    tabla('tRechDia', colsRechazo(false), RECH.filter(r => r.f === dia()), {vacio: '✅ Sin rechazos este día.'});
  }
  function colsRechazo(conFecha) {
    return (conFecha ? [{t: 'Fecha', fmt: r => fFecha(r.f), v: r => r.f}] : []).concat([
      {t: 'Chofer', k: 'chofer'}, {t: 'Camión', k: 'camion'}, {t: 'Sucursal', k: 'suc'},
      {t: 'Tipo', html: r => `<span class="badge ${r.tipo === 'Total' ? 'b-no' : 'b-warn'}">${r.tipo}</span>`, csv: r => r.tipo},
      {t: 'Órdenes rech.', k: 'ords'},
      {t: 'Motivo', fmt: r => r.mot.join(' · ')},
    ]);
  }

  // ---------- ALERTAS (solo el día) ----------
  function renderAlertas() {
    const rs = RUTAS.filter(r => r.f === dia());
    $('tDiaA').textContent = fFecha(dia());
    const noIni = rs.filter(r => !r.ini).sort((a, b) => porNombre(a.chofer, b.chofer));
    tarjetas('kAlertas', [
      ['No iniciaron', noIni.length, noIni.length ? 'rojo' : 'verde', `de ${rs.length} rutas`],
      ['Iniciaron', rs.length - noIni.length, 'verde', ''],
    ]);
    $('aNoInicio').innerHTML = !rs.length
      ? '<div class="vacio-msg">No hubo viajes este día.</div>'
      : noIni.length
        ? noIni.map(r => `<div class="alerta">🚨 <b>${esc(r.chofer)}</b> · camión ${esc(r.camion)} — no inició la ruta (${r.cli} clientes asignados)</div>`).join('')
        : '<div class="ok-msg">✅ Todos los choferes iniciaron su ruta.</div>';
  }

  // ---------- UTILIZACION ----------
  function resumenRutas(v) {
    return {dias: v.length, ini: suma(v, 'ini'), fin: suma(v, 'fin'), cli: suma(v, 'cli'),
            marc: suma(v, 'marc'), km: suma(v, 'km')};
  }
  const COLS_UTIL = [
    {t: 'Chofer', k: 'chofer'}, {t: 'Rutas', k: 'dias'}, {t: 'Iniciadas', k: 'ini'},
    {t: 'Sin iniciar', fmt: r => r.dias - r.ini}, {t: 'Finalizadas', k: 'fin'},
    {t: '% Utilización', fmt: r => fPct(pct(r.ini, r.dias))},
    {t: 'Paradas', k: 'cli'}, {t: 'Paradas sin marcar', fmt: r => r.cli - r.marc},
    {t: '% Marcadas', fmt: r => fPct(pct(r.marc, r.cli))},
    {t: 'Km planificados', fmt: r => fKm(r.km)},
  ];
  let filasUtilCsv = [];

  function renderUtil() {
    const rs = RUTAS.filter(enRango);
    const porChofer = agrupar(rs, 'chofer');
    if (chofUtil && !porChofer[chofUtil]) chofUtil = null;
    filasUtilCsv = Object.entries(porChofer).map(([chofer, v]) => Object.assign({chofer}, resumenRutas(v)))
      .sort((a, b) => porNombre(a.chofer, b.chofer));
    const tot = resumenRutas(rs);
    listaChoferes('lUtil',
      filasUtilCsv.map(x => ({c: x.chofer, sub: fPct(pct(x.ini, x.dias))})),
      chofUtil, fPct(pct(tot.ini, tot.dias)), c => { chofUtil = c; renderUtil(); });

    const base = chofUtil ? porChofer[chofUtil] : rs;
    const s = resumenRutas(base);
    $('tituloUtil').textContent = chofUtil ? `👤 ${chofUtil}` : '📊 Total general';
    const pU = pct(s.ini, s.dias), pM = pct(s.marc, s.cli);
    tarjetas('kUtil', [
      ['% Utilización', fPct(pU), colorPct(pU, 95, 85), `${fNum(s.ini)} de ${fNum(s.dias)} rutas iniciadas`],
      ['Rutas en el período', fNum(s.dias), 'gris', `${fNum(s.fin)} finalizadas`],
      ['Rutas sin iniciar', fNum(s.dias - s.ini), s.dias - s.ini ? 'rojo' : 'verde', ''],
      ['Paradas sin marcar', fNum(s.cli - s.marc), colorPct(pM, 90, 60), `${fPct(pM)} marcadas en la app`],
    ]);

    // Ranking: solo con "Total general"
    $('bloqueRanking').style.display = chofUtil ? 'none' : '';
    if (!chofUtil) {
      const ranking = filasUtilCsv.map(x => ({chofer: x.chofer, dias: x.dias, sinIni: x.dias - x.ini}))
        .sort((a, b) => b.sinIni - a.sinIni || porNombre(a.chofer, b.chofer));
      tabla('tRanking', [
        {t: 'Chofer', k: 'chofer'}, {t: 'Días con ruta', k: 'dias', n: 1},
        {t: 'Días sin iniciar', k: 'sinIni', n: 1},
        {t: '% sin iniciar', fmt: r => fPct(pct(r.sinIni, r.dias)), v: r => pct(r.sinIni, r.dias), n: 1},
      ], ranking);
    }

    const porDia = agrupar(base, 'f');
    const dias = Object.keys(porDia).sort();
    if (!dias.length) { sinGrafico('gUtil', 'Sin rutas en el período.'); return; }
    if (!chofUtil) {
      $('tituloGUtil').textContent = '% de rutas iniciadas por día';
      $('notaGUtil').textContent = 'Todos los choferes del proveedor.';
      graficar('gUtil', [{
        x: dias, y: dias.map(d => pct(suma(porDia[d], 'ini'), porDia[d].length)),
        type: 'scatter', mode: 'lines+markers', line: {color: '#0072CE', width: 2}, marker: {size: 5},
        name: '% Utilización', hovertemplate: '%{x|%d/%m/%Y}<br>%{y:.1f}% de rutas iniciadas<extra></extra>',
      }], {yaxis: {range: [0, 105], ticksuffix: '%'}, xaxis: {tickformat: '%d/%m'}});
    } else {
      $('tituloGUtil').textContent = 'Día a día';
      $('notaGUtil').textContent = '🟩 inició la ruta · 🟥 no la inició · la línea muestra el % de paradas que marcó en la app.';
      const inicio = d => suma(porDia[d], 'ini') === porDia[d].length;
      const si = dias.filter(inicio), no = dias.filter(d => !inicio(d));
      const ancho = UN_DIA_MS * 0.8;   // cada barra ocupa un día
      graficar('gUtil', [
        {type: 'bar', x: si, y: si.map(() => 100), width: si.map(() => ancho), name: 'Inició',
         marker: {color: 'rgba(67,160,71,.35)'}, hovertemplate: '%{x|%d/%m/%Y}<br>Inició la ruta<extra></extra>'},
        {type: 'bar', x: no, y: no.map(() => 100), width: no.map(() => ancho), name: 'No inició',
         marker: {color: 'rgba(211,47,47,.75)'}, hovertemplate: '%{x|%d/%m/%Y}<br><b>No inició la ruta</b><extra></extra>'},
        {type: 'scatter', mode: 'lines+markers', x: dias,
         y: dias.map(d => pct(suma(porDia[d], 'marc'), suma(porDia[d], 'cli'))),
         name: '% paradas marcadas', line: {color: '#0072CE', width: 2}, marker: {size: 5},
         hovertemplate: '%{x|%d/%m/%Y}<br>%{y:.0f}% de paradas marcadas<extra></extra>'},
      ], {barmode: 'overlay', yaxis: {range: [0, 105], ticksuffix: '%'},
          xaxis: {type: 'date', tickformat: '%d/%m'}, legend: {orientation: 'h', y: -0.2}, height: 330});
    }
  }

  // ---------- RECHAZOS ----------
  function renderRech() {
    const rs = RUTAS.filter(enRango);
    const rx = RECH.filter(enRango);
    const rutasChofer = agrupar(rs, 'chofer'), rxChofer = agrupar(rx, 'chofer');
    if (chofRech && !rutasChofer[chofRech]) chofRech = null;
    const items = Object.keys(rutasChofer).map(c => ({c, n: (rxChofer[c] || []).length}))
      .sort((a, b) => b.n - a.n || porNombre(a.c, b.c))
      .map(x => ({c: x.c, sub: `${x.n} rech.`}));
    listaChoferes('lRech', items, chofRech, `${rx.length} rech.`, c => { chofRech = c; renderRech(); });

    const baseR = chofRech ? rutasChofer[chofRech] : rs;
    const baseX = chofRech ? (rxChofer[chofRech] || []) : rx;
    const cli = suma(baseR, 'cli'), p = pct(baseX.length, cli);
    $('tituloRech').textContent = chofRech ? `👤 ${chofRech}` : '📊 Total general';
    tarjetas('kRech', [
      ['% Rechazo', fPct(p), colorPctInv(p, 2, 5), `sobre ${fNum(cli)} clientes visitados`],
      ['Clientes con rechazo', fNum(baseX.length), baseX.length ? 'rojo' : 'verde', ''],
      ['Rechazos totales', fNum(baseX.filter(r => r.tipo === 'Total').length), 'rojo', ''],
      ['Rechazos parciales', fNum(baseX.filter(r => r.tipo === 'Parcial').length), 'ambar', ''],
    ]);

    const dias = [...new Set(baseR.map(r => r.f))].sort();
    const cuenta = agrupar(baseX, 'f');
    if (dias.length) {
      graficar('gRechLinea', [{
        x: dias, y: dias.map(d => (cuenta[d] || []).length), type: 'scatter', mode: 'lines+markers',
        line: {color: '#D32F2F', width: 2}, marker: {size: 5}, fill: 'tozeroy', fillcolor: 'rgba(211,47,47,.08)',
        hovertemplate: '%{x|%d/%m/%Y}<br>%{y} clientes con rechazo<extra></extra>',
      }], {yaxis: {rangemode: 'tozero', title: 'Clientes con rechazo'}, xaxis: {tickformat: '%d/%m'}});
    } else sinGrafico('gRechLinea', 'Sin rutas en el período.');

    const mot = {};
    baseX.forEach(r => r.mot.forEach(m => { mot[m] = (mot[m] || 0) + 1; }));
    const motOrd = Object.entries(mot).sort((a, b) => a[1] - b[1]);
    if (motOrd.length) {
      graficar('gMotivos', [{
        type: 'bar', orientation: 'h', x: motOrd.map(m => m[1]), y: motOrd.map(m => m[0]),
        text: motOrd.map(m => m[1]), textposition: 'outside', marker: {color: '#D32F2F'},
        hovertemplate: '%{y}: %{x}<extra></extra>',
      }], {height: Math.max(240, 34 * motOrd.length + 50), margin: {t: 10, r: 40, b: 30, l: 190}});
    } else sinGrafico('gMotivos', '✅ Sin rechazos en el período.');

    $('tituloDetalle').textContent = `Detalle de rechazos · ${chofRech || 'Total general'}`;
    tabla('tRechDet', colsRechazo(true), [...baseX].sort((a, b) => b.f.localeCompare(a.f)),
          {vacio: '✅ Sin rechazos en el período.'});
  }

  // ---------- general ----------
  const RENDER = {resumen: renderResumen, alertas: renderAlertas, util: renderUtil, rech: renderRech};
  const render = () => RENDER[tabActiva]();

  function init() {
    if (META.demo) $('bannerDemo').style.display = 'block';
    if (!fechas.length) { $('sinDatos').style.display = 'block'; document.querySelectorAll('.tab').forEach(t => t.style.display = 'none'); return; }
    const primera = fechas[0], ultima = fechas[fechas.length - 1];
    ['fDia', 'fDesde', 'fHasta'].forEach(id => { $(id).min = primera; $(id).max = ultima; });
    $('fDia').value = fechas.includes(META.dia_def) ? META.dia_def : ultima;
    $('fHasta').value = ultima;
    $('fDesde').value = restar(ultima, 29) < primera ? primera : restar(ultima, 29);

    ['fDia', 'fDesde', 'fHasta'].forEach(id => $(id).addEventListener('change', render));
    document.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
      const n = +b.dataset.dias;
      $('fHasta').value = ultima;
      $('fDesde').value = n === 0 || restar(ultima, n - 1) < primera ? primera : restar(ultima, n - 1);
      render();
    }));
    document.querySelectorAll('nav.tabs button').forEach(b => b.addEventListener('click', () => {
      document.querySelectorAll('nav.tabs button').forEach(x => x.classList.remove('activa'));
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('activa'));
      b.classList.add('activa'); $('tab-' + b.dataset.tab).classList.add('activa');
      tabActiva = b.dataset.tab; render();
    }));
    $('csvViajes').addEventListener('click', () => csv('tResumen', 'viajes_' + dia()));
    $('csvRechDia').addEventListener('click', () => csv('tRechDia', 'rechazos_' + dia()));
    $('csvUtil').addEventListener('click', () => csvDesde(COLS_UTIL, filasUtilCsv, 'utilizacion_choferes'));
    $('csvRech').addEventListener('click', () => csv('tRechDet', 'rechazos_detalle'));
    render();
  }
  init();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main():
    demo = "--demo" in sys.argv
    ahora = datetime.now(TZ_AR).replace(tzinfo=None)
    hoy = ahora.date()

    if demo:
        print("🧪 Modo demo: datos inventados")
        historico = historico_demo(hoy)
        salida = RAIZ / f"demo_{SLUG}.html"
    else:
        print(f"⬇️  Actualizando histórico de {PROVEEDOR}...")
        actualizar_historico(leer_api_key(), hoy)
        historico = leer_historico()
        CARPETA_SALIDA.mkdir(parents=True, exist_ok=True)
        salida = CARPETA_SALIDA / "index.html"

    rutas, rechazos = procesar(historico)
    salida.write_text(generar_html(rutas, rechazos, ahora, demo), encoding="utf-8")
    print(f"✅ Listo: {salida}  ({len(rutas)} viajes, {len(rechazos)} rechazos)")


if __name__ == "__main__":
    main()
