"""
Encontra vídeos virais no YouTube sem chave de API (via yt-dlp).

Estratégia:
  1. Busca na página de resultados do YouTube com os filtros
     "data de envio = hoje/semana/mês" + "ordenar por visualizações" (modo flat: rápido)
  2. Extrai os metadados completos só dos mais vistos (data, likes, thumbnail...)
  3. Calcula views_por_hora = views / horas desde a publicação e filtra pelos limiares

Os termos vêm de TEMAS no config.py (ou de --termo).
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import yt_dlp

from config import MAX_VIRAIS, MIN_VIEWS, MIN_VIEWS_POR_HORA, RESULTADOS_POR_TERMO

logger = logging.getLogger(__name__)

_PERIODOS = {"hora": 1, "hoje": 2, "semana": 3, "mes": 4, "ano": 5}


def _url_busca(termo: str, periodo: str) -> str:
    """
    Monta a URL de busca com o parâmetro 'sp' (protobuf em base64):
      campo 1 = ordenação (3 = visualizações)
      campo 2 = filtros { 1: data de envio, 2: tipo (1 = vídeo) }
    """
    sp = base64.urlsafe_b64encode(bytes([0x08, 0x03, 0x12, 0x04, 0x08, _PERIODOS[periodo], 0x10, 0x01]))
    return f"https://www.youtube.com/results?search_query={quote(termo)}&sp={quote(sp.decode())}"


def info_para_dict(e: dict) -> dict:
    """Converte o dict do yt-dlp no formato usado pelo pipeline, já com as métricas."""
    ts = e.get("timestamp")
    return _metricas_virais({
        "video_id":     e.get("id"),
        "url":          e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        "titulo":       e.get("title") or "",
        "descricao":    e.get("description") or "",
        "canal":        e.get("channel") or e.get("uploader") or "",
        "tags":         "|".join(e.get("tags") or []),
        "publicado_em": datetime.fromtimestamp(ts, timezone.utc) if ts else None,
        "duracao_s":    e.get("duration"),
        "thumbnail":    e.get("thumbnail"),
        "views":        e.get("view_count") or 0,
        "likes":        e.get("like_count") or 0,
        "comentarios":  e.get("comment_count") or 0,
    })


def _metricas_virais(video: dict) -> dict:
    """Acrescenta horas desde a publicação, views/hora e taxa de engajamento."""
    publicado = video.get("publicado_em")
    horas = None
    if publicado:
        horas = max((datetime.now(timezone.utc) - publicado).total_seconds() / 3600, 1)
    views = video.get("views") or 0
    likes = video.get("likes") or 0
    comentarios = video.get("comentarios") or 0
    video["horas_desde_publicacao"] = round(horas, 1) if horas else None
    video["views_por_hora"] = round(views / horas) if horas else None
    video["engajamento"] = round((likes + comentarios) / views, 4) if views else None
    return video


def _eh_viral(video: dict) -> bool:
    return (video.get("views") or 0) >= MIN_VIEWS and (video.get("views_por_hora") or 0) >= MIN_VIEWS_POR_HORA


def _busca_flat(termo: str, periodo: str) -> list[dict]:
    """Lista rápida (id, título, views, duração) sem abrir cada vídeo."""
    opts = {"quiet": True, "extract_flat": True, "playlistend": RESULTADOS_POR_TERMO, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_url_busca(termo, periodo), download=False) or {}
    return [e for e in info.get("entries") or [] if e and e.get("id")]


def detalhes_video(url: str) -> Optional[dict]:
    """Metadados completos de um vídeo do YouTube."""
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "ignoreerrors": True}) as ydl:
        e = ydl.extract_info(url, download=False)
    return info_para_dict(e) if e else None


def _periodo_para_dias(dias: int) -> str:
    """Menor filtro do YouTube que cobre a janela pedida."""
    return "hoje" if dias <= 1 else "semana" if dias <= 7 else "mes" if dias <= 31 else "ano"


def buscar_virais(termos: list[str], periodo: str = "semana", dias: Optional[int] = None) -> list[dict]:
    """
    Retorna os vídeos virais ordenados por views_por_hora.

    dias → sobrepõe 'periodo': usa o filtro do YouTube que cobre a janela e
           descarta vídeos publicados antes de hoje - dias.
    """
    if dias:
        periodo = _periodo_para_dias(dias)

    rasos: dict[str, dict] = {}
    for t in termos:
        for e in _busca_flat(t, periodo):
            rasos.setdefault(e["id"], e)
    logger.info("%d vídeos listados em %d busca(s)", len(rasos), len(termos))

    # Só abre os mais vistos (cada extração completa é uma requisição)
    candidatos = sorted(rasos.values(), key=lambda e: e.get("view_count") or 0, reverse=True)
    candidatos = [e for e in candidatos if (e.get("view_count") or 0) >= MIN_VIEWS][:MAX_VIRAIS * 2]

    videos = []
    for e in candidatos:
        v = detalhes_video(f"https://www.youtube.com/watch?v={e['id']}")
        if v:
            videos.append(v)

    if dias:
        limite = datetime.now(timezone.utc) - timedelta(days=dias)
        videos = [v for v in videos if v["publicado_em"] and v["publicado_em"] >= limite]

    virais = [v for v in videos if _eh_viral(v)]
    virais.sort(key=lambda v: v.get("views_por_hora") or 0, reverse=True)
    logger.info("%d vídeos virais (de %d detalhados)", len(virais), len(videos))
    return virais[:MAX_VIRAIS]
