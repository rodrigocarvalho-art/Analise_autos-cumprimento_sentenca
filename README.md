# ⚖️ Análise de autos — sugestão de próximo passo

Uma ferramenta simples: o advogado envia o PDF dos autos de um processo e a
inteligência artificial responde, na tela, **qual a situação atual** e **qual o
provável próximo passo** — sempre como uma *sugestão* para o advogado revisar.

Nesta versão de teste, a ferramenta **não mexe na planilha**: ela só analisa o
arquivo e mostra o resultado. Quem decide o que registrar é a pessoa.

---

## 👩‍⚖️ Para quem vai usar (o dia a dia)

É bem direto:

1. **Abra o link** da ferramenta (se houver senha, digite-a).
2. **Clique em "Browse files"** e escolha o PDF dos autos. Pode selecionar mais de um arquivo, se os autos vierem divididos.
3. **Confira o recorte:** o app detecta sozinho onde começa a fase pós-sentença e mostra a página. Se errar, é só ajustar o número em *"Analisar a partir da página"* (ou marcar *"Analisar o documento inteiro"*).
4. **Clique em "Analisar"** e aguarde. Processos grandes podem levar alguns minutos — é normal, pode deixar a aba aberta.
4. **Leia o resultado** que aparece: a situação atual, o próximo passo sugerido e o nível de confiança da IA (🟢 alta, 🟡 média, 🔴 baixa).
5. Se quiser entender o porquê, abra **"Por que a IA sugeriu isso?"**.
6. No bloco **"Para colar na planilha"**, use o **botãozinho de copiar** (no canto do bloco) e cole o texto na coluna *Próximo passo* da sua planilha.

> **Importante:** o resultado é uma **sugestão** para agilizar a triagem. A
> palavra final é sempre do advogado — confira antes de registrar, principalmente
> quando a confiança vier 🟡 ou 🔴.

**Dica de qualidade:** quanto mais recentes e relevantes forem as peças enviadas
(últimas decisões, petições e movimentações), melhor tende a ser a sugestão.
Enviar os autos inteiros funciona, mas às vezes o excesso de páginas antigas
"dilui" a resposta.

---

## 🛠️ Para quem vai instalar e publicar

### O que você precisa
- Uma **chave do Google AI Studio** (https://aistudio.google.com/apikey) — tem **nível gratuito**.
- Uma conta no **GitHub** e outra no **Streamlit Community Cloud** (ambas gratuitas) — só na hora de publicar.

### Testar no seu computador (opcional)
```bash
pip install -r requirements.txt
```
Crie o arquivo `.streamlit/secrets.toml` (copie o `.streamlit/secrets.toml.example`
e preencha a chave). Depois:
```bash
streamlit run streamlit_app.py
```
O navegador abre sozinho em `http://localhost:8501`.

### Publicar (gerar o link para o escritório)
O caminho recomendado é GitHub + Streamlit Community Cloud (faremos o passo a
passo juntos quando você quiser). Em resumo:
1. Suba esta pasta para um repositório no GitHub.
2. No Streamlit Community Cloud, conecte o repositório e aponte para o arquivo `streamlit_app.py`.
3. No painel **Secrets** do app, cole a `GEMINI_API_KEY` (e a `APP_PASSWORD`, se quiser senha).
4. O Streamlit publica e te entrega o link.

### Sobre os segredos (leia isto)
A chave do Gemini e a senha **nunca** ficam escritas no código. Elas vão:
- no arquivo `.streamlit/secrets.toml` (no seu computador), **ou**
- no painel *Secrets* (no Streamlit Cloud).

O arquivo `.gitignore` já está configurado para **impedir** que `secrets.toml`
vá parar no GitHub por acidente. Não remova essa proteção.

### Ajustes rápidos (sem saber programar)
- **Colocar/ tirar senha:** defina (ou apague) `APP_PASSWORD` nos secrets.
- **Tamanho máximo de upload:** está em `.streamlit/config.toml` (`maxUploadSize`, em MB).
- **Trocar o modelo da IA:** defina `GEMINI_MODEL` nos secrets.

---

## ⚠️ Observações honestas
- **Privacidade:** são autos sigilosos. Use a senha (`APP_PASSWORD`) e evite
  deixar o link totalmente aberto. O texto é enviado ao Google (Gemini) apenas para a análise.
  após a análise.
- **A IA erra às vezes:** por isso o nível de confiança fica visível e a revisão
  humana é parte do processo, não um detalhe.
- **Tecnologias mudam:** nomes de modelo do Gemini e detalhes da biblioteca
  podem mudar; se algo parar de funcionar, normalmente é só atualizar o
  `GEMINI_MODEL` ou as dependências.
