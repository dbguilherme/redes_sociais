"""
Fontes de candidatos que não dependem do buscador web (DuckDuckGo bloqueia
após algumas dezenas de consultas). Complementam busca_cruzada.py.

  youtube      — busca do YouTube (reuploads e cortes em outros canais)
  dailymotion  — API pública, sem chave
  instagram    — páginas de hashtag (Reels), renderizadas com Playwright

Política de bloqueio: se a plataforma responder com CAPTCHA, tela de login
ou erro de limite, a fonte para e registra no log. Não há rotação de proxy
nem disfarce de navegador.

TikTok, X e Kwai ficam de fora: busca e hashtags exigem login ou caem em
CAPTCHA. Links dessas redes encontrados por busca_cruzada.py (ou informados
à mão) continuam sendo verificados normalmente.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from urllib.parse import quote

import requests
import yt_dlp

logger = logging.getLogger(__name__)

PAUSA = 3.0
_STOP = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no", "na", "nos", "nas",
         "um", "uma", "com", "por", "para", "pra", "que", "se", "ao", "vivo", "shorts", "veja",
         "apos", "após", "diz", "urgente", "acompanhe", "especial", "live"}


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def palavras_chave(titulo: str, n: int = 6) -> str:
    t = re.sub(r"#\w+|[\[\(\{].*?[\]\)\}]|\|.*$", " ", titulo)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join([p for p in t.split() if p.lower() not in _STOP and not p.isdigit()][:n])


def consultas(video: dict) -> list[str]:
    base = palavras_chave(video["titulo"])
    curta = palavras_chave(video["titulo"], 4)
    return [c for c in dict.fromkeys([base, curta, f"{curta} corte"]) if c]


def hashtags(video: dict, n: int = 5) -> list[str]:
    """
    Hashtags para as páginas do Instagram, normalizadas (sem acento/espaço):
    nomes próprios do título (ex.: "Flávio Bolsonaro" → flaviobolsonaro) e
    hashtags/tags do vídeo. Tags com o nome do canal (#bandjornalismo, #cnn)
    são descartadas: só trazem posts do próprio veículo.
    """
    norm = lambda s: re.sub(r"\W", "", _sem_acento(s).lower())
    canal = {norm(p) for p in str(video.get("canal") or "").split() if len(p) >= 3}

    # Pontuação mantida: "Lula: Impossível" não deve virar um nome só
    titulo = re.sub(r"#\w+", " ", video["titulo"])
    nomes = re.findall(r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+(?: [A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç]+)?)", titulo)
    brutas = [n_ for n_ in nomes if not n_.isupper() and n_.lower() not in _STOP]
    descricao = video.get("descricao") if isinstance(video.get("descricao"), str) else ""
    tags = video.get("tags") if isinstance(video.get("tags"), str) else ""
    brutas += re.findall(r"#(\w+)", f'{video["titulo"]} {descricao[:500]}')
    brutas += [t for t in tags.split("|") if t]

    vistas = []
    for h in map(norm, brutas):
        if (3 <= len(h) <= 30 and h not in ("shorts", "short", "aovivo", "live")
                and not any(c in h for c in canal) and h not in vistas):
            vistas.append(h)
    return vistas[:n]


# ─── YouTube ──────────────────────────────────────────────────────────────────

def youtube(video: dict, por_consulta: int = 20) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "extractor_args": {"youtube": {"lang": ["pt"]}}}
    out = []
    for q in consultas(video):
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                res = y.extract_info(f"ytsearch{por_consulta}:{q}", download=False) or {}
        except Exception as exc:
            logger.warning("YouTube: busca falhou (%s) — parando a fonte", str(exc)[:100])
            break
        for e in res.get("entries") or []:
            if e and e.get("id") and e["id"] != video.get("video_id"):
                out.append({"plataforma": "youtube", "url": f"https://www.youtube.com/watch?v={e['id']}",
                            "consulta": q})
        time.sleep(PAUSA)
    return out


# ─── Dailymotion ──────────────────────────────────────────────────────────────

def dailymotion(video: dict, por_consulta: int = 20) -> list[dict]:
    out = []
    for q in consultas(video):
        try:
            r = requests.get("https://api.dailymotion.com/videos", timeout=20, params={
                "search": q, "fields": "url", "limit": por_consulta, "sort": "relevance"})
            if r.status_code == 429:
                logger.warning("Dailymotion: limite de requisições — parando a fonte")
                break
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Dailymotion: %s", exc)
            break
        out += [{"plataforma": "dailymotion", "url": v["url"], "consulta": q} for v in r.json().get("list", [])]
        time.sleep(PAUSA)
    return out


# ─── Instagram (hashtags) ─────────────────────────────────────────────────────

_SINAIS_BLOQUEIO = ("captcha", "challenge", "suspicious", "tente novamente mais tarde",
                    "try again later", "page isn't available", "página não está disponível")


def instagram(video: dict, rolagens: int = 4) -> list[dict]:
    """Reels listados nas páginas de hashtag. A página mostra os posts 'em alta',
    então várias hashtags ligadas ao fato aumentam a chance de achar reuploads."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Instagram: playwright não instalado (pip install playwright==1.49.1 && "
                       "playwright install chromium) — fonte ignorada")
        return []

    out = []
    with sync_playwright() as p:
        try:
            nav = p.chromium.launch(headless=True)
        except Exception as exc:
            logger.warning("Instagram: navegador não iniciou (%s) — fonte ignorada", str(exc).splitlines()[0][:120])
            return []
        pg = nav.new_context(locale="pt-BR").new_page()
        for tag in hashtags(video):
            try:
                pg.goto(f"https://www.instagram.com/explore/tags/{quote(tag)}/", timeout=45000,
                        wait_until="domcontentloaded")
                pg.wait_for_timeout(6000)
                for _ in range(rolagens):
                    pg.mouse.wheel(0, 3000)
                    pg.wait_for_timeout(1500)
                html = pg.content()
            except Exception as exc:
                logger.warning("Instagram #%s: %s", tag, str(exc)[:100])
                continue
            reels = dict.fromkeys(re.findall(r'/(?:reel|p)/([\w-]{8,})/', html))
            if not reels and any(s in pg.inner_text("body").lower() for s in _SINAIS_BLOQUEIO):
                logger.warning("Instagram: bloqueio detectado em #%s — parando a fonte", tag)
                break
            logger.info("  Instagram #%s: %d posts", tag, len(reels))
            out += [{"plataforma": "instagram", "url": f"https://www.instagram.com/reel/{r}/",
                     "consulta": f"#{tag}"} for r in reels]
            time.sleep(PAUSA)
        nav.close()
    return out


FONTES = {"youtube": youtube, "dailymotion": dailymotion, "instagram": instagram}


def buscar(video: dict, fontes: list[str]) -> list[dict]:
    out = []
    for nome in fontes:
        try:
            achados = FONTES[nome](video)
        except Exception as exc:     # uma fonte quebrada não derruba as outras
            logger.warning("  fonte %s falhou: %s", nome, str(exc)[:150])
            continue
        logger.info("  fonte %-11s %d candidatos", nome, len(achados))
        out += achados
    return out
