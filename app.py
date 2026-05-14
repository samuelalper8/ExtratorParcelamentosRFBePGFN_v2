import streamlit as st
import pandas as pd
import fitz  # PyMuPDF
import re
import io
import pytesseract
from pdf2image import convert_from_bytes
from datetime import datetime

# --- CONFIGURAÇÃO ---
st.set_page_config(page_title="Big Data Fiscal RFB/PGFN", page_icon="📈", layout="wide")

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
    """Auxilia na ordenação cronológica das parcelas extraídas"""
    try: return datetime.strptime(item[0], "%d/%m/%Y")
    except: return datetime.min

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
    """Lê todas as tabelas e cria um dicionário {Parcela 1: Valor, Parcela 2: Valor...}"""
    text_clean = re.sub(r'[\r\t]', ' ', text)
    pagamentos = []

    # Padrão 1: RFB Convencional/OPP (Vencimento, Parcela, Pgto, Pago)
    p1 = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
    for m in p1:
        if parse_currency(m[3]) > 0:
            pagamentos.append((m[2], m[3])) # (Data Pgto, Valor Pago)

    # Padrão 2: RFB Arrecadação/PERT (Pgto, Valor Total, Amort, Juros)
    if not pagamentos:
        p2 = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
        for m in p2:
            if parse_currency(m[1]) > 0:
                pagamentos.append((m[0], m[1]))

    # Remover duplicatas que o OCR possa ter gerado acidentalmente
    pagamentos_unicos = list(dict.fromkeys(pagamentos))
    
    # Ordenar do mais antigo para o mais novo
    pagamentos_unicos.sort(key=extrair_data_sort)

    # Gerar colunas dinâmicas (Parcela 1 até Parcela N)
    dict_parcelas = {}
    for i, p in enumerate(pagamentos_unicos, start=1):
        # Arruma casas decimais se tiver ponto invés de vírgula
        val_format = p[1][:-3] + "," + p[1][-2:] if p[1][-3] == '.' else p[1]
        dict_parcelas[f"Parcela {i}"] = f"R$ {val_format} em {p[0]}"

    return dict_parcelas

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

    # Estrutura Base
    resultado = {
        "Órgão": orgao,
        "Município": municipio,
        "Data Adesão": data_adesao,
        "Total Concedido": total_conc,
        "Meses Já Pagos": pagas,
        "Parcelas Restantes": restantes,
        "Saldo Devedor 05/2026": saldo,
    }
    
    # Injetar matriz dinâmica de parcelas (expandir para a direita)
    parcelas = extrair_dicionario_historico(full_text)
    resultado.update(parcelas)
    
    return resultado

# --- INTERFACE WEB (STREAMLIT) ---
st.title("⭐ Matriz Dinâmica Fiscal: Histórico Completo")
st.markdown("O sistema expandirá colunas infinitamente para a direita mapeando todas as parcelas pagas de cada Município.")

arquivos = st.file_uploader("Suba seus Extratos (PDF)", type=["pdf"], accept_multiple_files=True)

if arquivos and st.button("Auditar Documentos e Expandir Matriz"):
    dados = []
    bar = st.progress(0)
    for i, arq in enumerate(arquivos):
        dados.append(processar(arq))
        bar.progress((i+1)/len(arquivos))
        
    df = pd.DataFrame(dados)
    
    # --- ORDENAÇÃO INTELIGENTE DE COLUNAS ---
    # Garante que as colunas fiquem na ordem matemática: Parcela 1, Parcela 2, ..., Parcela 240 (e não Parcela 1, 10, 100, 2)
    base_cols = ["Órgão", "Município", "Data Adesão", "Total Concedido", "Meses Já Pagos", "Parcelas Restantes", "Saldo Devedor 05/2026"]
    parcela_cols = [c for c in df.columns if c.startswith("Parcela ")]
    parcela_cols.sort(key=lambda x: int(x.split(" ")[1])) # Classifica pelo número
    
    final_cols = base_cols + parcela_cols
    df = df[final_cols]
    
    # Preencher células vazias (quando um município tem 10 parcelas e o outro tem 100)
    df.fillna("-", inplace=True)
    
    st.success(f"Extração Concluída! Maior parcelamento detectado: {len(parcela_cols)} colunas de pagamento.")
    st.dataframe(df)
    
    # --- EXPORTAÇÃO EXCEL PROFISSIONAL ---
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Matriz_Expandida')
        ws = writer.sheets['Matriz_Expandida']
        
        # Formata colunas base
        ws.set_column('A:A', 10) # Órgão
        ws.set_column('B:B', 30) # Município
        ws.set_column('C:F', 16) # Métricas
        ws.set_column('G:G', 22) # Saldo Devedor
        
        # Formata todas as centenas de colunas de parcelas dinamicamente
        if len(parcela_cols) > 0:
            ws.set_column(7, 7 + len(parcela_cols), 25) # Da coluna H (índice 7) até o final
            
    st.download_button("⬇️ Baixar Matriz em Excel", buffer.getvalue(), f"Matriz_Fiscal_{datetime.now().strftime('%d_%m_%H%M')}.xlsx")
