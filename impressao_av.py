"""
Impressões digitais de ÁUDIO e QUADROS para reconhecer o mesmo vídeo entre
plataformas, mesmo com título, miniatura, formato ou duração diferentes.

  Áudio  : constelação de picos do espectrograma (estilo Shazam). Resiste a
           reencode, volume, música de fundo leve, cortes e aceleração ~1,1x.
  Quadros: pHash de 1 quadro/s do recorte central. No candidato são testadas
           variações (proporção nativa, 9:16, 16:9, espelhado) para cobrir
           vertical-dentro-de-horizontal, tarjas e espelhamento.

Em ambos, os acertos precisam concordar num mesmo deslocamento de tempo
(votação por offset), o que elimina coincidências soltas (vinhetas, estúdio).

Original longo (lives de horas) é processado em blocos e indexado em numpy.
Cada vídeo é baixado em baixa resolução, vira um .npz em cache e é apagado.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
import yt_dlp
from scipy.fft import dct
from scipy.ndimage import maximum_filter
from scipy.signal import stft

logger = logging.getLogger(__name__)

try:
    from imageio_ffmpeg import get_ffmpeg_exe
    FFMPEG = shutil.which("ffmpeg") or get_ffmpeg_exe()
except ImportError:
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# ─── Parâmetros (validados com reuploads sintéticos e controles negativos) ────
SR, N_FFT, HOP = 8000, 1024, 256   # 8 kHz, quadro de ~32 ms
SEG_POR_QUADRO = HOP / SR
BLOCO_S        = 600               # áudio lido em blocos de 10 min
PEAK_NEIGH     = (15, 15)
FAN_OUT, MAX_DT = 5, 40
MAX_OCORRENCIAS = 300              # hashes muito frequentes no original são ignorados
MIN_VOTOS_AUDIO = 8
CROP           = 0.6               # fração central mantida nos quadros
HAMMING_MAX    = 10
MIN_VOTOS_QUADRO = 3
VARIACOES      = [(asp, flip) for asp in (None, 9 / 16, 16 / 9) for flip in (False, True)]


# ─── Download ─────────────────────────────────────────────────────────────────

class _Silencioso:
    def debug(self, msg): logger.debug(msg)
    info = warning = error = debug


def baixar(url: str, pasta: Path, cookies_browser: str = "", max_duracao: Optional[int] = None) -> Optional[dict]:
    """Baixa em baixa resolução, com a faixa de áudio ORIGINAL (o YouTube cria
    dublagens automáticas en/es que quebrariam a impressão de áudio)."""
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": _Silencioso(),
        "format": "bv*+ba[format_note*=original]/bv*+ba/b",
        "format_sort": ["res:240"],
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "outtmpl": str(pasta / "%(extractor_key)s_%(id)s.%(ext)s"),
        "extractor_args": {"youtube": {"lang": ["pt"]}},
    }
    if max_duracao:
        # "<?" aceita duração ausente (Reels do Instagram não informam)
        opts["match_filter"] = yt_dlp.utils.match_filter_func(f"duration <? {max_duracao}")
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
    except Exception as exc:
        logger.info("    download falhou: %s", str(exc)[:120])
        return None
    if not info or not info.get("requested_downloads"):
        return None
    return {
        "url_final": info.get("webpage_url") or url,
        "plataforma_ytdlp": info.get("extractor_key"),
        "id": info.get("id"),
        "titulo": info.get("title"),
        "autor": info.get("channel") or info.get("uploader"),
        "timestamp": info.get("timestamp"),
        "duracao_s": info.get("duration"),
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comentarios": info.get("comment_count"),
        "arquivo": info["requested_downloads"][0]["filepath"],
    }


# ─── Áudio ────────────────────────────────────────────────────────────────────

def _hashes_bloco(sinal: np.ndarray, t0: int) -> tuple[np.ndarray, np.ndarray]:
    _, _, Z = stft(sinal, fs=SR, nperseg=N_FFT, noverlap=N_FFT - HOP)
    S = np.log1p(np.abs(Z))
    picos = (S == maximum_filter(S, size=PEAK_NEIGH)) & (S > S.mean() + S.std())
    f, t = np.nonzero(picos)
    ordem = np.argsort(t, kind="stable")
    f, t = f[ordem], t[ordem]
    hs, ts = [], []
    for k in range(1, FAN_OUT + 1):
        dt = t[k:] - t[:-k]
        ok = (dt > 0) & (dt <= MAX_DT)
        hs.append((f[:-k][ok].astype(np.uint32) << 16) | (f[k:][ok].astype(np.uint32) << 6) | dt[ok].astype(np.uint32))
        ts.append(t[:-k][ok] + t0)
    return np.concatenate(hs), np.concatenate(ts).astype(np.int32)


def impressao_audio(arquivo: str) -> tuple[np.ndarray, np.ndarray]:
    """Lê o áudio em blocos (lives de horas cabem na memória). → (hashes, tempos)."""
    cmd = [FFMPEG, "-v", "error", "-i", arquivo, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    hs, ts, t0 = [], [], 0
    bytes_bloco = BLOCO_S * SR * 2
    while True:
        raw = proc.stdout.read(bytes_bloco)
        if not raw:
            break
        sinal = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
        if len(sinal) >= N_FFT:
            h, t = _hashes_bloco(sinal, t0)
            hs.append(h); ts.append(t)
        t0 += len(sinal) // HOP
    proc.wait()
    if not hs:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    h, t = np.concatenate(hs), np.concatenate(ts)
    ordem = np.argsort(h, kind="stable")
    return h[ordem], t[ordem]


# ─── Quadros ──────────────────────────────────────────────────────────────────

def _quadros(arquivo: str, aspecto: Optional[float], espelhar: bool) -> np.ndarray:
    m = (1 - CROP) / 2
    pre = f"crop='min(iw,ih*{aspecto})':'min(ih,iw/{aspecto})'," if aspecto else ""
    pre += "hflip," if espelhar else ""
    vf = f"fps=1,{pre}crop=iw*{CROP}:ih*{CROP}:iw*{m}:ih*{m},scale=32:32,format=gray"
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", arquivo, "-an", "-vf", vf, "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 32, 32).astype(np.float32)


def _phash(q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not len(q):
        return np.zeros(0, np.uint64), np.zeros(0, bool)
    d = dct(dct(q, axis=1, norm="ortho"), axis=2, norm="ortho")[:, :8, :8].reshape(len(q), -1)
    bits = d > np.median(d[:, 1:], axis=1, keepdims=True)
    informativo = q.reshape(len(q), -1).std(axis=1) > 12      # descarta quadros lisos/pretos
    return np.packbits(bits, axis=1).view(">u8").ravel().astype(np.uint64), informativo


def impressao_quadros(arquivo: str, variacoes: bool) -> list[tuple[np.ndarray, np.ndarray]]:
    vs = VARIACOES if variacoes else [(None, False)]
    return [_phash(_quadros(arquivo, asp, flip)) for asp, flip in vs]


# ─── Cache ────────────────────────────────────────────────────────────────────

def impressao(url: str, chave: str, cache_dir: Path, *, variacoes: bool,
              cookies_browser: str = "", max_duracao: Optional[int] = None) -> Optional[dict]:
    """Baixa → extrai impressões → apaga o vídeo. Resultado em cache (.npz)."""
    arq = cache_dir / f"{chave}.npz"
    if arq.exists():
        z = np.load(arq, allow_pickle=True)
        return {"meta": z["meta"].item(), "ah": z["ah"], "at": z["at"],
                "quadros": [(z[f"ph{i}"], z[f"inf{i}"]) for i in range(int(z["nvar"]))]}

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        meta = baixar(url, Path(tmp), cookies_browser, max_duracao)
        if not meta:
            return None
        try:
            ah, at = impressao_audio(meta["arquivo"])
            quadros = impressao_quadros(meta["arquivo"], variacoes)
        except subprocess.CalledProcessError as exc:
            logger.info("    ffmpeg falhou: %s", exc)
            return None
    meta.pop("arquivo")
    extra = {f"ph{i}": ph for i, (ph, _) in enumerate(quadros)} | {f"inf{i}": inf for i, (_, inf) in enumerate(quadros)}
    np.savez_compressed(arq, meta=np.array(meta, dtype=object), ah=ah, at=at, nvar=len(quadros), **extra)
    return {"meta": meta, "ah": ah, "at": at, "quadros": quadros}


# ─── Comparação ───────────────────────────────────────────────────────────────

def _offsets_validos(tc: np.ndarray, to: np.ndarray, min_votos: int, tol: int = 1):
    """Votação por offset (to - tc). Devolve máscara dos pares em offsets
    significativos e o offset dominante."""
    if not len(tc):
        return np.zeros(0, bool), None
    off = to.astype(np.int64) - tc
    valores, contagem = np.unique(off, return_counts=True)
    janela = np.zeros_like(contagem)
    for k in range(-tol, tol + 1):         # soma vizinhos ±tol
        idx = np.searchsorted(valores, valores + k)
        ok = (idx < len(valores)) & (valores[np.minimum(idx, len(valores) - 1)] == valores + k)
        janela[ok] += contagem[idx[ok]]
    bons = valores[janela >= min_votos]
    if not len(bons):
        return np.zeros(len(off), bool), None
    dominante = valores[np.argmax(janela)]
    return np.isin(off, bons), dominante


def _trecho(tempos: np.ndarray, escala: float) -> Optional[str]:
    if not len(tempos):
        return None
    a, b = np.percentile(tempos, [5, 95]) * escala
    return f"{int(a // 3600)}:{int(a % 3600 // 60):02d}:{int(a % 60):02d}-{int(b // 3600)}:{int(b % 3600 // 60):02d}:{int(b % 60):02d}"


def comparar_audio(orig: dict, cand: dict, dur_cand: float) -> dict:
    oh, ot, ch, ct = orig["ah"], orig["at"], cand["ah"], cand["at"]
    lo, hi = np.searchsorted(oh, ch, "left"), np.searchsorted(oh, ch, "right")
    n = hi - lo
    ok = (n > 0) & (n <= MAX_OCORRENCIAS)
    lo, n, ct = lo[ok], n[ok], ct[ok]
    if not n.sum():
        return {"audio_cobertura": 0.0, "audio_trecho_original": None}
    rep_ct = np.repeat(ct, n)
    idx = np.repeat(lo - np.concatenate([[0], np.cumsum(n)[:-1]]), n) + np.arange(n.sum())
    rep_ot = ot[idx]
    mask, dom = _offsets_validos(rep_ct, rep_ot, MIN_VOTOS_AUDIO)
    segundos = np.unique((rep_ct[mask] * SEG_POR_QUADRO).astype(int))
    no_dom = rep_ot[np.abs((rep_ot.astype(np.int64) - rep_ct) - dom) <= 1] if dom is not None else np.zeros(0)
    return {"audio_cobertura": round(min(len(segundos) / max(dur_cand, 1), 1.0), 3),
            "audio_trecho_original": _trecho(no_dom, SEG_POR_QUADRO)}


def _comparar_quadros(ph_o, inf_o, ph_c, inf_c) -> dict:
    io, ic = np.nonzero(inf_o)[0], np.nonzero(inf_c)[0]
    vazio = {"video_cobertura": 0.0, "video_trecho_original": None}
    if not len(io) or not len(ic):
        return vazio
    tc, to = [], []
    for ini in range(0, len(io), 20000):   # em blocos: lives têm dezenas de milhares de quadros
        bo = io[ini:ini + 20000]
        x = ph_c[ic][:, None] ^ ph_o[bo][None, :]
        dist = np.unpackbits(x.view(np.uint8).reshape(len(ic), len(bo), 8), axis=2).sum(axis=2)
        a, b = np.nonzero(dist <= HAMMING_MAX)
        tc.append(ic[a]); to.append(bo[b])
    tc, to = np.concatenate(tc), np.concatenate(to)
    mask, dom = _offsets_validos(tc, to, MIN_VOTOS_QUADRO)
    if dom is None:
        return vazio
    return {"video_cobertura": round(len(np.unique(tc[mask])) / max(len(ph_c), 1), 3),
            "video_trecho_original": _trecho(to[np.abs((to.astype(np.int64) - tc) - dom) <= 1], 1.0)}


def comparar(orig: dict, cand: dict) -> dict:
    dur = cand["meta"].get("duracao_s") or len(cand["quadros"][0][0]) or 1
    ph_o, inf_o = orig["quadros"][0]
    quadros = max((_comparar_quadros(ph_o, inf_o, ph, inf) for ph, inf in cand["quadros"]),
                  key=lambda m: m["video_cobertura"])
    m = {**comparar_audio(orig, cand, dur), **quadros}
    m["veredito"] = veredito(m["audio_cobertura"], m["video_cobertura"])
    return m


def veredito(a: float, v: float) -> str:
    if a >= 0.5 and v >= 0.5:
        return "reupload"                   # mesmo áudio e imagem
    if a >= 0.5:
        return "reupload_audio"             # mesmo áudio, imagem trocada/coberta
    if v >= 0.5:
        return "reupload_imagem"            # mesma imagem, áudio trocado (dublagem, música)
    if a >= 0.15 or v >= 0.15:
        return "contem_trecho"              # reportagem/react que usa parte do original
    return "diferente"
