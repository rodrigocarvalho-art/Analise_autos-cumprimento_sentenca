"""
Análise de autos — sugestão de próximo passo (Streamlit + Together.ai)
---------------------------------------------------------------------
Foco: acompanhamento da fase PÓS-SENTENÇA.

Fluxo:
  1) extrai o texto por página do PDF (PyMuPDF);
  2) recorta a fase pós-sentença pelos marcadores embutidos do PDF (a "indexação")
     e, na falta deles, por marcos da sentença no texto;
  3) mostra a página detectada e deixa o usuário CORRIGIR (transparência);
  4) envia só esse trecho a um modelo da Together.ai (API compatível com OpenAI).

Modelos de texto da Together NÃO leem PDF diretamente — por isso extraímos o
texto aqui. Como os autos são "na maior parte texto", isso cobre o essencial.

NÃO lê nem grava planilha. Segredos: .streamlit/secrets.toml ou painel "Secrets".
"""

import os
import re
import json
from datetime import datetime
import html as _html

import streamlit as st
import fitz  # PyMuPDF
from openai import OpenAI

st.set_page_config(page_title="Análise de autos", page_icon="⚖️", layout="centered")

TOGETHER_BASE_URL = "https://api.together.xyz/v1"


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
# Confirme o ID exato no painel da Together (o catálogo muda).
# Alternativa forte: "Qwen/Qwen2.5-72B-Instruct-Turbo".
MODEL = cfg("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
MAX_CHARS = int(cfg("MAX_CHARS", "300000"))   # rede de seguranca p/ o contexto do modelo
ESCRITORIO = cfg("ESCRITORIO", "Matheus Carvalho")   # usado para enquadrar o "próximo passo" como ação nossa
DIAS_PARADO_PROVOCAR = int(cfg("DIAS_PARADO_PROVOCAR", "60"))  # concluso/parado além disso => sugerir provocar o juízo
DATA_HOJE = datetime.now().strftime("%d/%m/%Y")


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


# ------------------------------------------------------------------ leitura e recorte do PDF
@st.cache_data(show_spinner=False)
def carregar_pdf(_nome: str, dados: bytes):
    with fitz.open(stream=dados, filetype="pdf") as d:
        textos = [p.get_text() for p in d]
        toc = d.get_toc(simple=True)  # [[nivel, titulo, pagina_1based], ...]
    return textos, toc


PADRAO_SENTENCA = re.compile(
    r"(julgo\s+(parcial(mente)?\s+)?(proceden|improceden)"
    r"|cumprimento\s+de\s+senten"
    r"|tr[âa]nsito\s+em\s+julgado"
    r"|disposit[íi]vo)",
    re.IGNORECASE,
)


# marcos que só aparecem no CORPO da sentença (não numa página de índice/sumário)
PADRAO_SENTENCA_FORTE = re.compile(
    r"(julgo\s+(parcial(mente)?\s+)?(proceden|improceden)"
    r"|\bhomologo\b"
    r"|publique[-\s]*se[,.\s]+registre[-\s]*se"
    r"|\bp\s*\.?\s*r\s*\.?\s*i\b)",
    re.IGNORECASE,
)


def _eh_indice(t: str) -> bool:
    """Heurística simples: a página é o índice/sumário do próprio documento."""
    cabeca = (t or "")[:600].lower()
    return any(p in cabeca for p in ("índice", "indice", "sumário", "sumario"))


def detectar_inicio(textos, toc):
    total = len(textos)
    # 1) marcadores embutidos do PDF (índice clicável), ignorando a capa (pág. 1)
    if toc:
        paginas = [pg for (_lvl, titulo, pg) in toc
                   if re.search(r"senten|tr[âa]nsito\s+em\s+julgado", titulo or "", re.IGNORECASE)
                   and pg > 1]
        if paginas:
            return max(0, min(paginas) - 1), "marcadores do PDF (índice)"
    # 2) corpo da sentença: marcos fortes que NÃO aparecem numa página de índice/sumário
    for i in range(total):
        if _eh_indice(textos[i]):
            continue
        if PADRAO_SENTENCA_FORTE.search(textos[i] or ""):
            return i, "conteúdo da sentença no texto"
    # 3) fallback fraco (palavras-título), pulando a capa e as páginas de índice/sumário
    inicio = 1 if total > 1 else 0
    for i in range(inicio, total):
        if _eh_indice(textos[i]):
            continue
        if PADRAO_SENTENCA.search(textos[i] or ""):
            return i, "marcos da sentença no texto"
    return None, None


# numero unico CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO
RE_CNJ = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")


def extrair_numero(texto_completo: str) -> str:
    m = RE_CNJ.search(texto_completo or "")
    return m.group(0) if m else ""


# ------------------------------------------------------------------ IA (Together.ai)
SISTEMA = (
    "Voce e um assistente juridico especializado em processo civil brasileiro, fase de "
    "cumprimento de sentenca e execucao. Recebera o TEXTO da fase pos-sentenca de um processo "
    "(ja recortado) e a DATA DE HOJE. O escritorio que faz a consulta e: \"{ESCRITORIO}\".\n\n"
    "Identifique no texto qual parte o escritorio \"{ESCRITORIO}\" representa (procuracoes, "
    "peticoes assinadas, OAB) e em qual polo. TODO o proximo_passo deve ser escrito do ponto de "
    "vista do NOSSO escritorio: o que NOS devemos fazer em seguida.\n\n"
    "Produza um objeto JSON valido (sem texto fora dele e sem cercas de codigo) com EXATAMENTE "
    "estas chaves:\n"
    "- situacao_atual: em que pe esta o andamento pos-sentenca, 1-2 frases objetivas, com base nas "
    "movimentacoes mais recentes.\n"
    "- parte_representada: qual parte/polo o escritorio representa, ou \"indefinido\".\n"
    "- proximo_passo: a proxima acao a ser tomada PELO NOSSO ESCRITORIO, como SUGESTAO para revisao "
    "de advogado. Considere o estado atual: (a) se ha prazo da parte contraria em curso (ex.: "
    "pagamento/impugnacao), o passo e aguardar e acompanhar o decurso do prazo; (b) se o processo "
    "esta concluso/aguardando decisao ou ato do juizo ha MAIS de {DIAS} dias em relacao a DATA DE "
    "HOJE, o passo e PROVOCAR O JUIZO (peticionar cobrando a decisao/o andamento, com pedido de "
    "prioridade se cabivel); (c) se ja cabe a nos uma providencia concreta (ex.: requerer multa, "
    "penhora, Sisbajud), descreva-a objetivamente.\n"
    "- data_ultima_movimentacao: data da ultima movimentacao relevante (DD/MM/AAAA) ou \"indefinido\".\n"
    "- valor_execucao: o valor que o NOSSO escritorio busca receber NESTA fase de cumprimento "
    "(em regra, o principal nao pago acrescido da multa do art. 523 e dos honorarios), conforme "
    "requerido nos autos; ou \"nao localizado\".\n"
    "- valor_da_causa: o valor da causa originario indicado nos autos; ou \"nao localizado\".\n"
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
    prompt = (f"DATA DE HOJE: {DATA_HOJE}\n\n"
              "=== TEXTO (fase pós-sentença) ===\n" + texto_pos_sentenca +
              "\n\nResponda apenas com o JSON pedido.")
    resp = cliente.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SISTEMA},
                  {"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1024,
    )
    return _parse_json(resp.choices[0].message.content)





def card_resultado(numero, r):
    def esc(x):
        return _html.escape(str(x) if x not in (None, "") else "—")
    conf = (r.get("confianca") or "baixa").lower()
    cores = {"alta": ("#15803d", "#dcfce7"), "media": ("#92740e", "#fef9c3"),
             "baixa": ("#b91c1c", "#fee2e2")}
    ctxt, cbg = cores.get(conf, ("#475569", "#e2e8f0"))
    pill = ('<span style="background:%s;color:%s;padding:3px 12px;border-radius:999px;'
            'font-size:13px;font-weight:700">%s</span>') % (cbg, ctxt, esc(conf))

    def metric(rotulo, valor):
        return ('<div style="flex:1;min-width:120px;background:#f4f1ea;border-radius:10px;'
                'padding:10px 12px"><div style="font-size:11px;letter-spacing:.04em;'
                'text-transform:uppercase;color:#7a7468;margin-bottom:3px">' + esc(rotulo) +
                '</div><div style="font-size:15px;color:#1c2530">' + esc(valor) + '</div></div>')

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

    metricas = ('<div style="display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px">'
                + metric("Valor em execução", r.get("valor_execucao"))
                + metric("Valor da causa", r.get("valor_da_causa"))
                + metric("Última movimentação", r.get("data_ultima_movimentacao"))
                + '</div>')

    return ('<div style="border:1px solid #e6e1d6;border-radius:14px;padding:18px 20px;'
            'background:#ffffff;font-family:Georgia,serif">'
            + cabecalho
            + label("Situação atual") + texto(r.get("situacao_atual"))
            + label("Próximo passo (sugestão)") + texto(r.get("proximo_passo"))
            + metricas
            + '<div style="font-size:15px;color:#1c2530"><b>Confiança:</b> ' + pill + '</div>'
            + '</div>')


# ------------------------------------------------------------------ interface
st.title("⚖️ Análise de autos — fase pós-sentença")
st.caption("Envie o(s) PDF(s). O app recorta a fase pós-sentença e a IA sugere o próximo passo — "
           "para revisão de um advogado.")

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
            st.caption(f"Documento inteiro: {total} páginas.")
        else:
            if start is not None:
                st.success(f"Fase pós-sentença detectada a partir da página {start + 1} "
                           f"(via {motivo}).")
                padrao = start + 1
            else:
                st.warning("Não identifiquei automaticamente o marco da sentença. "
                           "Ajuste a página inicial abaixo, se souber.")
                padrao = 1
            pini = st.number_input("Analisar a partir da página", min_value=1,
                                   max_value=total, value=padrao, step=1)
            texto_para_analise = "\n".join(textos[pini - 1:])
            st.caption(f"Serão analisadas as páginas {pini}–{total} "
                       f"({total - pini + 1} de {total}).")
    else:
        partes, completos = [], []
        for uf in arquivos:
            textos, toc = carregar_pdf(uf.name, uf.getvalue())
            completos.append("\n".join(textos))
            start, _ = detectar_inicio(textos, toc)
            s = start if start is not None else 0
            partes.append(f"[{uf.name}]\n" + "\n".join(textos[s:]))
        texto_para_analise = "\n\n".join(partes)
        texto_completo = "\n".join(completos)
        st.caption("Vários arquivos: apliquei o recorte pós-sentença automaticamente em cada um.")

    numero_processo = st.text_input(
        "Número do processo",
        value=extrair_numero(texto_completo),
        placeholder="Não identificado — preencha se quiser",
    )

if st.button("Analisar", type="primary", disabled=not texto_para_analise):
    if len(texto_para_analise.strip()) < 200:
        st.warning("Quase não há texto neste trecho — o PDF pode ser escaneado (imagem). "
                   "O resultado pode ficar limitado.")
    truncado = len(texto_para_analise) > MAX_CHARS
    if truncado:
        texto_para_analise = texto_para_analise[-MAX_CHARS:]

    qtd = len(texto_para_analise)
    st.caption(f"Trecho enviado à IA: ~{qtd:,} caracteres (≈ {qtd // 4:,} tokens estimados).")

    with st.spinner("Analisando…"):
        try:
            r = analisar(texto_para_analise)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate" in msg.lower() or "quota" in msg.lower():
                st.error("Limite de uso atingido. Aguarde um instante e tente de novo, "
                         "ou reduza o trecho (começar de uma página mais à frente).")
            elif "model" in msg.lower() and ("not" in msg.lower() or "invalid" in msg.lower()):
                st.error("Modelo não reconhecido. Confira o ID em TOGETHER_MODEL no painel da Together.")
            else:
                st.error("Não foi possível concluir a análise. Veja o detalhe abaixo.")
            with st.expander("Detalhes técnicos do erro"):
                st.code(msg)
            st.stop()

    if truncado:
        st.caption("⚠️ Trecho ainda muito extenso; analisei a porção final do texto.")

    st.divider()
    st.markdown(card_resultado(numero_processo, r), unsafe_allow_html=True)
    if r.get("justificativa"):
        with st.expander("Por que a IA sugeriu isso?"):
            st.write(r["justificativa"])

    st.markdown("#### Para colar na planilha")
    st.caption("Use o botão de copiar no canto do bloco abaixo.")
    st.code(r.get("proximo_passo", ""), language=None)
    st.info("Esta é uma **sugestão** da IA para triagem. A decisão final é sempre do advogado.")
