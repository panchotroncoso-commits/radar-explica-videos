"""Publica en Instagram, a su hora, los videos de la cola. Corre en GitHub Actions cada 15 minutos.

Cola: cola/<slug>/video.mp4 + info.json {slug, texto, programado_para (ISO con zona), portada (opcional)}.
Instagram descarga el video desde la URL raw de este repo (por eso el repo es público) y lo publica como Reel.
Lo publicado se anota en publicadas.jsonl y se borra de la cola.

Secretos: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID. Sin secretos no hace nada.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

API = "https://graph.instagram.com/v23.0"
RAIZ = Path(__file__).resolve().parent
COLA = RAIZ / "cola"
REGISTRO = RAIZ / "publicadas.jsonl"
REPO = os.environ.get("GITHUB_REPOSITORY", "panchotroncoso-commits/radar-explica-videos")
RAMA = os.environ.get("GITHUB_REF_NAME", "main")
ESPERA_MAX = 900


def _ok(r: requests.Response) -> dict:
    if not r.ok:
        raise RuntimeError(f"Instagram {r.status_code}: {r.text[:300]}")
    return r.json()


def raw(ruta: Path) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{RAMA}/{ruta.relative_to(RAIZ).as_posix()}"


def publicar_reel(item: Path, info: dict, token: str, usuario: str) -> str:
    datos = {"media_type": "REELS", "video_url": raw(item / "video.mp4"), "caption": info.get("texto", "")[:2200],
             "share_to_feed": "true", "access_token": token}
    if (item / "portada.jpg").exists():
        datos["cover_url"] = raw(item / "portada.jpg")
    else:
        datos["thumb_offset"] = "500"
    contenedor = _ok(requests.post(f"{API}/{usuario}/media", data=datos, timeout=60))["id"]
    inicio = time.time()
    while True:
        e = _ok(requests.get(f"{API}/{contenedor}", params={"fields": "status_code,status", "access_token": token}, timeout=30))
        if e.get("status_code") == "FINISHED":
            break
        if e.get("status_code") in ("ERROR", "EXPIRED") or time.time() - inicio > ESPERA_MAX:
            raise RuntimeError(f"Instagram no procesó el video: {e}")
        time.sleep(15)
    p = _ok(requests.post(f"{API}/{usuario}/media_publish", data={"creation_id": contenedor, "access_token": token}, timeout=60))
    enlace = _ok(requests.get(f"{API}/{p['id']}", params={"fields": "permalink", "access_token": token}, timeout=30))
    return enlace.get("permalink") or f"https://www.instagram.com/reel/{p['id']}"


def main() -> int:
    token, usuario = os.environ.get("INSTAGRAM_ACCESS_TOKEN"), os.environ.get("INSTAGRAM_USER_ID")
    if not token or not usuario:
        print("Sin INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID: nada que hacer")
        return 0
    ahora = dt.datetime.now(dt.timezone.utc)
    hechos, fallos = [], []
    for info_path in sorted(COLA.glob("*/info.json")):
        item = info_path.parent
        info = json.loads(info_path.read_text())
        if "instagram" not in info.get("plataformas", ["instagram"]):
            continue
        cuando = info.get("programado_para")
        if cuando:
            hora = dt.datetime.fromisoformat(cuando)
            if hora.tzinfo is None:
                hora = hora.replace(tzinfo=dt.timezone(dt.timedelta(hours=-3)))  # Chile
            if hora > ahora:
                continue
        if not (item / "video.mp4").exists():
            continue
        try:
            url = publicar_reel(item, info, token, usuario)
        except Exception as e:  # noqa: BLE001
            print(f"{info['slug']}: {e}", file=sys.stderr)
            info.setdefault("intentos", []).append({"cuando": ahora.isoformat(timespec="minutes"), "error": str(e)[:300]})
            info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2))
            fallos.append(info["slug"])
            if len(info["intentos"]) >= 4:  # cuatro fallos (una hora): se saca de la cola para no insistir
                with REGISTRO.open("a") as f:
                    f.write(json.dumps({"slug": info["slug"], "plataforma": "instagram", "error": str(e)[:300],
                                        "cuando": ahora.isoformat(timespec="minutes")}, ensure_ascii=False) + "\n")
                subprocess.run(["git", "rm", "-rq", str(item)], check=True)
            continue
        with REGISTRO.open("a") as f:
            f.write(json.dumps({"slug": info["slug"], "plataforma": "instagram", "url": url,
                                "cuando": ahora.isoformat(timespec="minutes")}, ensure_ascii=False) + "\n")
        subprocess.run(["git", "rm", "-rq", str(item)], check=True)
        hechos.append(info["slug"])
        print(f"{info['slug']}: {url}")
    if hechos or fallos:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.name=Radar Explica", "-c", "user.email=radarexplica@gmail.com", "commit", "-qm",
                        f"publicado: {', '.join(hechos) or 'nada'}" + (f"; falló: {', '.join(fallos)}" if fallos else "")], check=True)
        subprocess.run(["git", "push", "-q"], check=True)
    print(f"Publicados: {len(hechos)}; fallos: {len(fallos)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
