"""
Análise de autos + gravação no Google Sheets (Streamlit + Together.ai)
----------------------------------------------------------------------
Fluxo: upload do PDF -> recorte da fase pós-sentença -> análise (Together) ->
o advogado confere e clica em "Gravar na planilha". O app casa o nº do processo
(extraído do PDF) com a coluna "Nº PROCESSO" da planilha e grava:
  - PRÓXIMO PASSO
  - OBSERVAÇÕES (situação, última mov., valores, confiança, data da análise)

Segredos (Streamlit Cloud: painel "Secrets"; local: .streamlit/secrets.toml):
  TOGETHER_API_KEY, APP_PASSWORD, SPREADSHEET_ID, e a tabela [gcp_service_account].
A conta de serviço (gcp_service_account) precisa ter a planilha COMPARTILHADA como Editor.
"""

import os
import re
import json
from datetime import datetime

import streamlit as st
import fitz  # PyMuPDF
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

st.set_page_config(page_title="Análise de autos", page_icon="⚖️", layout="centered")

TOGETHER_BASE_URL = "https://api.together.xyz/v1"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ------------------------------------------------------------------ config
def cfg(nome, padrao=None):
    try:
        if nome in st.secrets:
            return st.secrets[nome]
    except Exception:
        pass
    return os.environ.get(nome, padrao)


TOGETHER_API_KEY = cfg("TOGETHER_API_KEY")
APP_PASSWORD = cfg("APP_PASSWORD")
MODEL = cfg("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
MAX_CHARS = int(cfg("MAX_CHARS", "300000"))
ESCRITORIO = cfg("ESCRITORIO", "Matheus Carvalho")
DIAS_PARADO_PROVOCAR = int(cfg("DIAS_PARADO_PROVOCAR", "60"))
DATA_HOJE = datetime.now().strftime("%d/%m/%Y")

SPREADSHEET_ID = cfg("SPREADSHEET_ID", "")
SHEET_NAME = cfg("SHEET_NAME", "Cumprimento de setença - Recebíveis (Farol+MC)")
HEADER_ROW = int(cfg("HEADER_ROW", "3"))
COL_NUMERO = cfg("COL_NUMERO", "Nº PROCESSO")
COL_PROXIMO = cfg("COL_PROXIMO", "PRÓXIMO PASSO")
COL_OBS = cfg("COL_OBS", "OBSERVAÇÕES")

_SENTINELAS = {"", "—", "nao localizado", "não localizado", "nao se aplica",
               "não se aplica", "indefinido", "n/a"}


def _limpo(v):
    v = "" if v is None else str(v).strip()
    return "" if v.lower() in _SENTINELAS else v


# ------------------------------------------------------------------ login opcional
def liberar_acesso() -> bool:
    if not APP_PASSWORD:
        return True
    if st.session_state.get("autenticado"):
        return True
    st.title("⚖️ Acesso restrito")
    senha = st.text_input("Senha", type="password")
    if st.button("Entrar"):
        if senha == APP_PASSWORD:
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    return False


if not liberar_acesso():
    st.stop()

if not TOGETHER_API_KEY:
    st.error("A chave da Together não está configurada. Defina TOGETHER_API_KEY nos *Secrets*.")
    st.stop()

cliente = OpenAI(api_key=TOGETHER_API_KEY, base_url=TOGETHER_BASE_URL)


# ------------------------------------------------------------------ PDF: leitura e recorte
@st.cache_data(show_spinner=False)
def carregar_pdf(_nome: str, dados: bytes):
    with fitz.open(stream=dados, filetype="pdf") as d:
        textos = [p.get_text() for p in d]
        toc = d.get_toc(simple=True)
    return textos, toc


PADRAO_SENTENCA = re.compile(
    r"(julgo\s+(parcial(mente)?\s+)?(proceden|improceden)|cumprimento\s+de\s+senten"
    r"|tr[âa]nsito\s+em\s+julgado|disposit[íi]vo)", re.IGNORECASE)
PADRAO_SENTENCA_FORTE = re.compile(
    r"(julgo\s+(parcial(mente)?\s+)?(proceden|improceden)|\bhomologo\b"
    r"|publique[-\s]*se[,.\s]+registre[-\s]*se|\bp\s*\.?\s*r\s*\.?\s*i\b)", re.IGNORECASE)


def _eh_indice(t: str) -> bool:
    cabeca = (t or "")[:600].lower()
    return any(p in cabeca for p in ("índice", "indice", "sumário", "sumario"))


def detectar_inicio(textos, toc):
    total = len(textos)
    if toc:
        paginas = [pg for (_lvl, titulo, pg) in toc
                   if re.search(r"senten|tr[âa]nsito\s+em\s+julgado", titulo or "", re.IGNORECASE)
                   and pg > 1]
        if paginas:
            return max(0, min(paginas) - 1), "marcadores do PDF (índice)"
    for i in range(total):
        if _eh_indice(textos[i]):
            continue
        if PADRAO_SENTENCA_FORTE.search(textos[i] or ""):
            return i, "conteúdo da sentença no texto"
    inicio = 1 if total > 1 else 0
    for i in range(inicio, total):
        if _eh_indice(textos[i]):
            continue
        if PADRAO_SENTENCA.search(textos[i] or ""):
            return i, "marcos da sentença no texto"
    return None, None


RE_CNJ = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")


def extrair_numero(texto_completo: str) -> str:
    m = RE_CNJ.search(texto_completo or "")
    return m.group(0) if m else ""


# ------------------------------------------------------------------ IA (Together)
SISTEMA = (
    "Voce e um assistente juridico especializado em processo civil brasileiro, fase de "
    "cumprimento de sentenca e execucao. Recebera o TEXTO da fase pos-sentenca de um processo "
    "(ja recortado) e a DATA DE HOJE. O escritorio que faz a consulta e: \"{ESCRITORIO}\".\n\n"
    "Identifique no texto qual parte o escritorio \"{ESCRITORIO}\" representa (procuracoes, "
    "peticoes assinadas, OAB). TODO o proximo_passo deve ser escrito do ponto de vista do NOSSO "
    "escritorio: o que NOS devemos fazer em seguida.\n\n"
    "Produza um objeto JSON valido (sem texto fora dele e sem cercas de codigo) com EXATAMENTE "
    "estas chaves:\n"
    "- situacao_atual: em que pe esta o andamento pos-sentenca, 1-2 frases objetivas, com base nas "
    "movimentacoes mais recentes. NAO cite os nomes das partes; refira-se a elas como autor/exequente "
    "e reu/executado (ou autora/exequente e re/executada).\n"
    "- parte_representada: qual parte/polo o escritorio representa, ou \"indefinido\".\n"
    "- proximo_passo: a proxima acao a ser tomada PELO NOSSO ESCRITORIO, como SUGESTAO para revisao "
    "de advogado. Considere o estado atual: (a) se ha prazo da parte contraria em curso, o passo e "
    "aguardar e acompanhar; (b) se esta concluso/aguardando ato do juizo ha MAIS de {DIAS} dias em "
    "relacao a DATA DE HOJE, o passo e PROVOCAR O JUIZO (peticionar cobrando a decisao, com pedido de "
    "prioridade se cabivel); (c) se ja cabe a nos uma providencia concreta (ex.: requerer multa, "
    "penhora, Sisbajud), descreva-a.\n"
    "- valor_execucao: o valor que o NOSSO escritorio busca receber NESTA fase de cumprimento "
    "(principal nao pago + multa do art. 523 + honorarios), conforme requerido nos autos. Se ainda "
    "NAO estiver em execucao, responda \"nao se aplica\"; se deveria constar e nao achou, \"nao localizado\".\n"
    "- valor_da_causa: o valor da causa originario, ou \"nao localizado\".\n"
    "- data_ultima_movimentacao: data da ultima movimentacao relevante (DD/MM/AAAA) ou \"indefinido\".\n"
    "- confianca: alta/media/baixa.\n"
    "- justificativa: cite brevemente as pecas/decisoes em que se baseou.\n\n"
    "Nao invente fatos que nao estejam no texto. Se algo nao constar, use \"indefinido\"/\"nao "
    "localizado\" e reduza a confianca."
).replace("{ESCRITORIO}", ESCRITORIO).replace("{DIAS}", str(DIAS_PARADO_PROVOCAR))


def _parse_json(texto: str) -> dict:
    t = (texto or "").replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(t)
    except Exception:
        i, f = t.find("{"), t.rfind("}")
        if i >= 0 and f > i:
            try:
                return json.loads(t[i:f + 1])
            except Exception:
                pass
    return {"situacao_atual": "Falha ao interpretar a resposta da IA",
            "proximo_passo": t[:200], "confianca": "baixa", "justificativa": ""}


def analisar(texto_pos_sentenca: str) -> dict:
    prompt = (f"DATA DE HOJE: {DATA_HOJE}\n\n=== TEXTO (fase pós-sentença) ===\n"
              + texto_pos_sentenca + "\n\nResponda apenas com o JSON pedido.")
    resp = cliente.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SISTEMA}, {"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=1024,
    )
    return _parse_json(resp.choices[0].message.content)


# ------------------------------------------------------------------ cartão de resultado
def card_resultado(numero, r):
    import html as _html
    def esc(x):
        return _html.escape(str(x) if x not in (None, "") else "—")
    conf = (r.get("confianca") or "baixa").lower()
    cores = {"alta": ("#15803d", "#dcfce7"), "media": ("#92740e", "#fef9c3"),
             "baixa": ("#b91c1c", "#fee2e2")}
    ctxt, cbg = cores.get(conf, ("#475569", "#e2e8f0"))
    pill = ('<span style="background:%s;color:%s;padding:3px 12px;border-radius:999px;'
            'font-size:13px;font-weight:700">%s</span>') % (cbg, ctxt, esc(conf))

    def metric(rotulo, valor):
        v = _limpo(valor)
        if not v:
            return ""
        return ('<div style="flex:1;min-width:120px;background:#f4f1ea;border-radius:10px;'
                'padding:10px 12px"><div style="font-size:11px;letter-spacing:.04em;'
                'text-transform:uppercase;color:#7a7468;margin-bottom:3px">' + esc(rotulo) +
                '</div><div style="font-size:15px;color:#1c2530">' + esc(v) + '</div></div>')

    def label(t):
        return ('<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;'
                'color:#9a6a3a;font-weight:700;margin:0 0 4px">' + t + '</div>')

    def texto(t):
        return ('<div style="font-size:15.5px;color:#1c2530;line-height:1.6;margin:0 0 16px">'
                + esc(t) + '</div>')

    cabecalho = ''
    if (numero or '').strip():
        cabecalho = ('<div style="font-size:15px;color:#1c2530;margin:0 0 14px">'
                     '<b>Processo Nº:</b> ' + esc(numero) + '</div>')

    caixas = (metric("Valor em execução", r.get("valor_execucao"))
              + metric("Valor da causa", r.get("valor_da_causa"))
              + metric("Última movimentação", r.get("data_ultima_movimentacao")))
    metricas = ('<div style="display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px">'
                + caixas + '</div>') if caixas else ''

    return ('<div style="border:1px solid #e6e1d6;border-radius:14px;padding:18px 20px;'
            'background:#ffffff;font-family:Georgia,serif">'
            + cabecalho
            + label("Situação atual") + texto(r.get("situacao_atual"))
            + label("Próximo passo (sugestão)") + texto(r.get("proximo_passo"))
            + metricas
            + '<div style="font-size:15px;color:#1c2530"><b>Confiança:</b> ' + pill + '</div>'
            + '</div>')


# ------------------------------------------------------------------ Google Sheets
def _aba():
    return "'" + SHEET_NAME.replace("'", "''") + "'"


def _col_to_a1(idx):
    s = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _norm(x):
    return re.sub(r"\D", "", str(x or ""))


def _sheets():
    info = None
    try:
        if "gcp_service_account" in st.secrets:
            info = dict(st.secrets["gcp_service_account"])
    except Exception:
        info = None
    if info:
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            cfg("SERVICE_ACCOUNT_FILE", "service_account.json"), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _resolver_cols(valores, svc):
    header = valores[HEADER_ROW - 1] if len(valores) >= HEADER_ROW else []

    def achar(nome):
        a = nome.strip().lower()
        for i, h in enumerate(header):
            if str(h).strip().lower() == a:
                return i + 1
        return 0

    numero = achar(COL_NUMERO)
    if not numero:
        raise RuntimeError('Coluna "%s" não encontrada na linha %d da aba.' % (COL_NUMERO, HEADER_ROW))

    def garantir(nome, atual):
        if atual:
            return atual
        idx = len(header) + 1
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range="%s!%s%d" % (_aba(), _col_to_a1(idx), HEADER_ROW),
            valueInputOption="RAW", body={"values": [[nome]]}).execute()
        header.append(nome)
        return idx

    proximo = garantir(COL_PROXIMO, achar(COL_PROXIMO))
    obs = garantir(COL_OBS, achar(COL_OBS))
    return {"numero": numero, "proximo": proximo, "obs": obs}


def montar_observacoes(r):
    partes = []
    sit = _limpo(r.get("situacao_atual"))
    if sit:
        partes.append(sit)
    for rotulo, chave in [("Última mov.", "data_ultima_movimentacao"),
                          ("Valor em execução", "valor_execucao"), ("Valor da causa", "valor_da_causa")]:
        v = _limpo(r.get(chave))
        if v:
            partes.append("%s: %s" % (rotulo, v))
    return ("[Análise IA em %s] " % DATA_HOJE) + " | ".join(partes)


def gravar_na_planilha(numero, r):
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não configurado nos Secrets.")
    alvo = _norm(numero)
    if not alvo:
        raise RuntimeError("Número do processo vazio — preencha o campo antes de gravar.")
    svc = _sheets()
    valores = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=_aba()).execute().get("values", [])
    cols = _resolver_cols(valores, svc)
    ci = cols["numero"] - 1
    linha_alvo = None
    for idx in range(HEADER_ROW, len(valores)):
        linha = valores[idx]
        cel = linha[ci] if ci < len(linha) else ""
        if _norm(cel) == alvo:
            linha_alvo = idx + 1
            break
    if linha_alvo is None:
        raise RuntimeError('Processo %s não encontrado na coluna "%s" da planilha.' % (numero, COL_NUMERO))
    dados = [
        {"range": "%s!%s%d" % (_aba(), _col_to_a1(cols["proximo"]), linha_alvo),
         "values": [[r.get("proximo_passo", "")]]},
        {"range": "%s!%s%d" % (_aba(), _col_to_a1(cols["obs"]), linha_alvo),
         "values": [[montar_observacoes(r)]]},
    ]
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": dados}).execute()
    return "Gravado na linha %d da planilha (processo %s)." % (linha_alvo, numero)


# ------------------------------------------------------------------ interface
st.title("⚖️ Análise de autos — fase pós-sentença")
st.caption("Envie o(s) PDF(s). A IA sugere o próximo passo e grava na planilha pelo número do processo.")

arquivos = st.file_uploader("Autos em PDF", type=["pdf"], accept_multiple_files=True)
texto_para_analise = None
numero_processo = ""

if arquivos:
    texto_completo = ""
    if len(arquivos) == 1:
        uf = arquivos[0]
        textos, toc = carregar_pdf(uf.name, uf.getvalue())
        texto_completo = "\n".join(textos)
        total = len(textos)
        start, motivo = detectar_inicio(textos, toc)
        inteiro = st.checkbox("Analisar o documento inteiro (ignorar recorte)", value=False)
        if inteiro:
            texto_para_analise = "\n".join(textos)
            st.caption("Documento inteiro: %d páginas." % total)
        else:
            if start is not None:
                st.success("Fase pós-sentença detectada a partir da página %d (via %s)." % (start + 1, motivo))
                padrao = start + 1
            else:
                st.warning("Não identifiquei o marco da sentença. Ajuste a página inicial, se souber.")
                padrao = 1
            pini = st.number_input("Analisar a partir da página", min_value=1, max_value=total,
                                   value=padrao, step=1)
            texto_para_analise = "\n".join(textos[pini - 1:])
            st.caption("Serão analisadas as páginas %d–%d (%d de %d)." % (pini, total, total - pini + 1, total))
    else:
        partes, completos = [], []
        for uf in arquivos:
            textos, toc = carregar_pdf(uf.name, uf.getvalue())
            completos.append("\n".join(textos))
            start, _ = detectar_inicio(textos, toc)
            s = start if start is not None else 0
            partes.append("[%s]\n" % uf.name + "\n".join(textos[s:]))
        texto_para_analise = "\n\n".join(partes)
        texto_completo = "\n".join(completos)
        st.caption("Vários arquivos: recorte pós-sentença aplicado em cada um.")

    numero_processo = st.text_input("Número do processo", value=extrair_numero(texto_completo),
                                    placeholder="Não identificado — preencha se quiser")

if st.button("Analisar", type="primary", disabled=not texto_para_analise):
    if len(texto_para_analise.strip()) < 200:
        st.warning("Quase não há texto neste trecho — o PDF pode ser escaneado. O resultado pode ficar limitado.")
    if len(texto_para_analise) > MAX_CHARS:
        texto_para_analise = texto_para_analise[-MAX_CHARS:]
    with st.spinner("Analisando…"):
        try:
            r = analisar(texto_para_analise)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate" in msg.lower() or "quota" in msg.lower():
                st.error("Limite de uso atingido. Aguarde um instante e tente novamente.")
            else:
                st.error("Não foi possível concluir a análise. Veja o detalhe abaixo.")
            with st.expander("Detalhes técnicos do erro"):
                st.code(msg)
            st.stop()
    st.session_state["resultado"] = r
    st.session_state["numero"] = numero_processo
    st.session_state.pop("gravado", None)

if "resultado" in st.session_state:
    r = st.session_state["resultado"]
    numero = st.session_state.get("numero", "")
    st.divider()
    st.markdown(card_resultado(numero, r), unsafe_allow_html=True)
    if r.get("justificativa"):
        with st.expander("Por que a IA sugeriu isso?"):
            st.write(r["justificativa"])

    if st.session_state.get("gravado"):
        st.success(st.session_state["gravado"])
    else:
        if st.button("Gravar na planilha", type="primary"):
            try:
                st.session_state["gravado"] = gravar_na_planilha(numero, r)
                st.rerun()
            except Exception as e:
                st.error("Não consegui gravar: " + str(e))
                with st.expander("Detalhes técnicos"):
                    st.code(str(e))
    st.info("A sugestão é para triagem; a decisão final é sempre do advogado.")
