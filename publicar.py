"""Publica en Instagram, a su hora, los videos de la cola. Corre en GitHub Actions cada 15 minutos.

La cola vive en la rama `ig-cola` del repo privado panchotroncoso-commits/radar-explica (la nube de Claude solo
puede escribir ahí). Este script la clona con la llave de despliegue RADAR_REPO_KEY; por cada pieza que ya llegó a
su hora copia el video a `media/` de ESTE repo (público) para que Instagram pueda descargarlo, lo publica, anota la
URL en publicadas.jsonl, borra el video de media/ y saca la pieza de la cola.

También copia la rama `tablero` del repo privado a docs/radar/ (GitHub Pages).

Secretos: INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID, RADAR_REPO_KEY.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

API = "https://graph.instagram.com/v23.0"
RAIZ = Path(__file__).resolve().parent
COLA = RAIZ / ".ig-cola"
TABLERO = RAIZ / ".tablero"
MEDIA = RAIZ / "media"
REGISTRO = RAIZ / "publicadas.jsonl"
REPO = os.environ.get("GITHUB_REPOSITORY", "panchotroncoso-commits/radar-explica-videos")
PRIVADO = "git@github.com:panchotroncoso-commits/radar-explica.git"
ESPERA_MAX = 900
CHILE = dt.timezone(dt.timedelta(hours=-3))


def git(*args, cwd: Path = RAIZ, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _ok(r: requests.Response) -> dict:
    if not r.ok:
        raise RuntimeError(f"Instagram {r.status_code}: {r.text[:300]}")
    return r.json()


def raw(ruta: Path) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/main/{ruta.relative_to(RAIZ).as_posix()}"


def subir_media(archivos: list[Path], mensaje: str) -> None:
    git("add", *[str(a) for a in archivos])
    git("-c", "user.name=Radar Explica", "-c", "user.email=radarexplica@gmail.com", "commit", "-qm", mensaje)
    git("push", "-q")
    for _ in range(20):  # raw.githubusercontent tarda unos segundos en servir lo recién subido
        if requests.head(raw(archivos[0]), timeout=20).status_code == 200:
            return
        time.sleep(6)


def publicar_reel(video: Path, portada: Path | None, texto: str, token: str, usuario: str) -> str:
    datos = {"media_type": "REELS", "video_url": raw(video), "caption": texto[:2200], "share_to_feed": "true",
             "access_token": token}
    if portada:
        datos["cover_url"] = raw(portada)
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


def ya_publicadas() -> set[str]:
    if not REGISTRO.exists():
        return set()
    return {json.loads(l)["slug"] for l in REGISTRO.read_text().splitlines() if l.strip() and '"url"' in l}


def anotar(d: dict) -> None:
    with REGISTRO.open("a") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def tablero() -> bool:
    """Copia la rama `tablero` del repo privado a docs/radar/ si cambió."""
    if git("clone", "-q", "--depth", "1", "-b", "tablero", PRIVADO, str(TABLERO), check=False).returncode:
        return False
    destino = RAIZ / "docs" / "radar"
    if destino.exists():
        shutil.rmtree(destino)
    shutil.copytree(TABLERO, destino, ignore=shutil.ignore_patterns(".git"))
    git("add", "-A", "docs/radar")
    return git("diff", "--cached", "--quiet", check=False).returncode != 0


def main() -> int:
    token, usuario = os.environ.get("INSTAGRAM_ACCESS_TOKEN"), os.environ.get("INSTAGRAM_USER_ID")
    cambio_tablero = tablero()
    if cambio_tablero:
        git("-c", "user.name=Radar Explica", "-c", "user.email=radarexplica@gmail.com", "commit", "-qm", "tablero")
        git("push", "-q")
        print("Tablero actualizado")
    if not token or not usuario:
        print("Sin INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID: nada que publicar")
        return 0
    hay_cola = git("clone", "-q", "--depth", "1", "-b", "ig-cola", PRIVADO, str(COLA), check=False).returncode == 0
    if not hay_cola:
        print("Sin rama ig-cola (o falta RADAR_REPO_KEY); solo la cola vieja de este repo")
    ahora = dt.datetime.now(dt.timezone.utc)
    hechas = ya_publicadas()
    sacar, resumen = [], []
    vieja = RAIZ / "cola"  # cola anterior (hasta el 28-oct) dentro de este repo
    for info_path in sorted(list(COLA.glob("*/info.json")) + list(vieja.glob("*/info.json"))):
        item = info_path.parent
        info = json.loads(info_path.read_text())
        slug = info["slug"]
        if slug in hechas:  # ya salió (otra corrida no alcanzó a sacarla de la cola)
            sacar.append(item)
            continue
        cuando = info.get("programado_para")
        if cuando:
            hora = dt.datetime.fromisoformat(cuando)
            hora = hora if hora.tzinfo else hora.replace(tzinfo=CHILE)
            if hora > ahora:
                continue
            if ahora - hora > dt.timedelta(hours=36):  # noticia vencida: no se publica
                anotar({"slug": slug, "plataforma": "instagram", "error": "vencida (más de 36 h de atraso)",
                        "cuando": ahora.isoformat(timespec="minutes")})
                sacar.append(item)
                continue
        if not (item / "video.mp4").exists():
            continue
        MEDIA.mkdir(exist_ok=True)
        video = MEDIA / f"{slug}.mp4"
        shutil.copy2(item / "video.mp4", video)
        portada = None
        if (item / "portada.jpg").exists():
            portada = MEDIA / f"{slug}.jpg"
            shutil.copy2(item / "portada.jpg", portada)
        try:
            subir_media([video] + ([portada] if portada else []), f"media: + {slug}")
            url = publicar_reel(video, portada, info.get("texto", ""), token, usuario)
        except Exception as e:  # noqa: BLE001
            print(f"{slug}: {e}", file=sys.stderr)
            intentos = info.setdefault("intentos", [])
            intentos.append({"cuando": ahora.isoformat(timespec="minutes"), "error": str(e)[:300]})
            info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2))
            if len(intentos) >= 4:  # una hora fallando: se saca para no insistir
                anotar({"slug": slug, "plataforma": "instagram", "error": str(e)[:300],
                        "cuando": ahora.isoformat(timespec="minutes")})
                sacar.append(item)
            resumen.append(f"falló {slug}")
            continue
        finally:
            for f in (video, portada):
                if f and f.exists():
                    git("rm", "-q", "--cached", str(f), check=False)
                    f.unlink()
        anotar({"slug": slug, "plataforma": "instagram", "url": url, "cuando": ahora.isoformat(timespec="minutes")})
        sacar.append(item)
        resumen.append(f"publicado {slug}")
        print(f"{slug}: {url}")
    for item in [i for i in sacar if vieja in i.parents]:
        shutil.rmtree(item)
    sacar = [i for i in sacar if vieja not in i.parents]
    # este repo: registro, media borrada y cola vieja
    git("add", "-A", "publicadas.jsonl", "media", "cola", check=False)
    if git("diff", "--cached", "--quiet", check=False).returncode:
        git("-c", "user.name=Radar Explica", "-c", "user.email=radarexplica@gmail.com", "commit", "-qm",
            "; ".join(resumen) or "registro")
        git("push", "-q")
    if not hay_cola:
        print(f"Listo: {', '.join(resumen) or 'nada a su hora'}")
        return 0
    # la cola: sacar lo publicado y guardar los intentos fallidos
    for item in sacar:
        shutil.rmtree(item)
    git("add", "-A", cwd=COLA)
    if git("diff", "--cached", "--quiet", cwd=COLA, check=False).returncode:
        git("-c", "user.name=Radar Explica", "-c", "user.email=radarexplica@gmail.com", "commit", "-qm",
            "; ".join(resumen) or "cola: limpieza", cwd=COLA)
        for _ in range(3):
            if git("push", "-q", "origin", "ig-cola", cwd=COLA, check=False).returncode == 0:
                break
            git("pull", "-q", "--rebase", "origin", "ig-cola", cwd=COLA, check=False)
    print(f"Listo: {', '.join(resumen) or 'nada a su hora'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
