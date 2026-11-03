"""
Procura, em outras plataformas, CORTES (trechos curtos) dos vídeos virais do YouTube.

Reposts inteiros são reconhecidos por duração/thumbnail (main.py). Cortes não: são
trechos de poucos minutos de lives de horas. Aqui a comparação é pela FALA:

  1. Transcrição de cada vídeo viral via legenda automática do YouTube
  2. Candidatos: links já encontrados por main.py + novas buscas "<tema> corte"
  3. Mantém só vídeos curtos (≤ DURACAO_MAX_CORTE), baixa o áudio e transcreve com Whisper
  4. Compara n-gramas de palavras do corte com a transcrição da live, buscando a janela
     de tempo com mais n-gramas em comum → cobertura (0–1) e minuto do trecho na live

Uso (depois de rodar main.py):
  python cortes.py
  python cortes.py --sem-busca      # só reavalia os candidatos já encontrados
  python cortes.py --retomar        # pula busca e metadados: usa data/candidatos_cortes.csv

Saída: data/cortes.csv
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import timedelta

import pandas as pd

from busca_cruzada import _buscar_web, _palavras_chave, extrair_metadados
from config import (
    CANDIDATOS_CORTES_CSV,
    CORRESPONDENCIAS_CSV,
    CORTES_CSV,
    DURACAO_MAX_CORTE,
    LIMIAR_CORTE,
    MIN_NGRAMAS_CORTE,
    PADROES_URL_VIDEO,
    PAUSA_ENTRE_BUSCAS,
    PLATAFORMAS,
    PLATAFORMAS_CORTES,
    TAMANHO_NGRAMA,
    VIRAIS_CSV,
)
from transcricao import legenda_youtube, transcrever_corte

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ─── Comparação de transcrições ───────────────────────────────────────────────

def _palavras(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", texto)


def _ngramas_com_tempo(segmentos: list[dict]) -> list[tuple[tuple, float]]:
    """Cada n-grama recebe o tempo de início do segmento da sua primeira palavra."""
    palavras = [(p, s["inicio"]) for s in segmentos for p in _palavras(s["texto"])]
    n = TAMANHO_NGRAMA
    return [(tuple(p for p, _ in palavras[i:i + n]), palavras[i][1]) for i in range(len(palavras) - n + 1)]


def indexar(segmentos: list[dict]) -> dict[tuple, list[float]]:
    indice = defaultdict(list)
    for ng, t in _ngramas_com_tempo(segmentos):
        indice[ng].append(t)
    return indice


def comparar(corte: list[dict], indice_live: dict[tuple, list[float]], duracao_corte: float) -> dict:
    """
    Procura a janela da live (duração do corte + 60 s) com mais n-gramas distintos do corte.
    cobertura = n-gramas do corte encontrados nessa janela / n-gramas do corte.
    """
    ngramas = list({ng for ng, _ in _ngramas_com_tempo(corte)})
    if len(ngramas) < MIN_NGRAMAS_CORTE:
        return {"cobertura": None, "inicio_na_live": None, "ngramas_corte": len(ngramas)}

    eventos = sorted((t, i) for i, ng in enumerate(ngramas) for t in indice_live.get(ng, []))
    janela = (duracao_corte or 180) + 60
    contagem, distintos, melhor, inicio_melhor, esq = Counter(), 0, 0, None, 0
    for t, i in eventos:
        contagem[i] += 1
        distintos += contagem[i] == 1
        while eventos[esq][0] < t - janela:
            j = eventos[esq][1]
            contagem[j] -= 1
            distintos -= contagem[j] == 0
            esq += 1
        if distintos > melhor:
            melhor, inicio_melhor = distintos, eventos[esq][0]

    return {
        "cobertura":      round(melhor / len(ngramas), 3),
        "inicio_na_live": inicio_melhor,
        "ngramas_corte":  len(ngramas),
    }


# ─── Candidatos ───────────────────────────────────────────────────────────────

def _consultas_cortes(virais: pd.DataFrame) -> list[str]:
    consultas = []
    for titulo in virais["titulo"]:
        consultas += [_palavras_chave(titulo, 5), f"{_palavras_chave(titulo, 4)} corte"]
    return [c for c in dict.fromkeys(consultas) if c]


def buscar_candidatos(virais: pd.DataFrame) -> list[dict]:
    candidatos = []
    for consulta in _consultas_cortes(virais):
        for plataforma in PLATAFORMAS_CORTES:
            for r in _buscar_web(f"{consulta} {PLATAFORMAS[plataforma]}"):
                url = r.get("href", "")
                if "watch" not in url:
                    url = url.split("?")[0]
                if re.search(PADROES_URL_VIDEO[plataforma], url):
                    candidatos.append({"plataforma": plataforma, "url": url, "consulta": consulta})
            time.sleep(PAUSA_ENTRE_BUSCAS)
    logger.info("%d links encontrados nas novas buscas", len(candidatos))
    return candidatos


def _candidatos_anteriores() -> list[dict]:
    """Reaproveita os links (e metadados) que main.py já encontrou."""
    if not CORRESPONDENCIAS_CSV.exists():
        return []
    df = pd.read_csv(CORRESPONDENCIAS_CSV)
    df = df[df["plataforma"].isin(PLATAFORMAS_CORTES)]
    cols = ["plataforma", "url", "consulta", "autor", "titulo_plataforma", "duracao_s", "views", "timestamp", "metadados_ok"]
    return df[cols].to_dict("records")


def _completar_metadados(cand: dict) -> dict:
    if cand.get("metadados_ok"):
        return cand
    meta = extrair_metadados(cand["url"]) or {}
    cand.update({
        "autor":             meta.get("autor"),
        "titulo_plataforma": meta.get("titulo"),
        "duracao_s":         meta.get("duracao_s"),
        "views":             meta.get("views"),
        "timestamp":         meta.get("timestamp"),
        "metadados_ok":      bool(meta),
    })
    return cand


def _id_corte(url: str) -> str:
    return re.sub(r"\W+", "_", url.split("://", 1)[-1])[-80:]


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sem-busca", action="store_true", help="não faz novas buscas web")
    parser.add_argument("--retomar", action="store_true", help="reusa os candidatos curtos da última execução")
    args = parser.parse_args()

    virais = pd.read_csv(VIRAIS_CSV, parse_dates=["publicado_em"]).drop_duplicates("video_id")

    # 1. Transcrições das lives
    indices = {}
    for v in virais.itertuples():
        seg = legenda_youtube(v.video_id)
        if seg:
            indices[v.video_id] = indexar(seg)
    logger.info("%d de %d vídeos virais com transcrição", len(indices), len(virais))

    if args.retomar and CANDIDATOS_CORTES_CSV.exists():
        curtos = pd.read_csv(CANDIDATOS_CORTES_CSV).to_dict("records")
    else:
        # 2. Candidatos (sem duplicatas)
        candidatos = _candidatos_anteriores() + ([] if args.sem_busca else buscar_candidatos(virais))
        candidatos = list({c["url"]: c for c in candidatos}.values())
        logger.info("%d candidatos únicos", len(candidatos))

        # 3. Só vídeos curtos, publicados depois do vídeo viral mais antigo
        inicio_janela = virais["publicado_em"].min().timestamp() - 86400
        curtos = []
        for c in map(_completar_metadados, candidatos):
            dur, ts = c.get("duracao_s"), c.get("timestamp")
            if pd.notna(dur) and 0 < dur <= DURACAO_MAX_CORTE and (pd.isna(ts) or ts >= inicio_janela):
                curtos.append(c)
        pd.DataFrame(curtos).to_csv(CANDIDATOS_CORTES_CSV, index=False)
    logger.info("%d candidatos curtos (≤ %d s) para transcrever", len(curtos), DURACAO_MAX_CORTE)

    # 4. Transcreve e compara com todas as lives
    titulos = virais.set_index("video_id")["titulo"].to_dict()
    resultados = []
    for i, c in enumerate(curtos, 1):
        seg = transcrever_corte(c["plataforma"], c["url"], _id_corte(c["url"]))
        if not seg:
            continue
        melhor = max(
            ({"yt_video_id": vid, **comparar(seg, idx, c["duracao_s"])} for vid, idx in indices.items()),
            key=lambda r: r["cobertura"] or 0,
        )
        inicio = melhor["inicio_na_live"]
        eh_corte = (melhor["cobertura"] or 0) >= LIMIAR_CORTE
        resultados.append({
            **c,
            **melhor,
            "yt_titulo":       titulos[melhor["yt_video_id"]],
            "inicio_hhmmss":   str(timedelta(seconds=int(inicio))) if inicio is not None else None,
            "link_trecho_yt":  f"https://www.youtube.com/watch?v={melhor['yt_video_id']}&t={int(inicio)}s" if inicio is not None else None,
            "eh_corte":        eh_corte,
            "transcricao":     " ".join(s["texto"] for s in seg)[:500],
        })
        if eh_corte:
            logger.info("  ✔ [%d/%d] %-9s cobertura=%.2f em %s de '%s' — %s", i, len(curtos), c["plataforma"],
                        melhor["cobertura"], resultados[-1]["inicio_hhmmss"], titulos[melhor["yt_video_id"]][:40], c["url"])
        pd.DataFrame(resultados).sort_values("cobertura", ascending=False).to_csv(CORTES_CSV, index=False)

    df = pd.DataFrame(resultados)
    if len(df):
        logger.info("Cortes confirmados por plataforma:\n%s", df[df["eh_corte"]].groupby("plataforma").size().to_string())
    logger.info("Resultados em %s", CORTES_CSV)


if __name__ == "__main__":
    main()
