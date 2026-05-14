import streamlit as st
import pandas as pd
import fitz  # PyMuPDF
import re
import io
import pytesseract
from pdf2image import convert_from_bytes
from datetime import datetime

# --- CONFIGURAÇÃO ---
st.set_page_config(page_title="Big Data Fiscal & Projeções", page_icon="🚀", layout="wide")

# --- FUNÇÕES MATEMÁTICAS E DE LIMPEZA ---

def parse_currency(value_str):
    if not value_str or value_str == "N/D": return 0.0
    try:
        clean = str(value_str).replace(" ", "").replace("R$", "").strip()
        if len(clean) >= 3 and clean[-3] in [',', '.']:
            cents = clean[-2:]
            reais = clean[:-3].replace('.', '').replace(',', '')
            return float(f"{reais}.{cents}")
        return float(re.sub(r'[^\d]', '', clean)) / 100
    except: return 0.0

def extrair_data_sort(item):
    try: return datetime.strptime(item[0], "%d/%m/%Y")
    except: return datetime.min

def formata_br(x):
    """Formata float para o padrão brasileiro puro visual no Streamlit (Sem R$)"""
    if isinstance(x, (int, float)):
        if pd.isna(x): return ""
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(x)

# --- FUNÇÕES DE EXTRAÇÃO DO PDF ---

def extrair_metrica_parcelas(text):
    concedidas, restantes = 0, 0
    match_total = re.search(r"Quantidade de Parcelas concedidas[:\s]*(\d+)", text, re.IGNORECASE)
    if match_total: concedidas = int(match_total.group(1))
    
    match_rest = re.search(r"Quantidade de Parcelas restantes[:\s]*(\d+)", text, re.IGNORECASE)
    if match_rest: restantes = int(match_rest.group(1))
    
    pagas = concedidas - restantes if concedidas >= restantes else 0
    return concedidas, restantes, pagas

def extrair_data_adesao(text):
    match = re.search(r"Data da (?:Negociaç[ãa]o|consolidaç[ãa]o)[:\s]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if match: return match.group(1)
    return "Não informada"

def extrair_dicionario_historico(text):
    text_clean = re.sub(r'[\r\t]', ' ', text)
    pagamentos = []

    p1 = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
    for m in p1:
        if parse_currency(m[3]) > 0:
            pagamentos.append((m[2], m[3])) 

    if not pagamentos:
        p2 = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
        for m in p2:
            if parse_currency(m[1]) > 0:
                pagamentos.append((m[0], m[1]))

    if not pagamentos:
        return {}, "N/D", 0.0

    pagamentos_unicos = list(dict.fromkeys(pagamentos))
    pagamentos_unicos.sort(key=extrair_data_sort)

    dict_parcelas = {}
    for i, p in enumerate(pagamentos_unicos, start=1):
        # EXTRAI APENAS O VALOR NUMÉRICO FLOAT (Sem data, sem R$)
        dict_parcelas[f"Parcela {i}"] = parse_currency(p[1])

    ultima_data = pagamentos_unicos[-1][0]
    ultimo_valor = parse_currency(pagamentos_unicos[-1][1])

    return dict_parcelas, ultima_data, ultimo_valor

def processar(uploaded_file):
    pdf_bytes = uploaded_file.read()
    full_text = ""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc: full_text += page.get_text() + "\n"
    except: pass

    if len(full_text.strip()) < 50:
        images = convert_from_bytes(pdf_bytes, dpi=300)
        full_text = "\n".join([pytesseract.image_to_string(img, lang='por') for img in images])

    municipio_match = re.search(r"(?:Munic[íi]pio(?: de)?|Prefeitura)\s+([A-Za-zÀ-ÿ\s\-]+?)(?:\n|-|\d|CNPJ)", full_text, re.IGNORECASE)
    municipio = municipio_match.group(1).strip() if municipio_match else uploaded_file.name
    orgao = "PGFN" if "PGFN" in full_text.upper() or "SISPAR" in full_text.upper() else "RFB"
    
    total_conc, restantes, pagas = extrair_metrica_parcelas(full_text)
    data_adesao = extrair_data_adesao(full_text)
    
    saldo_match = re.search(r"Saldo\s*Devedor.*?([\d\.]+[,.]\d{2})", full_text, re.IGNORECASE)
    saldo = parse_currency(saldo_match.group(1)) if saldo_match else 0.0

    parcelas, ultima_data, valor_ultima_parcela = extrair_dicionario_historico(full_text)

    data_quitacao = "N/D"
    if ultima_data != "N/D" and restantes > 0:
        try:
            ud = datetime.strptime(ultima_data, "%d/%m/%Y")
            m = ud.month - 1 + restantes
            y = ud.year + m // 12
            m = m % 12 + 1
            data_quitacao = f"{m:02d}/{y}"
        except: pass
    elif restantes == 0 and ultima_data != "N/D":
        data_quitacao = "Quitado"

    custo_projetado_restante = valor_ultima_parcela * restantes
    projecao_300x = valor_ultima_parcela * 300
    diferenca_defasagem = saldo - custo_projetado_restante

    resultado = {
        "Órgão": orgao,
        "Município": municipio,
        "Data Adesão": data_adesao,
        "Total Concedido": total_conc,
        "Meses Já Pagos": pagas,
        "Parcelas Restantes": restantes,
        "Saldo Devedor Atual": saldo,
        "Valor Última Parcela": valor_ultima_parcela,
        "Estimativa Quitação": data_quitacao,
        "Custo Projetado (Restante)": custo_projetado_restante,
        "Projeção em 300x": projecao_300x,
        "Diferença (Defasagem)": diferenca_defasagem
    }
    resultado.update(parcelas)
    return resultado

# --- INTERFACE WEB (STREAMLIT) ---
st.title("🚀 Big Data Fiscal & Motor de Projeções")

arquivos = st.file_uploader("Suba seus Extratos (PDF)", type=["pdf"], accept_multiple_files=True)

if arquivos and st.button("Auditar, Projetar e Expandir"):
    dados = []
    bar = st.progress(0)
    for i, arq in enumerate(arquivos):
        dados.append(processar(arq))
        bar.progress((i+1)/len(arquivos))
        
    df = pd.DataFrame(dados)
    
    base_cols = [
        "Órgão", "Município", "Data Adesão", "Total Concedido", "Meses Já Pagos", 
        "Parcelas Restantes", "Saldo Devedor Atual", "Valor Última Parcela", 
        "Estimativa Quitação", "Custo Projetado (Restante)", "Projeção em 300x", "Diferença (Defasagem)"
    ]
    parcela_cols = [c for c in df.columns if c.startswith("Parcela ")]
    parcela_cols.sort(key=lambda x: int(x.split(" ")[1])) 
    
    final_cols = base_cols + parcela_cols
    df = df[final_cols]
    
    st.success(f"Matriz de {len(parcela_cols)} colunas gerada com valores puros!")
    
    # --- APLICA FORMATO BRASILEIRO PURO NA TELA ---
    formatos_tela = {
        "Saldo Devedor Atual": formata_br,
        "Valor Última Parcela": formata_br,
        "Custo Projetado (Restante)": formata_br,
        "Projeção em 300x": formata_br,
        "Diferença (Defasagem)": formata_br
    }
    # Aplica o formato puro em todas as infinitas colunas de parcelas também
    for p_col in parcela_cols:
        formatos_tela[p_col] = formata_br

    st.dataframe(df.style.format(formatos_tela), use_container_width=True)
    
    # --- EXPORTAÇÃO EXCEL ---
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Projeções_e_Matriz')
        ws = writer.sheets['Projeções_e_Matriz']
        
        ws.set_column('A:A', 10) 
        ws.set_column('B:B', 30) 
        ws.set_column('C:F', 16) 
        ws.set_column('G:L', 22) 
        if len(parcela_cols) > 0:
            ws.set_column(12, 12 + len(parcela_cols), 16)
            
    st.download_button("⬇️ Baixar Projeções em Excel", buffer.getvalue(), f"Projecao_Fiscal_{datetime.now().strftime('%d_%m_%H%M')}.xlsx")
