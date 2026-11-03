"""
Confirma reuploads e cortes dos vídeos virais comparando ÁUDIO e QUADROS
(impressao_av.py), em vez de título/duração/miniatura (busca_cruzada.py).

Candidatos, de todas as fontes juntas (sem duplicatas):
  - data/correspondencias.csv e data/cortes.csv (links achados por main.py / cortes.py)
  - fontes_extra.py: YouTube, Dailymotion, hashtags do Instagram
  - --urls / --urls-arquivo: links informados à mão (TikTok, X, Facebook...)

Uso:
  python verificacao_av.py                          # virais de data/virais_youtube.csv
  python verificacao_av.py --max-virais 3
  python verificacao_av.py --url https://www.youtube.com/watch?v=XXXX
  python verificacao_av.py --url URL --urls https://www.tiktok.com/@x/video/123
  python verificacao_av.py --fontes youtube dailymotion   # sem Instagram
  python verificacao_av.py --sem-fontes-extra             # só candidatos já encontrados

Saída: data/reuploads_av.csv — um candidato por linha, com cobertura de áudio
e de quadros (0–1), trecho do original que ele usa e veredito:
  reupload | reupload_audio | reupload_imagem | contem_trecho | diferente

Impressões ficam em cache em data/impressoes/ (o vídeo é apagado após a extração).
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import fontes_extra
import impressao_av as av
from config import CORRESPONDENCIAS_CSV, CORTES_CSV, DATA_DIR, VIRAIS_CSV, YTDLP_COOKIES_BROWSER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ddgs").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SAIDA_CSV     = DATA_DIR / "reuploads_av.csv"
IMPRESSOES    = DATA_DIR / "impressoes"
MAX_DURACAO_CANDIDATO = 30 * 60      # s — acima disso não é tratado como reupload/corte
PAUSA_DOWNLOAD = 2.0


def _chave(plataforma: str, url: str) -> str:
    limpo = re.sub(r"\W+", "_", url.split("://", 1)[-1])[-90:]
    return f"{plataforma}_{limpo}"


def _plataforma(url: str) -> str:
    for nome, padrao in [("youtube", r"youtube\.com|youtu\.be"), ("tiktok", "tiktok"), ("instagram", "instagram"),
                         ("facebook", r"facebook|fb\.watch"), ("x", r"//(www\.)?(x|twitter)\.com"),
                         ("dailymotion", "dailymotion"), ("kwai", "kwai")]:
        if re.search(padrao, url):
            return nome
    return "outra"


def _candidatos_salvos(video_id: str) -> list[dict]:
    """Links que main.py/cortes.py já encontraram para este vídeo."""
    out = []
    if CORRESPONDENCIAS_CSV.exists():
        df = pd.read_csv(CORRESPONDENCIAS_CSV)
        df = df[df["yt_video_id"] == video_id]
        out += [{"plataforma": r.plataforma, "url": r.url, "consulta": f"busca_cruzada: {r.consulta}"}
                for r in df.itertuples()]
    if CORTES_CSV.exists():
        df = pd.read_csv(CORTES_CSV)
        if "yt_video_id" in df:
            df = df[df["yt_video_id"] == video_id]
            out += [{"plataforma": r.plataforma, "url": r.url, "consulta": "cortes.py"} for r in df.itertuples()]
    return out


def verificar_video(video: dict, candidatos: list[dict], max_candidatos: int) -> list[dict]:
    url_o = video.get("url") or f"https://www.youtube.com/watch?v={video['video_id']}"
    logger.info("Original: %s", video["titulo"][:80])
    t = time.time()
    orig = av.impressao(url_o, f"orig_youtube_{video['video_id']}", IMPRESSOES, variacoes=False,
                        cookies_browser=YTDLP_COOKIES_BROWSER)
    if not orig:
        logger.warning("  não foi possível processar o original — pulando")
        return []
    logger.info("  impressão do original: %d hashes de áudio, %d quadros (%.0fs)",
                len(orig["ah"]), len(orig["quadros"][0][0]), time.time() - t)

    candidatos = list({c["url"]: c for c in candidatos}.values())[:max_candidatos]
    logger.info("  %d candidatos", len(candidatos))
    resultados = []
    for i, c in enumerate(candidatos, 1):
        cand = av.impressao(c["url"], _chave(c["plataforma"], c["url"]), IMPRESSOES, variacoes=True,
                            cookies_browser=YTDLP_COOKIES_BROWSER, max_duracao=MAX_DURACAO_CANDIDATO)
        if not cand:
            resultados.append({**_linha_base(video, c), "status": "nao_baixado"})
            continue
        if cand["meta"].get("id") == video["video_id"]:
            continue
        m = av.comparar(orig, cand)
        meta = cand["meta"]
        ts = meta.get("timestamp")
        resultados.append({
            **_linha_base(video, c),
            "status":          "ok",
            "autor":           meta.get("autor"),
            "titulo_candidato": meta.get("titulo"),
            "publicado_em":    datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            "duracao_s":       meta.get("duracao_s"),
            "views":           meta.get("views"),
            "likes":           meta.get("likes"),
            "comentarios":     meta.get("comentarios"),
            **m,
        })
        if m["veredito"] != "diferente":
            logger.info("  ✔ [%d/%d] %-11s %-15s áudio=%.2f quadros=%.2f %s", i, len(candidatos), c["plataforma"],
                        m["veredito"], m["audio_cobertura"], m["video_cobertura"], c["url"])
        time.sleep(PAUSA_DOWNLOAD)
    return resultados


def _linha_base(video: dict, c: dict) -> dict:
    return {"yt_video_id": video["video_id"], "yt_titulo": video["titulo"],
            "plataforma": c["plataforma"], "url": c["url"], "fonte": c.get("consulta")}


def _salvar(linhas: list[dict]) -> None:
    """Substitui as linhas dos vídeos reprocessados e mantém as demais."""
    novo = pd.DataFrame(linhas)
    if SAIDA_CSV.exists() and len(novo):
        antigo = pd.read_csv(SAIDA_CSV)
        antigo = antigo[~antigo["yt_video_id"].isin(novo["yt_video_id"].unique())]
        novo = pd.concat([antigo, novo], ignore_index=True)
    novo.to_csv(SAIDA_CSV, index=False)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="vídeo do YouTube específico (em vez de virais_youtube.csv)")
    p.add_argument("--max-virais", type=int, default=3)
    p.add_argument("--max-candidatos", type=int, default=200, help="por vídeo")
    p.add_argument("--fontes", nargs="*", default=list(fontes_extra.FONTES), choices=list(fontes_extra.FONTES))
    p.add_argument("--sem-fontes-extra", action="store_true")
    p.add_argument("--urls", nargs="*", default=[], help="candidatos informados à mão")
    p.add_argument("--urls-arquivo", type=Path, help="arquivo com um link por linha")
    a = p.parse_args()
    IMPRESSOES.mkdir(exist_ok=True)

    if a.url:
        from youtube_virais import detalhes_video
        virais = [detalhes_video(a.url)]
    else:
        virais = pd.read_csv(VIRAIS_CSV).drop_duplicates("video_id").head(a.max_virais).to_dict("records")

    manuais = list(a.urls)
    if a.urls_arquivo:
        manuais += [l.strip() for l in a.urls_arquivo.read_text().splitlines() if l.strip() and not l.startswith("#")]

    todas = []
    for n, video in enumerate(virais, 1):
        logger.info("═══ [%d/%d] %s", n, len(virais), video["titulo"][:80])
        cands = [{"plataforma": _plataforma(u), "url": u, "consulta": "manual"} for u in manuais]
        cands += _candidatos_salvos(video["video_id"])
        if not a.sem_fontes_extra:
            cands += fontes_extra.buscar(video, a.fontes)
        todas += verificar_video(video, cands, a.max_candidatos)
        _salvar(todas)

    df = pd.DataFrame(todas)
    if len(df) and "veredito" in df:
        achados = df[df["veredito"].notna() & (df["veredito"] != "diferente")]
        logger.info("Resumo por plataforma (reuploads/cortes confirmados):\n%s",
                    achados.groupby(["plataforma", "veredito"]).size().to_string() if len(achados) else "(nenhum)")
        logger.info("Não baixados: %s", df[df["status"] == "nao_baixado"].groupby("plataforma").size().to_dict())
    logger.info("Resultados em %s", SAIDA_CSV)


if __name__ == "__main__":
    main()
