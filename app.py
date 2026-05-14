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
        # Remove espaços e R$
        clean = str(value_str).replace(" ", "").replace("R$", "").strip()
        # Verifica se o último separador (centavos) é ponto ou vírgula
        if len(clean) >= 3 and clean[-3] in [',', '.']:
            cents = clean[-2:]
            reais = clean[:-3].replace('.', '').replace(',', '')
            return float(f"{reais}.{cents}")
        else:
            # Sem centavos explícitos
            clean = re.sub(r'[^\d]', '', clean)
            return float(clean)
    except:
        return 0.0

def extrair_municipio(text, filename):
    match = re.search(r"(?:Munic[íi]pio(?: de)?|Prefeitura(?: Municipal)?(?: de)?)\s+([A-Za-zÀ-ÿ\s\-]+?)(?:\n|-|\d|CNPJ)", text, re.IGNORECASE)
    if match:
        cidade = match.group(1).strip()
        if len(cidade) > 2: return cidade
    
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
        r"Saldo\s*Devedor\s*c(?:om|/)\s*Juros.*?(?:R\$)?\s*([\d\.]+[,.]\d{2})",
        r"Valor\s*total\s*consolidado.*?(?:R\$)?\s*([\d\.]+[,.]\d{2})",
        r"Total\s*Geral.*?(?:R\$)?\s*([\d\.]+[,.]\d{2})",
        r"(?:Saldo\s*Devedor|Valor\s*Consolidado).*?(?:R\$)?\s*([\d\.]+[,.]\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE | re.DOTALL)
        valores = [parse_currency(m) for m in matches if parse_currency(m) > 0]
        if valores: return max(valores)
    return 0.0

def extrair_parcela_e_pagamento(text):
    """
    Função avançada que escaneia as tabelas brutas do PDF para achar o último pagamento real.
    """
    ultima_parcela = 0.0
    valor_pago = 0.0
    data_pagamento = "N/D"
    texto_original = "N/D"

    # Limpa quebras de linha irregulares mantendo espaços
    text_clean = re.sub(r'[\r\t]', ' ', text)

    # Padrão Tabela 1 (Extrato Convencional/OPP): Data Venc -> Parcela -> Data Pgto -> Valor Pago
    padrao_completo = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
    if padrao_completo:
        # Lê a tabela de trás pra frente e pega a última parcela efetivamente paga
        for match in reversed(padrao_completo):
            v_pago = parse_currency(match[3])
            if v_pago > 0:
                ultima_parcela = parse_currency(match[1])
                data_pagamento = match[2]
                valor_pago = v_pago
                # Arruma formatação caso o PDF tenha trazido 11.205.34 em vez de 11.205,34 no texto
                txt_valor = match[3][:-3] + "," + match[3][-2:] if match[3][-3] == '.' else match[3]
                texto_original = f"R$ {txt_valor} em {data_pagamento}"
                return ultima_parcela, texto_original, valor_pago, data_pagamento

    # Padrão Tabela 2 (Extrato Arrecadação/PERT): Data Pgto -> Valor Total -> Amortizado -> Juros
    padrao_simplificado = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
    if padrao_simplificado:
        for match in reversed(padrao_simplificado):
            v_pago = parse_currency(match[1])
            if v_pago > 0:
                ultima_parcela = v_pago
                data_pagamento = match[0]
                valor_pago = v_pago
                txt_valor = match[1][:-3] + "," + match[1][-2:] if match[1][-3] == '.' else match[1]
                texto_original = f"R$ {txt_valor} em {data_pagamento}"
                return ultima_parcela, texto_original, valor_pago, data_pagamento
    
    # Padrão 3 (Fallback / Textos OCR Sujos): Busca proximidade de R$ e Datas
    matches_prox = re.findall(r"(R\$\s*[\d\.]+[,.]\d{2}[\s\S]{0,40}?\d{2}/\d{2}/\d{4})", text_clean, re.IGNORECASE)
    if not matches_prox:
        matches_prox = re.findall(r"(\d{2}/\d{2}/\d{4}[\s\S]{0,40}?R\$\s*[\d\.]+[,.]\d{2})", text_clean, re.IGNORECASE)
    
    if matches_prox:
        texto_sujo = matches_prox[-1].replace('\n', ' ').strip()
        val_m = re.search(r"([\d\.]+[,.]\d{2})", texto_sujo)
        dat_m = re.search(r"(\d{2}/\d{2}/\d{4})", texto_sujo)
        if val_m and dat_m:
            ultima_parcela = parse_currency(val_m.group(1))
            valor_pago = ultima_parcela
            data_pagamento = dat_m.group(1)
            txt_valor = val_m.group(1)[:-3] + "," + val_m.group(1)[-2:] if val_m.group(1)[-3] == '.' else val_m.group(1)
            texto_original = f"R$ {txt_valor} em {data_pagamento}"
            return ultima_parcela, texto_original, valor_pago, data_pagamento

    # Se nada funcionar
    return 0.0, "N/D", 0.0, "N/D"


# --- FUNÇÕES ESPECÍFICAS PARA DARF ---

def extrair_processo_darf(text):
    match = re.search(r"REFER[EÊ]NCIA[\s\n]*([\d\.\-]+)", text, re.IGNORECASE)
    if match and len(match.group(1)) > 5: return match.group(1).strip()
    match_cnpj = re.search(r"CPF OU CNPJ[\s\n]*([\d\.\-\/]+)", text, re.IGNORECASE)
    if match_cnpj: return f"CNPJ: {match_cnpj.group(1).strip()}"
    return "Não informado"

def extrair_valor_darf(text):
    match = re.search(r"VALOR TOTAL[\s\n]*(?:R\$)?\s*([\d\.]+[,.]\d{2})", text, re.IGNORECASE)
    if match: return parse_currency(match.group(1))
    match2 = re.search(r"VALOR DO PRINCIPAL[\s\n]*(?:R\$)?\s*([\d\.]+[,.]\d{2})", text, re.IGNORECASE)
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
        processo = extrair_processo_darf(full_text)
        modalidade = "DARF (Pagamento Isolado)"
        saldo = 0.0
        
        valor_darf = extrair_valor_darf(full_text)
        data_darf = extrair_data_darf(full_text)
        
        ultima_parcela = valor_darf
        valor_pagamento = valor_darf
        data_pagamento = data_darf
        
        texto_pag_formatado = f"{valor_darf:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        texto_pagamento = f"R$ {texto_pag_formatado} em {data_darf}" if valor_darf else "N/D"
        
    else:
        processo = extrair_processo_extrato(full_text)
        modalidade = inferir_modalidade(full_text)
        saldo = extrair_saldo_devedor(full_text)
        # Novo método que extrai as 4 colunas de uma vez lendo a tabela
        ultima_parcela, texto_pagamento, valor_pagamento, data_pagamento = extrair_parcela_e_pagamento(full_text)
        
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
st.title("⭐ Extrator Fiscal Automatizado (Tabelas Blindadas)")
st.markdown("Algoritmo ajustado para varrer tabelas sem 'R$' e ignorar pontuação incorreta em centavos (ex: 11.205.34).")

arquivos = st.file_uploader("Arraste os arquivos PDF aqui", type=["pdf"], accept_multiple_files=True)

if arquivos:
    if st.button("Processar Documentos"):
        dados = []
        bar = st.progress(0)
        
        with st.spinner("Analisando Tabelas e extraindo métricas..."):
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
