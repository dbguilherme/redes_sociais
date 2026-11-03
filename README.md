# Redes sociais: vídeos virais entre plataformas

Rastreia como um vídeo viral do YouTube se espalha por outras redes (TikTok,
Instagram, Facebook, X, Kwai, Dailymotion), seja como **repost inteiro**, seja
como **corte** (trecho curto de uma live longa). O objetivo de fundo é medir
**viés por rede social**: o mesmo conteúdo, do mesmo perfil, alcança públicos
diferentes em cada plataforma, e isso muda conforme o lado político?

O caso de estudo é o **2º turno das eleições presidenciais de 2026**
(Lula × Flávio Bolsonaro, 25/10/2026).

Nenhum script usa chave de API: a coleta é feita com `yt-dlp`, busca web
(DuckDuckGo via `ddgs`) e APIs públicas.

---

## Pipeline

```
youtube_virais.py ──► virais_youtube.csv
        │
        ▼
busca_cruzada.py (main.py) ──► correspondencias.csv      Experimento 1: metadados
        │
        ▼
cortes.py + transcricao.py ──► candidatos_cortes.csv     Experimento 2: fala (n-gramas)
                               cortes.csv
        │
        ▼
verificacao_av.py + impressao_av.py + fontes_extra.py    Experimento 3: áudio e quadros
                               ──► reuploads_av.csv
```

| Arquivo | Função |
|---|---|
| `config.py` | Parâmetros, limiares, temas de busca e caminhos |
| `youtube_virais.py` | Encontra vídeos virais no YouTube |
| `main.py` | Experimento 1: orquestra a coleta e a busca cruzada |
| `busca_cruzada.py` | Procura o vídeo em outras redes e pontua por metadados |
| `cortes.py` | Experimento 2: detecta cortes comparando transcrições |
| `transcricao.py` | Legendas do YouTube + Whisper para os cortes (com cache) |
| `fontes_extra.py` | Fontes de candidatos sem buscador web (YouTube, Dailymotion, hashtags do Instagram) |
| `impressao_av.py` | Impressões digitais de áudio (estilo Shazam) e de quadros (pHash) |
| `verificacao_av.py` | Experimento 3: confirma reuploads/cortes por áudio e imagem |
| `coleta_vies_redes.xlsx` | Planilha da coleta manual do estudo de viés (Experimento 4) |

---

## Etapa 0: seleção dos vídeos virais (`youtube_virais.py`)

1. Busca no YouTube com os filtros "data de envio" e "ordenar por
   visualizações" (parâmetro `sp` montado à mão), por termo do tema escolhido
   (`geral` ou `politica` em `config.py`).
2. Extrai os metadados completos só dos mais vistos.
3. Calcula `views_por_hora = views / horas desde a publicação` e
   `engajamento = (likes + comentários) / views`.
4. Critério de viral: `views ≥ 100.000` **e** `views_por_hora ≥ 1.000`.
   Mantém os 20 com maior `views_por_hora` (`MAX_VIRAIS`).

**Coleta realizada** (tema `politica`, semana do 2º turno):
20 vídeos publicados entre 19/10 e 26/10/2026, de 3,2 M a 33 M de views
(mediana 4,9 M). Quase todos são **lives longas** de veículos de imprensa
(Band, CNN Brasil, UOL, Jovem Pan, SBT News…), com duração mediana de ~6 h:
apuração, debate da Band, cobertura do dia da votação.

---

## Experimento 1: busca cruzada por metadados (`main.py`, `busca_cruzada.py`)

**Hipótese:** um repost inteiro tem título, duração e miniatura parecidos com
os do original.

**Método**
- Consultas geradas a partir do título (palavras-chave sem stopwords), do
  canal e das hashtags.
- Busca web com `site:` por plataforma (8 resultados por consulta); só URLs que
  apontam para um vídeo (regex em `PADROES_URL_VIDEO`).
- Metadados de cada candidato via `yt-dlp`.
- Score ponderado (componentes ausentes são ignorados e os pesos renormalizados):

  | Componente | Peso | Medida |
  |---|---|---|
  | texto | 0,4 | `rapidfuzz.token_set_ratio` entre títulos/legendas |
  | duração | 0,3 | `1 − |d1−d2| / max(3 s, 15 % da maior)` |
  | thumbnail | 0,3 | dHash 64 bits, reescalado (0,5 = aleatório → 0; ≥0,9 → 1) |

- `provavel_mesmo_video` exige `score ≥ 0,65` **e** duração ou thumbnail
  `≥ 0,8` (texto parecido sozinho não confirma).

**Resultado** (`data/correspondencias.csv`, 769 candidatos para os 20 vídeos)

| Plataforma | Candidatos | Metadados extraídos | Prováveis matches |
|---|---|---|---|
| TikTok | 209 | 203 | 0 |
| Instagram | 175 | 168 | 0 |
| Facebook | 162 | 157 | 4 |
| Dailymotion | 137 | 131 | 0 |
| Kwai | 71 | 0 | 0 |
| X | 15 | 5 | 0 |

**Conclusão:** como os virais são lives de horas, quase ninguém reposta o
vídeo inteiro: o que circula são **cortes**. Duração e thumbnail não servem
para reconhecê-los. Isso motivou o Experimento 2. O Kwai não é extraível pelo
`yt-dlp`.

---

## Experimento 2: cortes pela fala (`cortes.py`, `transcricao.py`)

**Hipótese:** um corte repete literalmente a fala de um trecho da live.

**Método**
1. Transcrição da live: legenda automática do YouTube (`json3`, faixa
   `pt-orig`), sem baixar o vídeo. Há retry com backoff exponencial para HTTP 429.
2. Candidatos: links do Experimento 1 + novas buscas `"<palavras-chave> corte"`
   em TikTok, Instagram, Facebook, X e Dailymotion.
3. Filtro: duração `≤ 900 s` e publicados depois do viral mais antigo.
4. Transcrição do corte: baixa só o áudio e roda `faster-whisper`
   (modelo `medium`, GPU, `language="pt"`, VAD).
5. Comparação: 4-gramas de palavras normalizadas (sem acento). Uma janela
   deslizante sobre a live (duração do corte + 60 s) procura o trecho com mais
   n-gramas distintos em comum.
   `cobertura = n-gramas do corte encontrados na janela / n-gramas do corte`.
   Cortes com menos de 15 n-gramas são descartados.
6. `eh_corte` se `cobertura ≥ 0,3`. Também são registrados o minuto do trecho
   na live e um link `&t=` direto para ele.

Todas as transcrições ficam em cache em `data/transcricoes/` (115 arquivos).

**Resultado** (`data/candidatos_cortes.csv`, `data/cortes.csv`)
- 278 candidatos curtos (Facebook 113, TikTok 112, Dailymotion 50, X 3).
  Desses, 87 foram baixados e transcritos com sucesso.
- **22 cortes confirmados**: Facebook 17, TikTok 4, Dailymotion 1, X 0.
- Os cortes saíram de 6 dos 20 vídeos virais. O que mais gerou cortes foi o
  debate presidencial da Band (10 cortes). Também aparecem a apuração da CNN
  Brasil (3), a JP Especial (4), o SBT News (2), o especial do STF (2) e o ICL (1).

**Limitações:** depende de haver fala (cortes só com música ou legenda escrita
não são avaliados); o download do Instagram e do Facebook muitas vezes exige
cookies (`YTDLP_COOKIES_BROWSER`); o DuckDuckGo bloqueia depois de algumas
dezenas de consultas.

---

## Experimento 3: impressão digital de áudio e quadros (`verificacao_av.py`, `impressao_av.py`)

**Hipótese:** reconhecer o mesmo conteúdo mesmo com título, miniatura,
formato, idioma da fala ou duração diferentes, sem depender de transcrição.

**Fontes de candidatos** (`fontes_extra.py`, criadas porque o buscador web
bloqueia): busca do YouTube, API pública do Dailymotion, páginas de hashtag do
Instagram (Playwright, hashtags derivadas de nomes próprios e tags do vídeo),
além dos links dos Experimentos 1 e 2 e de URLs informadas à mão. Quando uma
fonte esbarra em CAPTCHA, tela de login ou limite de requisições, ela é
interrompida e o evento vai para o log. Não há rotação de proxy nem disfarce
de navegador.

**Método**
- Download em 240p com a **faixa de áudio original** (evita as dublagens
  automáticas do YouTube). O vídeo é apagado logo após a extração, e só as
  impressões ficam em `data/impressoes/*.npz`.
- **Áudio**: constelação de picos do espectrograma (8 kHz, STFT 1024/256).
  Os pares de picos (fan-out 5, Δt ≤ 40 quadros) viram hashes de 32 bits.
  Lives são lidas em blocos de 10 min. Hashes com mais de 300 ocorrências no
  original são ignorados.
- **Quadros**: 1 quadro/s, recorte central de 60 %, pHash (DCT 8×8). No
  candidato são testadas 6 variações: proporção nativa, 9:16 e 16:9, cada uma
  com e sem espelhamento. Isso cobre vídeo vertical dentro de horizontal,
  tarjas e espelhamento. Quadros lisos/pretos são descartados.
- **Votação por offset**: os acertos só contam se concordarem num mesmo
  deslocamento de tempo (±1), com mínimo de 8 votos para áudio e 3 para
  quadros. Isso elimina coincidências soltas, como vinhetas e cenário de estúdio.
- **Veredito** a partir da cobertura (fração do candidato que bate):

  | Veredito | Regra |
  |---|---|
  | `reupload` | áudio ≥ 0,5 e quadros ≥ 0,5 |
  | `reupload_audio` | áudio ≥ 0,5 (imagem trocada ou coberta) |
  | `reupload_imagem` | quadros ≥ 0,5 (áudio trocado) |
  | `contem_trecho` | áudio ou quadros ≥ 0,15 (reportagem, react) |
  | `diferente` | caso contrário |

**Resultado** (`data/reuploads_av.csv`): rodada piloto com **1 vídeo**
("Flávio Bolsonaro reage à vitória de Lula", um short) e 200 candidatos
(194 processados, 6 sem download).

| Plataforma | Candidatos | reupload_audio | contem_trecho | diferente |
|---|---|---|---|---|
| Instagram | 118 | 0 | 0 | 116 |
| YouTube | 45 | 1 | 0 | 40 |
| Dailymotion | 37 | 1 | 1 | 35 |

Os 2 reuploads encontrados têm áudio idêntico (cobertura 0,73 e 0,81) e
imagem diferente (cobertura de quadros 0). Esse padrão é típico de quem
reaproveita o áudio sobre outra edição.

**Pendências:** rodar para os 20 virais; a cobertura de quadros deu 0 em
todos os candidatos do piloto, então vale validar o ramo de vídeo com
reuploads conhecidos antes de confiar nele.

---

## Experimento 4: viés por rede (`coleta_vies_redes.xlsx`)

Desenho do estudo principal, com **coleta manual**. A planilha tem as abas
`Leia-me`, `Perfis`, `Coleta` e `Resumo`.

- **Pergunta:** um vídeo idêntico do mesmo perfil alcança mais gente além da
  própria base numa rede do que nas outras? Isso muda com o espectro
  político (esquerda / centro / direita)?
- **Unidade:** um conteúdo publicado pelo mesmo perfil nas 4 redes (YouTube,
  TikTok, Instagram, X). Cada rede ocupa uma linha, e as 4 compartilham o mesmo
  `id_conteudo`.
- **Métrica:** `alcance_relativo = views / seguidores do perfil naquela rede`,
  resumida pela **média geométrica**, porque a distribuição é muito assimétrica.
- **Comparação principal:** a razão direita ÷ esquerda **dentro** de cada rede,
  e depois essa razão comparada **entre** redes. A comparação é feita assim
  porque "view" tem definições diferentes em cada plataforma.
- **Controles:** views coletadas com a mesma idade (sugestão: 7 dias); rótulos
  de tom, emocionalidade e formato, para separar o viés político do viés de
  engajamento.
- **Janela:** 30 a 90 dias após o 2º turno (24/11/2026 a 23/01/2027).
- **O que dá para afirmar:** diferenças de alcance da rede como um todo
  (algoritmo + público + criadores). Não dá para isolar o efeito do algoritmo,
  porque não há contas de teste. Há viés de seleção: só entram perfis que
  repostam nas 4 redes.

---

## Como rodar

```bash
pip install -r requirements.txt
# opcional, para hashtags do Instagram:
pip install playwright==1.49.1 && playwright install chromium

python main.py --tema politica --dias 7     # Etapa 0 + Experimento 1
python cortes.py                            # Experimento 2 (--retomar / --sem-busca)
python verificacao_av.py --max-virais 3     # Experimento 3
```

Variáveis de ambiente:

| Variável | Padrão | Uso |
|---|---|---|
| `YTDLP_COOKIES_BROWSER` | (vazio) | ex. `firefox`; necessário para Instagram/Facebook |
| `WHISPER_MODELO` | `medium` | modelo do faster-whisper |
| `WHISPER_DISPOSITIVO` | `cuda` | use `cpu` sem GPU |

`ffmpeg` precisa estar no PATH (ou instale `imageio-ffmpeg`). O
`requirements.txt` ainda não lista `numpy`, `scipy` e `playwright`, que o
Experimento 3 usa.

## Dados versionados (`data/`)

| Arquivo | Conteúdo |
|---|---|
| `virais_youtube.csv` | 20 vídeos virais e suas métricas |
| `correspondencias.csv` | 769 candidatos do Experimento 1 com scores |
| `candidatos_cortes.csv` | 278 candidatos curtos do Experimento 2 |
| `cortes.csv` | 87 cortes transcritos, com cobertura e trecho na live |
| `reuploads_av.csv` | 200 candidatos do piloto do Experimento 3 |
| `transcricoes/` | cache de legendas e transcrições Whisper (JSON) |
| `impressoes/` | cache das impressões de áudio e quadros (`.npz`) |

Os dados vêm de conteúdo público e foram coletados em outubro de 2026.
