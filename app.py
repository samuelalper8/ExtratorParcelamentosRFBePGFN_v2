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

# --- FUNÇÕES MATEMÁTICAS (BLINDAGEM NUMÉRICA) ---

def parse_currency(value_str):
    """Garante que a saída seja estritamente um número FLOAT, sem texto ou data"""
    if not value_str or pd.isna(value_str): return 0.0
    try:
        text_val = str(value_str).split(" em ")[0].split(" ")[0].upper()
        clean = text_val.replace("R$", "").strip()
        clean = re.sub(r'[^\d,\.]', '', clean)
        
        # Corrige ponto isolado nos centavos
        if len(clean) >= 3 and clean[-3] in [',', '.']:
            cents = clean[-2:]
            reais = clean[:-3].replace('.', '').replace(',', '')
            return float(f"{reais}.{cents}")
        elif len(clean) > 0:
            return float(re.sub(r'[^\d]', '', clean)) / 100
        return 0.0
    except: return 0.0

def extrair_data_sort(item):
    try: return datetime.strptime(item[0], "%d/%m/%Y")
    except: return datetime.min

def formata_br(x):
    if isinstance(x, (int, float)) and pd.notna(x):
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ""

# --- FUNÇÕES DE EXTRAÇÃO DO PDF ---

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
    """Procura o saldo devedor pulando quebras de linha e ignorando lixo do PDF"""
    patterns = [
        r"Saldo\s*Devedor\s*do\s*Parcelamento[\s\S]{0,40}?([\d\.]+[,.]\d{2})",
        r"Saldo\s*devedor\s*em[\s\S]{0,40}?([\d\.]+[,.]\d{2})",
        r"Saldo\s*Devedor\s*c(?:om|/)\s*Juros[\s\S]{0,40}?([\d\.]+[,.]\d{2})",
        r"Valor\s*total\s*consolidado[\s\S]{0,40}?([\d\.]+[,.]\d{2})",
        r"Total\s*Geral[\s\S]{0,40}?([\d\.]+[,.]\d{2})",
        r"Saldo\s*Devedor[\s\S]{0,40}?([\d\.]+[,.]\d{2})"
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        valores = [parse_currency(m) for m in matches if parse_currency(m) > 0]
        if valores: return max(valores) # Retorna o maior valor encontrado nesse padrão
    return 0.0

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
        val = parse_currency(m[3])
        if val > 0: pagamentos.append((m[2], val)) 

    if not pagamentos:
        p2 = re.findall(r"(\d{2}/\d{2}/\d{4})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})[\s\n]+([\d\.]+[,.]\d{2})", text_clean)
        for m in p2:
            val = parse_currency(m[1])
            if val > 0: pagamentos.append((m[0], val))

    if not pagamentos: return {}, "N/D", 0.0

    unique_dict = { (d, v): True for d, v in pagamentos }
    pagamentos_unicos = list(unique_dict.keys())
    pagamentos_unicos.sort(key=extrair_data_sort)

    dict_parcelas = {}
    for i, p in enumerate(pagamentos_unicos, start=1):
        dict_parcelas[f"Parcela {i}"] = p[1] 

    ultima_data = pagamentos_unicos[-1][0]
    ultimo_valor = pagamentos_unicos[-1][1]

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

    is_darf = "DARF" in full_text.upper() or "DOCUMENTO DE ARRECADA" in full_text.upper() or "DARF" in uploaded_file.name.upper()

    municipio_match = re.search(r"(?:Munic[íi]pio(?: de)?|Prefeitura)\s+([A-Za-zÀ-ÿ\s\-]+?)(?:\n|-|\d|CNPJ)", full_text, re.IGNORECASE)
    municipio = municipio_match.group(1).strip() if municipio_match else re.sub(r'(?i)\.pdf$|\s*-\s*(?:DARF|Extrato|PASEP).*', '', uploaded_file.name).strip()
    
    orgao = "PGFN" if "PGFN" in full_text.upper() or "SISPAR" in full_text.upper() else "RFB"

    if is_darf:
        processo = "DARF/Guia"
        modalidade = "Pagamento Isolado"
        data_adesao = "N/D"
        total_conc, restantes, pagas = 0, 0, 1
        saldo = 0.0
        
        match_v = re.search(r"VALOR TOTAL[\s\n]*(?:R\$)?\s*([\d\.]+[,.]\d{2})", full_text, re.IGNORECASE)
        valor_ultima_parcela = parse_currency(match_v.group(1)) if match_v else 0.0
        
        match_d = re.search(r"DATA DE VENCIMENTO[\s\n]*(\d{2}/\d{2}/\d{4})", full_text, re.IGNORECASE)
        ultima_data = match_d.group(1).strip() if match_d else "N/D"
        
        parcelas = {"Parcela 1": valor_ultima_parcela}
    else:
        processo = extrair_processo_extrato(full_text)
        modalidade = inferir_modalidade(full_text)
        total_conc, restantes, pagas = extrair_metrica_parcelas(full_text)
        data_adesao = extrair_data_adesao(full_text)
        
        # <<< Restauração da Busca de Saldo Blindada >>>
        saldo = extrair_saldo_devedor(full_text)

        parcelas, ultima_data, valor_ultima_parcela = extrair_dicionario_historico(full_text)

    # Projeção de Quitação
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
        "Processo": processo,
        "Modalidade": modalidade,
        "Data Adesão": data_adesao,
        "Total Concedido": total_conc,
        "Meses Já Pagos": pagas,
        "Parcelas Restantes": restantes,
        "Data Último Pgto": ultima_data, 
        "Estimativa Quitação": data_quitacao,
        "Saldo Devedor Atual": saldo, 
        "Valor Última Parcela": valor_ultima_parcela, 
        "Custo Projetado (Restante)": custo_projetado_restante, 
        "Projeção em 300x": projecao_300x, 
        "Diferença (Defasagem)": diferenca_defasagem 
    }
    
    resultado.update(parcelas)
    return resultado

# --- INTERFACE WEB (STREAMLIT) ---
st.title("🚀 Big Data Fiscal & Motor de Projeções (Valores Puros)")

arquivos = st.file_uploader("Suba seus Extratos e DARFs (PDF)", type=["pdf"], accept_multiple_files=True)

if arquivos and st.button("Processar Dados Estruturados"):
    dados = []
    bar = st.progress(0)
    for i, arq in enumerate(arquivos):
        dados.append(processar(arq))
        bar.progress((i+1)/len(arquivos))
        
    df = pd.DataFrame(dados)
    
    base_cols = [
        "Órgão", "Município", "Processo", "Modalidade", "Data Adesão", "Total Concedido", 
        "Meses Já Pagos", "Parcelas Restantes", "Data Último Pgto", "Estimativa Quitação", 
        "Saldo Devedor Atual", "Valor Última Parcela", "Custo Projetado (Restante)", 
        "Projeção em 300x", "Diferença (Defasagem)"
    ]
    parcela_cols = [c for c in df.columns if c.startswith("Parcela ")]
    parcela_cols.sort(key=lambda x: int(x.split(" ")[1])) 
    
    final_cols = base_cols + parcela_cols
    df = df[final_cols]
    
    df.fillna(value=pd.NA, inplace=True)
    
    st.success("Tabela Numérica Pura gerada com Sucesso!")
    
    formatos_tela = {
        "Saldo Devedor Atual": formata_br,
        "Valor Última Parcela": formata_br,
        "Custo Projetado (Restante)": formata_br,
        "Projeção em 300x": formata_br,
        "Diferença (Defasagem)": formata_br
    }
    for p_col in parcela_cols:
        formatos_tela[p_col] = formata_br

    st.dataframe(df.style.format(formatos_tela, na_rep=""), use_container_width=True)
    
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Projeções_e_Matriz')
        ws = writer.sheets['Projeções_e_Matriz']
        
        ws.set_column('A:A', 10) 
        ws.set_column('B:D', 25) 
        ws.set_column('E:J', 16) 
        ws.set_column('K:O', 20) 
        if len(parcela_cols) > 0:
            ws.set_column(15, 15 + len(parcela_cols), 15)
            
    st.download_button("⬇️ Baixar Projeções em Excel (.xlsx)", buffer.getvalue(), f"Projecao_Pura_{datetime.now().strftime('%d_%m_%H%M')}.xlsx")
