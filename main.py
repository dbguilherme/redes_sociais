"""
Pipeline: encontra vídeos virais no YouTube e procura o mesmo vídeo em outras plataformas.

Uso:
  python main.py                            # virais da semana (tema 'geral' do config.py)
  python main.py --tema politica --dias 15  # virais de política dos últimos 15 dias
  python main.py --termo "feminicídio"      # virais sobre um termo específico
  python main.py --termo "enchente" --periodo hoje --max 5
  python main.py --url https://www.youtube.com/watch?v=XXXX   # um vídeo específico

Saídas em data/:
  virais_youtube.csv     — vídeos virais e suas métricas
  correspondencias.csv   — candidatos encontrados em outras plataformas com score
"""

from __future__ import annotations

import argparse
import logging

import pandas as pd

# from busca_cruzada import procurar_em_outras_plataformas
from config import TEMAS, VIRAIS_CSV # CORRESPONDENCIAS_CSV
from youtube_virais import buscar_virais, detalhes_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ddgs").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--termo", help="termo de busca específico (sobrepõe --tema)")
    parser.add_argument("--tema", default="geral", choices=list(TEMAS), help="conjunto de termos do config.py")
    parser.add_argument("--periodo", default="semana", choices=["hora", "hoje", "semana", "mes", "ano"],
                        help="janela de publicação dos vídeos (padrão: semana)")
    parser.add_argument("--dias", type=int, help="só vídeos publicados nos últimos N dias (sobrepõe --periodo)")
    parser.add_argument("--url", help="analisar um vídeo do YouTube específico")
    parser.add_argument("--max", type=int, default=None, help="limite de vídeos processados")
    args = parser.parse_args()

    if args.url:
        virais = [detalhes_video(args.url)]
    else:
        virais = buscar_virais([args.termo] if args.termo else TEMAS[args.tema], args.periodo, args.dias)
    if args.max:
        virais = virais[:args.max]

    pd.DataFrame(virais).to_csv(VIRAIS_CSV, index=False)
    logger.info("Virais salvos em %s", VIRAIS_CSV)

    #todas = []
    #for i, video in enumerate(virais, 1):
    #    logger.info("[%d/%d] %s (%s views/h)", i, len(virais), video["titulo"][:70], video.get("views_por_hora"))
    #    resultados = procurar_em_outras_plataformas(video)
    #    todas += resultados
    #    for r in resultados:
    #        if r["provavel_mesmo_video"]:
    #            logger.info("    ✔ %-10s score=%.2f %s", r["plataforma"], r["score"], r["url"])
    #    # Salva a cada vídeo para não perder progresso
    #    pd.DataFrame(todas).to_csv(CORRESPONDENCIAS_CSV, index=False)

    #if todas:
    #    df = pd.DataFrame(todas)
    #    resumo = df[df["provavel_mesmo_video"]].groupby("plataforma")["yt_video_id"].nunique()
    #    logger.info("Vídeos encontrados por plataforma:\n%s", resumo.to_string() if len(resumo) else "(nenhum)")
    #logger.info("Correspondências salvas em %s", CORRESPONDENCIAS_CSV)


if __name__ == "__main__":
    main()
