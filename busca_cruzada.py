"""
Procura um vídeo do YouTube em outras plataformas e verifica se é o mesmo conteúdo.

Etapas:
  1. Gera consultas a partir do título/canal/hashtags do vídeo
  2. Busca na web com filtro site: por plataforma (DuckDuckGo via ddgs)
  3. Para cada candidato, tenta extrair metadados com yt-dlp
     (TikTok, X, Kwai e Dailymotion costumam funcionar sem login; Instagram/Facebook pedem cookies)
  4. Calcula um score de similaridade combinando:
       - texto     : similaridade entre títulos/legendas (rapidfuzz)
       - duração   : diferença em segundos
       - thumbnail : distância de hash perceptual (dHash)
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Optional

import requests
from PIL import Image
from rapidfuzz import fuzz

from config import (
    LIMIAR_CONFIRMACAO,
    LIMIAR_MATCH,
    PAUSA_ENTRE_BUSCAS,
    PADROES_URL_VIDEO,
    PESOS,
    PLATAFORMAS,
    RESULTADOS_POR_PLATAFORMA,
    YTDLP_COOKIES_BROWSER,
)

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no", "na", "nos", "nas",
    "um", "uma", "com", "por", "para", "pra", "que", "se", "the", "of", "and", "to", "in",
    "shorts", "short", "video", "vídeo", "oficial", "official", "viral",
}


# ─── Consultas ────────────────────────────────────────────────────────────────

def _limpar_titulo(titulo: str) -> str:
    """Remove hashtags, emojis, conteúdo entre colchetes/parênteses e pontuação."""
    t = re.sub(r"#\w+", " ", titulo)
    t = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _palavras_chave(titulo: str, n: int = 7) -> str:
    palavras = [p for p in _limpar_titulo(titulo).split() if p.lower() not in _STOPWORDS]
    return " ".join(palavras[:n])


def gerar_consultas(video: dict) -> list[str]:
    consultas = [_palavras_chave(video["titulo"])]
    if video.get("canal"):
        consultas.append(f'"{video["canal"]}" {_palavras_chave(video["titulo"], 4)}')
    hashtags = re.findall(r"#\w+", video["titulo"] + " " + video.get("descricao", "")[:300])
    hashtags = [h for h in hashtags if h.lower() not in ("#shorts", "#short")]
    if hashtags:
        # Hashtag sozinha é genérica demais: combina com as palavras do título
        consultas.append(f'{_palavras_chave(video["titulo"], 3)} {" ".join(hashtags[:2])}')
    return [c for c in dict.fromkeys(consultas) if c]


# ─── Busca web ────────────────────────────────────────────────────────────────

def _buscar_web(consulta: str) -> list[dict]:
    from ddgs import DDGS

    try:
        with DDGS() as ddgs:
            return list(ddgs.text(consulta, region="br-pt", max_results=RESULTADOS_POR_PLATAFORMA))
    except Exception as exc:
        logger.warning("Busca falhou para '%s': %s", consulta, exc)
        return []


def buscar_candidatos(video: dict) -> list[dict]:
    """Retorna candidatos únicos {plataforma, url, titulo_busca, snippet}."""
    vistos, candidatos = set(), []
    for consulta in gerar_consultas(video):
        for plataforma, filtro in PLATAFORMAS.items():
            for r in _buscar_web(f"{consulta} {filtro}"):
                url = r.get("href", "")
                if "watch" not in url:
                    url = url.split("?")[0]
                if not re.search(PADROES_URL_VIDEO[plataforma], url) or url in vistos:
                    continue
                vistos.add(url)
                candidatos.append({
                    "plataforma":   plataforma,
                    "url":          url,
                    "titulo_busca": r.get("title", ""),
                    "snippet":      r.get("body", ""),
                    "consulta":     consulta,
                })
            time.sleep(PAUSA_ENTRE_BUSCAS)
    logger.info("  %d candidatos para '%s'", len(candidatos), video["titulo"][:60])
    return candidatos


# ─── Metadados do candidato ───────────────────────────────────────────────────

class _YtdlpSilencioso:
    """Manda as mensagens do yt-dlp para o log em nível DEBUG (falhas são esperadas)."""
    def debug(self, msg): logger.debug(msg)
    info = warning = error = debug


def extrair_metadados(url: str) -> Optional[dict]:
    """Tenta obter título, legenda, duração, thumbnail e data via yt-dlp."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _YtdlpSilencioso()}
    if YTDLP_COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.debug("yt-dlp não extraiu %s: %s", url, str(exc)[:120])
        return None
    if not info or info.get("_type") == "playlist":
        return None
    return {
        "titulo":    info.get("title") or "",
        "descricao": info.get("description") or "",
        "autor":     info.get("uploader") or info.get("channel") or "",
        "duracao_s": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "timestamp": info.get("timestamp"),
        "views":     info.get("view_count"),
        "likes":     info.get("like_count"),
    }


# ─── Similaridade ─────────────────────────────────────────────────────────────

def _dhash(url: str, tamanho: int = 8) -> Optional[int]:
    """Hash perceptual (difference hash) da imagem."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("L").resize((tamanho + 1, tamanho))
    except Exception:
        return None
    px = list(img.getdata())
    bits = 0
    for lin in range(tamanho):
        for col in range(tamanho):
            esq = px[lin * (tamanho + 1) + col]
            dir_ = px[lin * (tamanho + 1) + col + 1]
            bits = (bits << 1) | (esq > dir_)
    return bits


def _sim_texto(video: dict, cand: dict, meta: Optional[dict]) -> float:
    ref = _limpar_titulo(video["titulo"]).lower()
    textos = [cand["titulo_busca"], cand["snippet"]]
    if meta:
        textos += [meta["titulo"], meta["descricao"][:500]]
    return max(fuzz.token_set_ratio(ref, _limpar_titulo(t).lower()) for t in textos if t) / 100


def _sim_duracao(d1: Optional[float], d2: Optional[float]) -> Optional[float]:
    if not d1 or not d2:
        return None
    diff = abs(d1 - d2)
    # Reposts costumam cortar/adicionar alguns segundos; tolerância proporcional
    return max(0.0, 1 - diff / max(3, 0.15 * max(d1, d2)))


def _sim_thumbnail(h1: Optional[int], h2: Optional[int]) -> Optional[float]:
    if h1 is None or h2 is None:
        return None
    bruto = 1 - bin(h1 ^ h2).count("1") / 64
    # Imagens sem relação dão ~0.5; reescala para 0 (aleatório) … 1 (≥ 0.9, quase idênticas)
    return min(1.0, max(0.0, (bruto - 0.5) / 0.4))


def pontuar(video: dict, cand: dict, meta: Optional[dict], hash_ref: Optional[int]) -> dict:
    """Score combinado; componentes ausentes são ignorados e os pesos renormalizados."""
    comp = {
        "texto":     _sim_texto(video, cand, meta),
        "duracao":   _sim_duracao(video.get("duracao_s"), meta and meta["duracao_s"]),
        "thumbnail": _sim_thumbnail(hash_ref, _dhash(meta["thumbnail"]) if meta and meta["thumbnail"] else None),
    }
    validos = {k: v for k, v in comp.items() if v is not None}
    peso_total = sum(PESOS[k] for k in validos)
    score = sum(PESOS[k] * v for k, v in validos.items()) / peso_total
    return {
        **{f"sim_{k}": (round(v, 3) if v is not None else None) for k, v in comp.items()},
        "score": round(score, 3),
        "componentes_usados": "+".join(validos),
    }


# ─── Interface pública ────────────────────────────────────────────────────────

def procurar_em_outras_plataformas(video: dict) -> list[dict]:
    """Retorna todos os candidatos pontuados, com a flag 'provavel_mesmo_video'."""
    hash_ref = _dhash(video["thumbnail"]) if video.get("thumbnail") else None
    resultados = []
    for cand in buscar_candidatos(video):
        meta = extrair_metadados(cand["url"])
        pont = pontuar(video, cand, meta, hash_ref)
        resultados.append({
            "yt_video_id":   video["video_id"],
            "yt_titulo":     video["titulo"],
            "yt_publicado_em": video.get("publicado_em"),
            **cand,
            "autor":         meta and meta["autor"],
            "titulo_plataforma": meta and meta["titulo"],
            "duracao_s":     meta and meta["duracao_s"],
            "views":         meta and meta["views"],
            "timestamp":     meta and meta["timestamp"],
            "metadados_ok":  meta is not None,
            **pont,
            # Texto parecido não basta: exige duração ou thumbnail compatível para confirmar
            "provavel_mesmo_video": pont["score"] >= LIMIAR_MATCH and max(
                pont["sim_duracao"] or 0, pont["sim_thumbnail"] or 0) >= LIMIAR_CONFIRMACAO,
        })
    resultados.sort(key=lambda r: r["score"], reverse=True)
    return resultados
