"""
Análise de autos — sugestão de próximo passo (Streamlit + Gemini)
-----------------------------------------------------------------
Versão de teste: o usuário sobe o(s) PDF(s) dos autos, a IA analisa e a tela
mostra situação atual, próximo passo, confiança e justificativa.
NÃO lê nem grava planilha (não precisa de conta de serviço / JSON).

Segredos:
- Localmente: arquivo .streamlit/secrets.toml (veja o .example) OU variável de ambiente.
- No Streamlit Community Cloud: painel "Secrets" do app.
"""

import os
import json
import time
import tempfile

import streamlit as st
from google import genai
from google.genai import types

st.set_page_config(page_title="Análise de autos", page_icon="⚖️", layout="centered")


# ------------------------------------------------------------------
# Segredos / configuração (tenta st.secrets, depois variável de ambiente)
# ------------------------------------------------------------------
def cfg(nome, padrao=None):
    try:
        if nome in st.secrets:
            return st.secrets[nome]
    except Exception:
        pass
    return os.environ.get(nome, padrao)


GEMINI_API_KEY = cfg("GEMINI_API_KEY")
APP_PASSWORD = cfg("APP_PASSWORD")          # opcional: se vazio, acesso é livre
MODEL = cfg("GEMINI_MODEL", "gemini-2.0-flash")


# ------------------------------------------------------------------
# Login opcional (só ativa se APP_PASSWORD estiver definido)
# ------------------------------------------------------------------
def liberar_acesso() -> bool:
    if not APP_PASSWORD:
        return True
    if st.session_state.get("autenticado"):
        return True
    st.title("⚖️ Acesso restrito")
    st.caption("Digite a senha para acessar a ferramenta.")
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

if not GEMINI_API_KEY:
    st.error(
        "A chave do Gemini não está configurada. "
        "Defina GEMINI_API_KEY nos *Secrets* do app (ou no arquivo .streamlit/secrets.toml)."
    )
    st.stop()

cliente = genai.Client(api_key=GEMINI_API_KEY)


# ------------------------------------------------------------------
# IA
# ------------------------------------------------------------------
SISTEMA = (
    "Voce e um assistente juridico especializado em processo civil brasileiro, com foco "
    "na fase de cumprimento de sentenca e execucao. Recebera o(s) PDF(s) dos autos de um "
    "processo. Tarefas: (1) resumir a SITUACAO ATUAL em uma frase objetiva, baseada nas "
    "pecas e decisoes MAIS RECENTES; (2) indicar o PROXIMO PASSO processual mais provavel "
    "a ser adotado pela parte, como SUGESTAO a ser revisada por um advogado; (3) atribuir "
    "confianca (alta/media/baixa). Priorize a fase atual e as ultimas movimentacoes; "
    "ignore ruido processual antigo. Nao invente fatos que nao estejam nos autos. Se os "
    "documentos forem insuficientes, diga isso e use confianca baixa. Responda SOMENTE com "
    "JSON valido, sem comentarios nem cercas de codigo, com as chaves: situacao_atual, "
    "proximo_passo, confianca, justificativa."
)
PROMPT = (
    "Analise os autos em anexo e devolva o JSON conforme as instrucoes. "
    "A justificativa deve citar brevemente em que peca/decisao voce se baseou."
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


def analisar(caminhos):
    enviados = []
    try:
        for p in caminhos:
            f = cliente.files.upload(file=p)
            while f.state.name == "PROCESSING":   # PDFs grandes demoram a processar
                time.sleep(2)
                f = cliente.files.get(name=f.name)
            if f.state.name == "FAILED":
                raise RuntimeError(f"O Gemini não conseguiu processar {os.path.basename(p)}.")
            enviados.append(f)

        resp = cliente.models.generate_content(
            model=MODEL,
            contents=list(enviados) + [PROMPT],
            config=types.GenerateContentConfig(
                system_instruction=SISTEMA,
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        return _parse_json(resp.text)
    finally:
        for f in enviados:
            try:
                cliente.files.delete(name=f.name)
            except Exception:
                pass


# ------------------------------------------------------------------
# Interface
# ------------------------------------------------------------------
st.title("⚖️ Análise de autos")
st.caption("Envie o(s) PDF(s) dos autos. A IA sugere a situação atual e o próximo passo — "
           "para revisão de um advogado.")

arquivos = st.file_uploader(
    "Autos em PDF (pode enviar mais de um arquivo)",
    type=["pdf"],
    accept_multiple_files=True,
)

if st.button("Analisar", type="primary", disabled=not arquivos):
    with st.spinner("Analisando os autos… pode levar alguns minutos em processos grandes."):
        tmp = tempfile.mkdtemp(prefix="autos_")
        caminhos = []
        for uf in arquivos:
            dest = os.path.join(tmp, uf.name)
            with open(dest, "wb") as out:
                out.write(uf.getbuffer())
            caminhos.append(dest)
        try:
            r = analisar(caminhos)
        except Exception as e:
            st.error(f"Não foi possível concluir a análise: {e}")
            st.stop()

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
