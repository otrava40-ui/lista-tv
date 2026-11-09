#!/usr/bin/env python3
"""
Genera una lista IPTV propia, ordenada y solo con los canales que quieres.

Toma los canales de listas legales y gratuitas (TDTChannels y Free-TV),
se queda con los de canales.txt en ese orden y escribe lista.m3u8.
Si algún canal falla o desaparece, lo apunta en problemas.txt.

Uso:  python generar_lista.py
Sin dependencias: solo librería estándar de Python 3.8+.
"""

import os
import re
import sys
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------- CONFIGURACIÓN ----------------

# Listas de origen, en orden de preferencia.
FUENTES = [
    ("TDTChannels", "https://www.tdtchannels.com/lists/tv.m3u8"),
    ("Free-TV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"),
]

# Guía de programación (EPG) que verá TiviMate.
EPG_URL = "https://www.tdtchannels.com/epg/TV.xml.gz"

ARCHIVO_CANALES = "canales.txt"
ARCHIVO_SALIDA = "lista.m3u8"
ARCHIVO_PROBLEMAS = "problemas.txt"

# Comprobar que cada stream responde (VERIFICAR=0 para saltarlo).
VERIFICAR = os.environ.get("VERIFICAR", "1") != "0"
TIMEOUT = 10
USER_AGENT = ("Mozilla/5.0 (Linux; Android 11; AFTKA) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

# Palabras de calidad que se ignoran al comparar nombres.
RUIDO = {"hd", "fhd", "uhd", "sd", "4k", "1080p", "720p", "576p", "480p", "360p"}

# ------------------------------------------------


def normalizar(nombre):
    """'Aragón TV HD' -> 'aragon tv'. Mantiene calificativos como (Catalunya)."""
    n = unicodedata.normalize("NFKD", nombre)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    n = re.sub(r"\[[^\]]*\]", " ", n)                    # [Geo-blocked], [Not 24/7]
    n = re.sub(r"\((\d{3,4}p|hd|sd|fhd)\)", " ", n)       # (1080p)
    n = re.sub(r"[^a-z0-9()]+", " ", n)
    palabras = [p for p in n.split() if p not in RUIDO]
    return " ".join(palabras).strip()


def leer_canales(ruta):
    """Devuelve [(grupo, nombre_visible, [alias normalizados]), ...]."""
    canales, grupo = [], "TV"
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            if linea.startswith("[") and linea.endswith("]"):
                grupo = linea[1:-1].strip()
                continue
            nombres = [n.strip() for n in linea.split("|") if n.strip()]
            canales.append((grupo, nombres[0], [normalizar(n) for n in nombres]))
    return canales


def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def separar_extinf(linea):
    """Separa '#EXTINF:-1 attr="x",Nombre' en (atributos, nombre)."""
    dentro = False
    for i, c in enumerate(linea):
        if c == '"':
            dentro = not dentro
        elif c == "," and not dentro:
            cabecera, nombre = linea[:i], linea[i + 1:]
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', cabecera))
            return attrs, nombre.strip()
    return {}, ""


def parsear_m3u(texto, fuente):
    """Devuelve lista de entradas {nombre, attrs, extras, url, fuente}."""
    entradas, actual = [], None
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        if linea.startswith("#EXTINF"):
            attrs, nombre = separar_extinf(linea)
            actual = {"nombre": nombre, "attrs": attrs, "extras": [], "fuente": fuente}
        elif linea.startswith("#"):
            # #EXTVLCOPT / #KODIPROP llevan cabeceras necesarias para algunos canales
            if actual and (linea.startswith("#EXTVLCOPT") or linea.startswith("#KODIPROP")):
                actual["extras"].append(linea)
        elif actual:
            actual["url"] = linea
            entradas.append(actual)
            actual = None
    return entradas


def es_stream(url):
    """Distingue un stream real de un enlace a la web de la cadena."""
    u = url.lower()
    if not u.startswith(("http://", "https://")):
        return False
    return bool(re.search(r"\.(m3u8?|mpd|ts|mp4|aac|mp3)(\?|$|/)", u)
                or "/hls/" in u or "manifest" in u or "playlist" in u)


def cabeceras_de(entrada):
    h = {"User-Agent": USER_AGENT}
    for extra in entrada["extras"]:
        m = re.match(r"#EXTVLCOPT:http-(user-agent|referrer|origin)=(.+)", extra, re.I)
        if m:
            clave = {"user-agent": "User-Agent", "referrer": "Referer",
                     "origin": "Origin"}[m.group(1).lower()]
            h[clave] = m.group(2).strip()
    return h


def funciona(entrada):
    """True si el stream responde con algo que parece vídeo o lista HLS."""
    url = entrada["url"]
    if not url.lower().startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, headers=cabeceras_de(entrada))
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            tipo = (r.headers.get("Content-Type") or "").lower()
            datos = r.read(2048)
            return (b"#EXTM3U" in datos or "mpegurl" in tipo or "video" in tipo
                    or "mp2t" in tipo or "octet-stream" in tipo)
    except Exception:
        return False


def leer_lista_anterior(ruta):
    """Entradas de la última lista generada, por nombre visible, como reserva."""
    if not os.path.exists(ruta):
        return {}
    with open(ruta, encoding="utf-8") as f:
        return {e["nombre"]: e for e in parsear_m3u(f.read(), "anterior")}


def main():
    canales = leer_canales(ARCHIVO_CANALES)
    anterior = leer_lista_anterior(ARCHIVO_SALIDA)

    # 1. Descargar todas las fuentes
    candidatos = []
    for nombre_fuente, url in FUENTES:
        try:
            entradas = parsear_m3u(descargar(url), nombre_fuente)
            print(f"✓ {nombre_fuente}: {len(entradas)} canales")
            candidatos.extend(entradas)
        except Exception as e:
            print(f"✗ No se pudo descargar {nombre_fuente}: {e}")

    if not candidatos:
        print("Ninguna fuente disponible; no se toca la lista.")
        sys.exit(1)

    # 2. Buscar candidatos para cada canal (respetando el orden de fuentes)
    por_canal = []
    for grupo, visible, alias in canales:
        todos = [e for e in candidatos if normalizar(e["nombre"]) in alias]
        encontrados = [e for e in todos if es_stream(e["url"])]
        solo_web = bool(todos) and not encontrados
        por_canal.append((grupo, visible, encontrados, solo_web, todos))

    # 3. Comprobar streams en paralelo
    estado = {}
    if VERIFICAR:
        todas = {id(e): e for _, _, lista, _, _ in por_canal for e in lista}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for clave, ok in zip(todas, pool.map(funciona, todas.values())):
                estado[clave] = ok

    # 4. Elegir la mejor entrada de cada canal
    salida = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    problemas = []
    for numero, (grupo, visible, encontrados, solo_web, todos) in enumerate(por_canal, start=1):
        elegido, nota = None, None
        if encontrados and VERIFICAR:
            elegido = next((e for e in encontrados if estado.get(id(e))), None)
            if not elegido:
                # Puede ser bloqueo geográfico (GitHub está fuera de España)
                elegido = encontrados[0]
                nota = "no responde desde aquí (puede ser bloqueo fuera de España)"
        elif encontrados:
            elegido = encontrados[0]
        elif visible in anterior:
            elegido = anterior[visible]
            nota = "ya no aparece en las fuentes; se mantiene el enlace anterior"
        else:
            motivo = ("solo hay enlace a la web de la cadena, no se puede ver por IPTV"
                      if solo_web else "no está en ninguna fuente")
            problemas.append(f"⚠ {numero}. {visible}: {motivo}")
            continue

        if nota:
            marca = "❌" if "ya no aparece" in nota else "⚠"
            problemas.append(f"{marca} {numero}. {visible}: {nota}")

        # Usar el ID de guía de TDTChannels si existe, para que salga la programación
        tvg_id = next((e["attrs"].get("tvg-id") for e in todos
                       if e["fuente"] == "TDTChannels" and e["attrs"].get("tvg-id")),
                      elegido["attrs"].get("tvg-id", ""))
        logo = elegido["attrs"].get("tvg-logo") or next(
            (e["attrs"]["tvg-logo"] for e in encontrados if e["attrs"].get("tvg-logo")), "")

        salida.append(f'#EXTINF:-1 tvg-chno="{numero}" tvg-id="{tvg_id}" '
                      f'tvg-name="{visible}" tvg-logo="{logo}" '
                      f'group-title="{grupo}",{visible}')
        salida.extend(elegido["extras"])
        salida.append(elegido["url"])
        print(f"{numero:>3}. {visible:<20} ← {elegido['fuente']}"
              + (f"  ⚠ {nota}" if nota else ""))

    with open(ARCHIVO_SALIDA, "w", encoding="utf-8") as f:
        f.write("\n".join(salida) + "\n")

    with open(ARCHIVO_PROBLEMAS, "w", encoding="utf-8") as f:
        f.write("\n".join(problemas) + ("\n" if problemas else ""))

    total = sum(1 for l in salida if l.startswith("#EXTINF"))
    print(f"\nLista generada: {ARCHIVO_SALIDA} ({total} de {len(canales)} canales)")
    if problemas:
        print(f"⚠ {len(problemas)} aviso(s), mira {ARCHIVO_PROBLEMAS}:")
        print("\n".join("   " + p for p in problemas))


if __name__ == "__main__":
    main()
