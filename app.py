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

# --- FUNÇÕES DE LIMPEZA E EXTRAÇÃO ---

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
    # 1. Tenta buscar no texto padrões como "Município de X" ou "Prefeitura Municipal de Y"
    match = re.search(r"(?:Munic[íi]pio(?: de)?|Prefeitura(?: Municipal)?(?: de)?)\s+([A-Za-zÀ-ÿ\s\-]+?)(?:\n|-|\d|CNPJ)", text, re.IGNORECASE)
    if match:
        cidade = match.group(1).strip()
        if len(cidade) > 2: return cidade
    
    # 2. Fallback: Usa o nome do arquivo (limpando a extensão)
    nome_arquivo = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE).strip()
    return nome_arquivo

def extrair_processo(text):
    # Tenta achar Processo ou Número de Negociação
    match_proc = re.search(r"(?:Processo|Negociaç[ãa]o|Conta|Parcelamento).*?[:\.]\s*([\d\.\-]+)", text, re.IGNORECASE)
    if match_proc: return match_proc.group(1).strip()
    return "Não informado"

def inferir_modalidade(text):
    # Mapeamento com base nas suas planilhas de GO, MS e TO
    mapa = {
        "EC 113": "Especial EC 113/2021",
        "13.485": "Especial Lei nº 13.485/17 - PREM",
        "12.810": "Lei 12.810 OPP",
        "SIMPLIFICADO": "Parcelamento Simplificado (OPP)",
        "CONVENCIONAL": "Parcelamento Convencional",
        "TRANSACAO EXCEPCIONAL": "Transação Excepcional",
        "EDITAL PGDAU": "Transação por Adesão - PGDAU",
        "PERT": "Pert III",
        "PASEP": "PASEP"
    }
    upper = text.upper()
    for key, val in mapa.items():
        if key in upper: return val
        
    # Se não achar pelos padrões acima, tenta capturar a linha da Modalidade
    match_mod = re.search(r"Modalidade[:\s\.]*(.*?)(?=\n)", text, re.IGNORECASE)
    if match_mod: return match_mod.group(1).strip()
    
    return "Não identificada"

def extrair_saldo_devedor(text):
    patterns = [
        r"Saldo\s*Devedor\s*c(?:om|/)\s*Juros.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Valor\s*total\s*consolidado.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Total\s*Geral.*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"(?:Saldo\s*Devedor|Valor\s*Consolidado).*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Total.*?(?:R\$)?.*?([\d\.]+,\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE | re.DOTALL)
        valores_validos = [parse_currency(m) for m in matches if parse_currency(m) > 0]
        if valores_validos:
            return max(valores_validos) # Retorna o maior valor confiável
    return 0.0

def extrair_ultima_parcela(text):
    # Procura valores associados a parcelas
    patterns = [
        r"(?:Valor da Parcela|Última Parcela|Parcela Básica|Valor Principal).*?(?:R\$)?\s*([\d\.]+,\d{2})",
        r"Valor\s*(?:Atual|Parcela).*?(?:R\$)?\s*([\d\.]+,\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        if matches:
            return parse_currency(matches[0])
    return 0.0

def extrair_pagamento_completo(text):
    texto_original = "N/D"
    valor = 0.0
    data = "N/D"
    
    # Procura por proximidade entre Valor (R$) e Data (DD/MM/AAAA) - Distância de até 40 caracteres
    # Padrão 1: R$ xxx,xx ... DD/MM/YYYY
    matches = re.findall(r"(R\$\s*[\d\.]+,\d{2}[\s\S]{0,40}?\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if not matches:
        # Padrão 2: DD/MM/YYYY ... R$ xxx,xx
        matches = re.findall(r"(\d{2}/\d{2}/\d{4}[\s\S]{0,40}?R\$\s*[\d\.]+,\d{2})", text, re.IGNORECASE)
        
    if matches:
        # Geralmente o último pagamento está no final do PDF, então pegamos a última ocorrência [-1]
        texto_sujo = matches[-1].replace('\n', ' ').strip()
        
        # Extrai os sub-elementos para as colunas matemáticas
        val_match = re.search(r"([\d\.]+,\d{2})", texto_sujo)
        if val_match: valor = parse_currency(val_match.group(1))
        
        data_match = re.search(r"(\d{2}/\d{2}/\d{4})", texto_sujo)
        if data_match: data = data_match.group(1)
        
        # Reconstrói a frase de forma limpa para a coluna de Texto Original
        if val_match and data_match:
            texto_original = f"R$ {val_match.group(1)} em {data}"
        else:
            texto_original = texto_sujo

    return texto_original, valor, data

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
    
    # 1. Leitura Nativa
    full_text = ""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc: full_text += page.get_text() + "\n"
    except: pass

    # 2. Leitura OCR (Se o texto nativo vier vazio ou blindado)
    if len(full_text.strip()) < 50:
        full_text = aplicar_ocr(pdf_bytes)
    
    # 3. Executa as Funções de Extração
    texto_pagamento, valor_pagamento, data_pagamento = extrair_pagamento_completo(full_text)
    
    # 4. Retorna o Dicionário já mapeado para o seu novo Layout de Colunas
    return {
        "Município": extrair_municipio(full_text, filename),
        "Processo": extrair_processo(full_text),
        "Modalidade": inferir_modalidade(full_text),
        "Saldo Devedor 05/2026": extrair_saldo_devedor(full_text),
        "Última Parcela (BRL)": extrair_ultima_parcela(full_text),
        "Último Pagamento (Texto Original)": texto_pagamento,
        "Valor Último Pagamento": valor_pagamento,
        "Data Último Pagamento": data_pagamento
    }

# --- INTERFACE WEB (STREAMLIT) ---
st.title("⭐ Extrator Fiscal Automatizado")
st.markdown("Extração estruturada de Extratos PGFN e RFB. Os dados já sairão formatados para cruzamento no Power Query/Excel.")

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
        
        # Formatação Visual da Tabela na Tela
        st.dataframe(df.style.format({
            "Saldo Devedor 05/2026": "R$ {:,.2f}",
            "Última Parcela (BRL)": "R$ {:,.2f}",
            "Valor Último Pagamento": "R$ {:,.2f}"
        }), use_container_width=True)
        
        # Métricas Globais
        col1, col2 = st.columns(2)
        col1.metric("Total Saldo Devedor Encontrado", f"R$ {df['Saldo Devedor 05/2026'].sum():,.2f}")
        col2.metric("Documentos Lidos", len(df))
        
        # Geração do Arquivo Excel (XLSX)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Base_Tratada')
            
            # Ajustando a largura das colunas do Excel automaticamente
            ws = writer.sheets['Base_Tratada']
            ws.set_column('A:A', 35) # Município
            ws.set_column('B:B', 30) # Processo
            ws.set_column('C:C', 45) # Modalidade
            ws.set_column('D:E', 22) # Saldo e Parcela
            ws.set_column('F:F', 35) # Txt Pagamento
            ws.set_column('G:H', 20) # Valor e Data Pagamento
            
        st.download_button(
            label="⬇️ Baixar Base Excel (.xlsx)", 
            data=buffer.getvalue(), 
            file_name=f"Base_Levantamento_{datetime.now().strftime('%d_%m_%H%M')}.xlsx",
            mime="application/vnd.ms-excel"
        )