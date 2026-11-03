"""
Transcrições com marcação de tempo, usadas para reconhecer cortes pela fala.

  - Vídeos longos do YouTube: legenda automática (json3) — não baixa o vídeo
  - Cortes de outras plataformas: baixa só o áudio e transcreve com faster-whisper (GPU)

Formato comum: lista de segmentos {"inicio": segundos, "texto": str}.
Tudo fica em cache em data/transcricoes/ (re-execuções não refazem o trabalho).
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

from config import TRANSCRICOES_DIR, WHISPER_DISPOSITIVO, WHISPER_MODELO, YTDLP_COOKIES_BROWSER

logger = logging.getLogger(__name__)

_modelo_whisper = None


def _cache(chave: str) -> Path:
    return TRANSCRICOES_DIR / f"{chave}.json"


def _ler_cache(chave: str) -> Optional[list[dict]]:
    p = _cache(chave)
    return json.loads(p.read_text()) if p.exists() else None


def _gravar_cache(chave: str, segmentos: list[dict]) -> list[dict]:
    _cache(chave).write_text(json.dumps(segmentos, ensure_ascii=False))
    return segmentos


class _YtdlpSilencioso:
    def debug(self, msg): logger.debug(msg)
    info = warning = error = debug


def _opts_ytdlp(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "logger": _YtdlpSilencioso(), **extra}
    if YTDLP_COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    return opts


# ─── YouTube: legenda automática ──────────────────────────────────────────────

def legenda_youtube(video_id: str) -> Optional[list[dict]]:
    chave = f"youtube_{video_id}"
    if (seg := _ler_cache(chave)) is not None:
        return seg

    with yt_dlp.YoutubeDL(_opts_ytdlp(skip_download=True)) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}

    # 'pt-orig' = transcrição original; 'pt' pode ser tradução automática
    faixas = {**info.get("automatic_captions", {}), **info.get("subtitles", {})}
    formatos = next((faixas[l] for l in ("pt-orig", "pt", "pt-BR") if l in faixas), None)
    url = next((f["url"] for f in formatos or [] if f.get("ext") == "json3"), None)
    if not url:
        logger.warning("Sem legenda em português para %s", video_id)
        return None

    eventos = None
    for tentativa in range(4):
        resp = requests.get(url, timeout=60)
        if resp.ok and resp.text.strip():
            eventos = resp.json().get("events", [])
            break
        # 429 / resposta vazia = limite de requisições do YouTube
        logger.info("Legenda %s: HTTP %s, nova tentativa em %ds", video_id, resp.status_code, 30 * 2 ** tentativa)
        time.sleep(30 * 2 ** tentativa)
    if eventos is None:
        logger.warning("Legenda %s indisponível (limite de requisições) — vídeo ignorado", video_id)
        return None

    segmentos = []
    for ev in eventos:
        texto = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if texto:
            segmentos.append({"inicio": ev.get("tStartMs", 0) / 1000, "texto": texto})
    logger.info("Legenda %s: %d segmentos", video_id, len(segmentos))
    return _gravar_cache(chave, segmentos)


# ─── Cortes: áudio + Whisper ──────────────────────────────────────────────────

def _carregar_libs_cuda() -> None:
    """
    Carrega cuBLAS/cuDNN dos pacotes pip nvidia-*-cu12 (o ctranslate2 não os acha sozinho
    quando não estão no LD_LIBRARY_PATH).
    """
    import ctypes
    import importlib.util

    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for raiz in map(Path, spec.submodule_search_locations):
        for padrao in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*", "cudnn/lib/libcudnn*.so.*"):
            for lib in sorted(raiz.glob(padrao)):
                try:
                    ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def _whisper():
    global _modelo_whisper
    if _modelo_whisper is None:
        if WHISPER_DISPOSITIVO == "cuda":
            _carregar_libs_cuda()
        from faster_whisper import WhisperModel

        tipo = "float16" if WHISPER_DISPOSITIVO == "cuda" else "int8"
        _modelo_whisper = WhisperModel(WHISPER_MODELO, device=WHISPER_DISPOSITIVO, compute_type=tipo)
    return _modelo_whisper


def transcrever_corte(plataforma: str, url: str, id_corte: str) -> Optional[list[dict]]:
    chave = f"{plataforma}_{id_corte}"
    if (seg := _ler_cache(chave)) is not None:
        return seg

    with tempfile.TemporaryDirectory() as tmp:
        opts = _opts_ytdlp(format="bestaudio/best", outtmpl=f"{tmp}/audio.%(ext)s")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            logger.debug("Falha ao baixar %s: %s", url, str(exc)[:120])
            return None
        arquivos = list(Path(tmp).glob("audio.*"))
        if not arquivos:
            return None
        try:
            trechos, _ = _whisper().transcribe(str(arquivos[0]), language="pt", vad_filter=True)
            segmentos = [{"inicio": round(t.start, 2), "texto": t.text.strip()} for t in trechos]
        except Exception as exc:
            logger.warning("Falha ao transcrever %s: %s", url, str(exc)[:120])
            return None

    return _gravar_cache(chave, segmentos)
