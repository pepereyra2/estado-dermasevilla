#!/usr/bin/env python3
"""
Comprueba si las direcciones de las apps están afectadas por los bloqueos de IP de LaLiga
y guarda el estado y el historial en data/ (lo publica la página index.html).

Fuente de los bloqueos: listas públicas de hayahora.futbol (una por operador).
Se ejecuta cada 15 minutos con GitHub Actions (.github/workflows/comprobar.yml).

Para pruebas sin red se pueden usar variables de entorno:
  ESTADO_FUENTE   plantilla de URL de las listas, con {op} (p. ej. file:///tmp/listas/{op}.txt)
  ESTADO_DNS      JSON {"host": ["ip", ...]} para no resolver por DNS
  ESTADO_HTTP     JSON {"host": código_http} para no conectar a las webs
  ESTADO_AHORA    fecha/hora ISO a usar como «ahora»
"""
import datetime as dt
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request

OPERADORES = ["movistar", "vodafone", "orange", "masmovil", "digi"]
FUENTE = os.environ.get("ESTADO_FUENTE", "https://hayahora.futbol/estado/blocked-{op}.txt")
AGENTE = "estado-dermasevilla/1.0 (comprobacion cada 15 min; github actions)"
PUBLICAR_CADA_MIN = 60          # aunque no cambie nada, se publica como mínimo cada hora
FALLOS_PARA_CAIDA = 2           # comprobaciones seguidas sin respuesta para considerar la web caída
MAX_PERIODOS = 5000

DIR = os.path.dirname(os.path.abspath(__file__))
F_CONFIG = os.path.join(DIR, "config.json")
F_ESTADO = os.path.join(DIR, "data", "estado.json")
F_HIST = os.path.join(DIR, "data", "historial.json")
F_FLAG = os.path.join(DIR, ".publicar")


def ahora():
    if os.environ.get("ESTADO_AHORA"):
        return os.environ["ESTADO_AHORA"]
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def a_fecha(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def leer_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def escribir_json(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


def descargar(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def listas_bloqueo():
    """{operador: [redes] | None si no se pudo leer}"""
    res = {}
    for op in OPERADORES:
        try:
            txt = descargar(FUENTE.format(op=op))
        except Exception as e:  # noqa: BLE001
            print(f"  aviso: no se pudo leer la lista de {op}: {e}")
            res[op] = None
            continue
        redes = []
        for tok in txt.replace(",", " ").split():
            if tok.startswith("#"):
                continue
            try:
                redes.append(ipaddress.ip_network(tok.strip(), strict=False))
            except ValueError:
                pass
        res[op] = redes
    return res


def resolver(host):
    prueba = os.environ.get("ESTADO_DNS")
    if prueba:
        return sorted(json.loads(prueba).get(host, []))
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError:
        return []


def comprobar_http(host):
    prueba = os.environ.get("ESTADO_HTTP")
    if prueba:
        cod = json.loads(prueba).get(host)
        return {"ok": cod is not None and cod < 500, "codigo": cod}

    class SinRedirecciones(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(SinRedirecciones)
    req = urllib.request.Request(f"https://{host}/", headers={"User-Agent": AGENTE})
    try:
        with opener.open(req, timeout=25) as r:
            return {"ok": r.status < 500, "codigo": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": e.code < 500, "codigo": e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "codigo": None, "error": str(e)[:120]}


def operadores_que_bloquean(ips, listas):
    ops = []
    for op, redes in listas.items():
        if not redes:
            continue
        for ip in ips:
            try:
                dir_ip = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if any(dir_ip.version == r.version and dir_ip in r for r in redes):
                ops.append(op)
                break
    return ops


def abrir(hist, app, tipo, ruta, momento, operadores=None):
    hist["periodos"].append({"app": app, "tipo": tipo, "ruta": ruta, "inicio": momento, "fin": None,
                             "operadores": sorted(operadores or [])})


def cerrar(hist, app, tipo, ruta, momento):
    for p in reversed(hist["periodos"]):
        if p["app"] == app and p["tipo"] == tipo and p["ruta"] == ruta and p["fin"] is None:
            p["fin"] = momento
            return


def actualizar_abierto(hist, app, tipo, ruta, operadores):
    for p in reversed(hist["periodos"]):
        if p["app"] == app and p["tipo"] == tipo and p["ruta"] == ruta and p["fin"] is None:
            nuevos = sorted(set(p.get("operadores", [])) | set(operadores))
            cambiado = nuevos != p.get("operadores", [])
            p["operadores"] = nuevos
            return cambiado
    return False


def main():
    config = leer_json(F_CONFIG, {})
    previo = leer_json(F_ESTADO, {})
    hist = leer_json(F_HIST, {"periodos": []})
    hist.setdefault("periodos", [])
    momento = ahora()

    listas = listas_bloqueo()
    fuente_ok = any(v is not None for v in listas.values())
    cambios = False
    estado = {"titulo": config.get("titulo", "Estado del acceso"), "ultima_comprobacion": momento,
              "fuente_ok": fuente_ok, "operadores": OPERADORES,
              "listas_leidas": [op for op, v in listas.items() if v is not None], "apps": []}

    for app in config.get("apps", []):
        aid = app["id"]
        ant = next((a for a in previo.get("apps", []) if a.get("id") == aid), {})
        rutas = {}
        for ruta, host in (("publica", app.get("publica")), ("cloudflare", app.get("origen"))):
            if not host:
                continue
            ips = resolver(host)
            # Cloudflare no da las mismas IPs en todos los países: GitHub (EE. UU.) ve otras distintas de las que ve
            # un usuario en España, que son las que se bloquean. Si config.json trae «ips_espana», se comprueban esas.
            vistas = ips
            if ruta == "publica" and app.get("ips_espana"):
                ips = sorted(app["ips_espana"])
            ops = operadores_que_bloquean(ips, listas) if fuente_ok else ant.get("rutas", {}).get(ruta, {}).get("operadores", [])
            bloq = bool(ops)
            antes = bool(ant.get("rutas", {}).get(ruta, {}).get("bloqueada"))
            if fuente_ok and bloq and not antes:
                abrir(hist, aid, "bloqueo", ruta, momento, ops); cambios = True
            elif fuente_ok and not bloq and antes:
                cerrar(hist, aid, "bloqueo", ruta, momento); cambios = True
            elif bloq and actualizar_abierto(hist, aid, "bloqueo", ruta, ops):
                cambios = True
            rutas[ruta] = {"host": host, "ips": ips, "ips_github": vistas, "bloqueada": bloq, "operadores": ops}

        # ¿Responde la dirección pública? (desde fuera de España, así que no la afectan los bloqueos)
        http = comprobar_http(app["publica"]) if app.get("publica") else {"ok": True}
        fallos = 0 if http["ok"] else int(ant.get("fallos_seguidos", 0)) + 1
        caida, caida_antes = fallos >= FALLOS_PARA_CAIDA, bool(ant.get("caida"))
        if caida and not caida_antes:
            abrir(hist, aid, "caida", "publica", momento); cambios = True
        elif not caida and caida_antes:
            cerrar(hist, aid, "caida", "publica", momento); cambios = True
        if fallos != int(ant.get("fallos_seguidos", 0)):
            cambios = True

        estado["apps"].append({"id": aid, "nombre": app.get("nombre", aid), "publica": app.get("publica"),
                               "rutas": rutas, "http": http, "fallos_seguidos": fallos, "caida": caida})

    hist["periodos"] = hist["periodos"][-MAX_PERIODOS:]
    ultima_pub = previo.get("ultima_publicacion")
    vencida = not ultima_pub or (a_fecha(momento) - a_fecha(ultima_pub)).total_seconds() >= PUBLICAR_CADA_MIN * 60
    if cambios or vencida:
        estado["ultima_publicacion"] = momento
        escribir_json(F_ESTADO, estado)
        escribir_json(F_HIST, hist)
        open(F_FLAG, "w").close()
        print(f"Publicado ({'cambios' if cambios else 'actualización periódica'}).")
    else:
        estado["ultima_publicacion"] = ultima_pub
        print("Sin cambios.")
    for a in estado["apps"]:
        r = a["rutas"]
        txt = lambda x: ('BLOQUEADA ' + ','.join(x['operadores'])) if x.get('bloqueada') else 'libre'
        cf = f" · cloudflare {txt(r['cloudflare'])}" if 'cloudflare' in r else ''
        print(f"  {a['id']}: pública {txt(r.get('publica', {}))}{cf} · http {a['http'].get('codigo')}")


if __name__ == "__main__":
    main()
