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


def detectar_inicio(textos, toc):
    if toc:
        paginas = [pg for (_lvl, titulo, pg) in toc
                   if re.search(r"senten|cumprimento", titulo or "", re.IGNORECASE)]
        if paginas:
            return max(0, min(paginas) - 1), "marcadores do PDF (índice)"
    for i, t in enumerate(textos):
        if PADRAO_SENTENCA.search(t or ""):
            return i, "marcos da sentença no texto"
    return None, None


# ------------------------------------------------------------------ IA (Together.ai)
SISTEMA = (
    "Voce e um assistente juridico especializado em processo civil brasileiro, com foco "
    "na fase de cumprimento de sentenca e execucao. Recebera o TEXTO da fase POS-SENTENCA "
    "de um processo (ja recortado). Tarefas: (1) resumir a SITUACAO ATUAL em uma frase "
    "objetiva, baseada nas pecas/decisoes mais recentes; (2) indicar o PROXIMO PASSO "
    "processual mais provavel a ser adotado pela parte, como SUGESTAO a ser revisada por "
    "um advogado; (3) atribuir confianca (alta/media/baixa). Nao invente fatos que nao "
    "estejam no texto. Se o texto for insuficiente, diga isso e use confianca baixa. "
    "Responda APENAS com um objeto JSON valido, sem nenhum texto fora dele e sem cercas de "
    "codigo, com exatamente as chaves: situacao_atual, proximo_passo, confianca, justificativa."
)


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
    prompt = ("=== TEXTO (fase pós-sentença) ===\n" + texto_pos_sentenca +
              "\n\nResponda apenas com o JSON pedido.")
    resp = cliente.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SISTEMA},
                  {"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1024,
    )
    return _parse_json(resp.choices[0].message.content)


# ------------------------------------------------------------------ interface
st.title("⚖️ Análise de autos — fase pós-sentença")
st.caption("Envie o(s) PDF(s). O app recorta a fase pós-sentença e a IA sugere o próximo passo — "
           "para revisão de um advogado.")

arquivos = st.file_uploader("Autos em PDF", type=["pdf"], accept_multiple_files=True)

texto_para_analise = None

if arquivos:
    if len(arquivos) == 1:
        uf = arquivos[0]
        textos, toc = carregar_pdf(uf.name, uf.getvalue())
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
        partes = []
        for uf in arquivos:
            textos, toc = carregar_pdf(uf.name, uf.getvalue())
            start, _ = detectar_inicio(textos, toc)
            s = start if start is not None else 0
            partes.append(f"[{uf.name}]\n" + "\n".join(textos[s:]))
        texto_para_analise = "\n\n".join(partes)
        st.caption("Vários arquivos: apliquei o recorte pós-sentença automaticamente em cada um.")

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

    conf = (r.get("confianca") or "baixa").lower()
    selo = {"alta": "🟢 alta", "media": "🟡 média", "baixa": "🔴 baixa"}.get(conf, "⚪ —")

    st.divider()
    st.markdown("#### Situação atual")
    st.write(r.get("situacao_atual", "—"))
    st.markdown("#### Próximo passo (sugestão)")
    st.write(r.get("proximo_passo", "—"))
    st.markdown(f"**Confiança:** {selo}")
    if r.get("justificativa"):
        with st.expander("Por que a IA sugeriu isso?"):
            st.write(r["justificativa"])
    st.markdown("#### Para colar na planilha")
    st.caption("Use o botão de copiar no canto do bloco abaixo.")
    st.code(r.get("proximo_passo", ""), language=None)
    st.info("Esta é uma **sugestão** da IA para triagem. A decisão final é sempre do advogado.")
