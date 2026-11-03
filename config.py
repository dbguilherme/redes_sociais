"""
Configurações do projeto de busca de vídeos virais entre plataformas.

Chaves de API são lidas via variáveis de ambiente — nunca hardcoded.
"""

from __future__ import annotations

import os
from pathlib import Path

# ─── Diretórios ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TRANSCRICOES_DIR = DATA_DIR / "transcricoes"
TRANSCRICOES_DIR.mkdir(exist_ok=True)

VIRAIS_CSV      = DATA_DIR / "virais_youtube.csv"
CORRESPONDENCIAS_CSV = DATA_DIR / "correspondencias.csv"
CORTES_CSV      = DATA_DIR / "cortes.csv"
CANDIDATOS_CORTES_CSV = DATA_DIR / "candidatos_cortes.csv"

# ─── YouTube (sem chave de API — tudo via yt-dlp) ─────────────────────────────
MAX_VIRAIS           = 20     # quantos vídeos virais processar por execução
RESULTADOS_POR_TERMO = 40     # vídeos listados por busca

# Conjuntos de termos de busca selecionáveis com --tema
TEMAS: dict[str, list[str]] = {
    # aproximação do "Em alta" do Brasil
    "geral": [
        "brasil",
        "notícias",
        "urgente",
        "flagrante",
        "#shorts",
    ],
    "politica": [
        "política",
        "Lula",
        "Bolsonaro",
        "eleições 2026",
        "presidente",
        "Congresso Nacional",
        "STF",
        "deputado",
        "senador",
        "governo federal",
    ],
}

# Critérios mínimos para considerar um vídeo "viral"
MIN_VIEWS          = 100_000
MIN_VIEWS_POR_HORA = 1_000    # velocidade de crescimento (views / horas desde a publicação)

# ─── Plataformas de destino ───────────────────────────────────────────────────
# nome → filtro "site:" usado na busca web
PLATAFORMAS: dict[str, str] = {
    "tiktok":     "site:tiktok.com",
    "instagram":  "site:instagram.com/reel",
    "facebook":   "site:facebook.com",
    "x":          "site:x.com OR site:twitter.com",
    "kwai":       "site:kwai.com",
    "dailymotion": "site:dailymotion.com",
}
RESULTADOS_POR_PLATAFORMA = 8

# Só URLs que apontam para um vídeo (descarta perfis, hashtags, grupos...)
PADROES_URL_VIDEO: dict[str, str] = {
    "tiktok":      r"tiktok\.com/@[^/]+/video/\d+",
    "instagram":   r"instagram\.com/(reel|reels|p|tv)/[\w-]+",
    "facebook":    r"facebook\.com/(.+/videos/|watch/?\?v=|reel/|share/[rv]/)",
    "x":           r"(x|twitter)\.com/[^/]+/status/\d+",
    "kwai":        r"kwai\.com/.*(video|photo)",
    "dailymotion": r"dailymotion\.com/video/\w+",
}

# ─── Verificação ──────────────────────────────────────────────────────────────
# Para Instagram/Facebook o yt-dlp normalmente precisa de login.
# Ex.: export YTDLP_COOKIES_BROWSER=firefox
YTDLP_COOKIES_BROWSER = os.getenv("YTDLP_COOKIES_BROWSER", "")

PESOS = {"texto": 0.4, "duracao": 0.3, "thumbnail": 0.3}
LIMIAR_MATCH = 0.65           # score combinado mínimo para considerar o mesmo vídeo
LIMIAR_CONFIRMACAO = 0.8      # duração ou thumbnail precisa atingir isso (texto sozinho não confirma)

PAUSA_ENTRE_BUSCAS = 2.0      # segundos — evita bloqueio do buscador

# ─── Cortes (cortes.py) ───────────────────────────────────────────────────────
# Kwai fica de fora: o yt-dlp não consegue baixar vídeos de lá
PLATAFORMAS_CORTES = ["tiktok", "instagram", "facebook", "x", "dailymotion"]
DURACAO_MAX_CORTE  = 900      # segundos — acima disso não é tratado como corte

WHISPER_MODELO     = os.getenv("WHISPER_MODELO", "medium")
WHISPER_DISPOSITIVO = os.getenv("WHISPER_DISPOSITIVO", "cuda")

TAMANHO_NGRAMA     = 4        # n-gramas de palavras comparados entre corte e live
MIN_NGRAMAS_CORTE  = 15       # cortes com menos fala que isso não são avaliados
LIMIAR_CORTE       = 0.3      # fração dos n-gramas do corte presentes num trecho da live
