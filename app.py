import streamlit as st
import pandas as pd
import fitz  # PyMuPDF
import re
import io
import pytesseract
from pdf2image import convert_from_bytes
from datetime import datetime
from PIL import Image

# --- CONFIGURAÇÃO ---
st.set_page_config(page_title="Extrator Tributário RFB/PGFN", page_icon="⭐", layout="wide")

# --- FUNÇÕES GERAIS DE LIMPEZA ---

def parse_currency(value_str):
    if not value_str: return 0.0
    try:
        clean = str(value_str).replace(" ", "").replace("R$", "")
        clean = re.sub(r'[^\d,\.]', '', clean)
        clean = clean.replace(".", "").replace(",", ".")
        return float(clean)
    except:
        return 0.0

def extrair_municipio(text, filename):
    # 1. Tenta buscar no texto padrões oficias
    match = re.search(r"(?:Munic[íi]pio(?: de)?|Prefeitura(?: Municipal)?(?: de)?)\s+([A-Za-zÀ-ÿ\s\-]+?)(?:\n|-|\d|CNPJ)", text, re.IGNORECASE)
    if match:
        cidade = match.group(1).strip()
        if len(cidade) > 2: return cidade
    
    # 2. Fallback: Usa o nome do arquivo, mas LIMPA a sujeira (Ex: tira "- DARF - PASEP")
    nome_arquivo = re.sub(r'(?i)\.pdf$', '', filename)
    nome_arquivo = re.sub(r'(?i)\s*-\s*(?:DARF|Extrato|PASEP|Simples|Processo|Comprovante).*', '', nome_arquivo)
    return nome_arquivo.strip()

# --- FUNÇÕES PARA EXTRATOS ---

def extrair_processo_extrato(text):
    match_proc = re.search(r"(?:Processo|Negociaç[ãa]o|Conta|Parcelamento).*?[:\.]\s*([\d\.\-]+)", text, re.IGNORECASE)
    if match_proc: return match_proc.group(1).strip()
    return "Não informado"

def inferir_modalidade(text):
    mapa = {
        "EC 113": "Especial EC 113/2021",
        "13.485": "Especial Lei nº 13.485/17 - PREM",
        "12.810": "Lei 12.810 OPP",
        "SIMPLIFICADO": "Parcelamento Simplificado (OPP)",
        "CONVENCIONAL": "Parcelamento Convencional",
        "TRANSACAO EXCEPCIONAL": "Transação Excepcional",
        "EDITAL PGDAU": "Transação por Adesão - PGDAU",
        "PERT": "Pert IIIb",
        "PASEP": "PASEP"
    }
    upper = text.upper()
    for key, val in mapa.items():
        if key in upper: return val
    match_mod = re.search(r"Modalidade[:\s\.]*(.*?)(?=\n)", text, re.IGNORECASE)
    if match_mod: return match_mod.group(1).strip()
    return "Não identificada"

def extrair_saldo_devedor(text):
    patterns = [
        r"Saldo\s*Devedor\s*c(?:om|/)\s*Juros.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Valor\s*total\s*consolidado.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Total\s*Geral.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"(?:Saldo\s*Devedor|Valor\s*Consolidado).*?(?:R\$)?\s*([\d\.]+,\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE | re.DOTALL)
        valores = [parse_currency(m) for m in matches if parse_currency(m) > 0]
        if valores: return max(valores)
    return 0.0

def extrair_ultima_parcela(text):
    patterns = [
        r"(?:Valor da Parcela|Última Parcela|Parcela Básica|Valor Principal).*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Valor\s*(?:Atual|Parcela).*?(?:R\$)?\s*([\d\.]+,\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        if matches: return parse_currency(matches[0])
    return 0.0

def extrair_pagamento_completo(text):
    texto_original = "N/D"
    valor = 0.0
    data = "N/D"
    
    matches = re.findall(r"(R\$\s*[\d\.]+,\d{2}[\s\S]{0,40}?\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if not matches:
        matches = re.findall(r"(\d{2}/\d{2}/\d{4}[\s\S]{0,40}?R\$\s*[\d\.]+,\d{2})", text, re.IGNORECASE)
        
    if matches:
        texto_sujo = matches[-1].replace('\n', ' ').strip()
        val_match = re.search(r"([\d\.]+,\d{2})", texto_sujo)
        if val_match: valor = parse_currency(val_match.group(1))
        
        data_match = re.search(r"(\d{2}/\d{2}/\d{4})", texto_sujo)
        if data_match: data = data_match.group(1)
        
        if val_match and data_match: texto_original = f"R$ {val_match.group(1)} em {data}"
        else: texto_original = texto_sujo

    return texto_original, valor, data

# --- FUNÇÕES ESPECÍFICAS PARA DARF ---

def extrair_processo_darf(text):
    # Procura o Número de Referência do DARF
    match = re.search(r"REFER[EÊ]NCIA[\s\n]*([\d\.\-]+)", text, re.IGNORECASE)
    if match and len(match.group(1)) > 5: return match.group(1).strip()
    # Fallback para CNPJ
    match_cnpj = re.search(r"CPF OU CNPJ[\s\n]*([\d\.\-\/]+)", text, re.IGNORECASE)
    if match_cnpj: return f"CNPJ: {match_cnpj.group(1).strip()}"
    return "Não informado"

def extrair_valor_darf(text):
    # Procura o Item 10: VALOR TOTAL
    match = re.search(r"VALOR TOTAL[\s\n]*(?:R\$)?\s*([\d\.]+,\d{2})", text, re.IGNORECASE)
    if match: return parse_currency(match.group(1))
    # Fallback para VALOR DO PRINCIPAL
    match2 = re.search(r"VALOR DO PRINCIPAL[\s\n]*(?:R\$)?\s*([\d\.]+,\d{2})", text, re.IGNORECASE)
    if match2: return parse_currency(match2.group(1))
    return 0.0

def extrair_data_darf(text):
    match = re.search(r"DATA DE VENCIMENTO[\s\n]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if match: return match.group(1).strip()
    match2 = re.search(r"PER[ÍI]ODO DE APURA[ÇC][ÃA]O[\s\n]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if match2: return match2.group(1).strip()
    return "N/D"

# --- ENGINE OCR ---
def aplicar_ocr(pdf_bytes):
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300)
        full_text = ""
        for img in images:
            text = pytesseract.image_to_string(img, lang='por')
            full_text += text + "\n"
        return full_text
    except: 
        return ""

# --- PROCESSAMENTO PRINCIPAL ---
def processar(uploaded_file):
    filename = uploaded_file.name
    pdf_bytes = uploaded_file.read()
    
    full_text = ""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc: full_text += page.get_text() + "\n"
    except: pass

    if len(full_text.strip()) < 50:
        full_text = aplicar_ocr(pdf_bytes)
    
    # 1. ROTEAMENTO: Identifica se é DARF ou Extrato
    is_darf = "DARF" in full_text.upper() or "DOCUMENTO DE ARRECADA" in full_text.upper() or "DARF" in filename.upper()
    
    municipio = extrair_municipio(full_text, filename)
    
    if is_darf:
        # LÓGICA DARF
        processo = extrair_processo_darf(full_text)
        modalidade = "DARF (Pagamento Isolado)"
        saldo = 0.0 # DARF não possui saldo devedor, apenas o valor da guia
        
        valor_darf = extrair_valor_darf(full_text)
        data_darf = extrair_data_darf(full_text)
        
        # O Valor Total do DARF preenche tanto a Última Parcela quanto o Pagamento
        ultima_parcela = valor_darf
        valor_pagamento = valor_darf
        data_pagamento = data_darf
        
        texto_pag_formatado = f"{valor_darf:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        texto_pagamento = f"R$ {texto_pag_formatado} em {data_darf}" if valor_darf else "N/D"
        
    else:
        # LÓGICA EXTRATO PADRÃO
        processo = extrair_processo_extrato(full_text)
        modalidade = inferir_modalidade(full_text)
        saldo = extrair_saldo_devedor(full_text)
        ultima_parcela = extrair_ultima_parcela(full_text)
        texto_pagamento, valor_pagamento, data_pagamento = extrair_pagamento_completo(full_text)
        
    return {
        "Município": municipio,
        "Processo": processo,
        "Modalidade": modalidade,
        "Saldo Devedor 05/2026": saldo,
        "Última Parcela (BRL)": ultima_parcela,
        "Último Pagamento (Texto Original)": texto_pagamento,
        "Valor Último Pagamento": valor_pagamento,
        "Data Último Pagamento": data_pagamento
    }

# --- INTERFACE WEB (STREAMLIT) ---
st.title("⭐ Extrator Fiscal Automatizado (Extratos + DARFs)")
st.markdown("Agora com roteamento inteligente: extrai dados perfeitamente tanto de **Extratos de Parcelamento** quanto de **Guias DARF** misturadas.")

arquivos = st.file_uploader("Arraste os arquivos PDF aqui", type=["pdf"], accept_multiple_files=True)

if arquivos:
    if st.button("Processar Documentos"):
        dados = []
        bar = st.progress(0)
        
        with st.spinner("Analisando PDFs e extraindo métricas..."):
            for i, arq in enumerate(arquivos):
                res = processar(arq)
                dados.append(res)
                bar.progress((i+1)/len(arquivos))
                
        df = pd.DataFrame(dados)
        st.success("Extração Concluída com Sucesso!")
        
        st.dataframe(df.style.format({
            "Saldo Devedor 05/2026": "R$ {:,.2f}",
            "Última Parcela (BRL)": "R$ {:,.2f}",
            "Valor Último Pagamento": "R$ {:,.2f}"
        }), use_container_width=True)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Base_Tratada')
            ws = writer.sheets['Base_Tratada']
            ws.set_column('A:A', 35) 
            ws.set_column('B:B', 30) 
            ws.set_column('C:C', 45) 
            ws.set_column('D:E', 22) 
            ws.set_column('F:F', 35) 
            ws.set_column('G:H', 20) 
            
        st.download_button(
            label="⬇️ Baixar Base Excel (.xlsx)", 
            data=buffer.getvalue(), 
            file_name=f"Base_Levantamento_{datetime.now().strftime('%d_%m_%H%M')}.xlsx",
            mime="application/vnd.ms-excel"
        )
