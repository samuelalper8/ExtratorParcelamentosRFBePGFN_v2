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
        # Trata o ponto incorreto nos centavos da Receita (Ex: 11.205.34)
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
    """Lê todas as tabelas e retorna: Dicionário de Parcelas, Última Data, Último Valor"""
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

    if not pagamentos:
        return {}, "N/D", 0.0

    # Remover duplicatas que o OCR possa ter gerado acidentalmente
    pagamentos_unicos = list(dict.fromkeys(pagamentos))
    # Ordenar do mais antigo para o mais novo
    pagamentos_unicos.sort(key=extrair_data_sort)

    # Gerar colunas dinâmicas (Parcela 1 até Parcela N)
    dict_parcelas = {}
    for i, p in enumerate(pagamentos_unicos, start=1):
        val_format = p[1][:-3] + "," + p[1][-2:] if p[1][-3] == '.' else p[1]
        dict_parcelas[f"Parcela {i}"] = f"R$ {val_format} em {p[0]}"

    # Capturar a Última Parcela para fazer as projeções matemáticas
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

    # Extrair Histórico e a "Base" para a Projeção
    parcelas, ultima_data, valor_ultima_parcela = extrair_dicionario_historico(full_text)

    # --- MOTOR DE PROJEÇÃO ATÉ A QUITAÇÃO ---
    data_quitacao = "N/D"
    if ultima_data != "N/D" and restantes > 0:
        try:
            # Soma os meses restantes à data do último pagamento para achar o mês da quitação
            ud = datetime.strptime(ultima_data, "%d/%m/%Y")
            m = ud.month - 1 + restantes
            y = ud.year + m // 12
            m = m % 12 + 1
            data_quitacao = f"{m:02d}/{y}"
        except: pass
    elif restantes == 0 and ultima_data != "N/D":
        data_quitacao = "Quitado"

    # Cálculos Financeiros Projetados
    custo_projetado_restante = valor_ultima_parcela * restantes
    projecao_300x = valor_ultima_parcela * 300
    diferenca_defasagem = saldo - custo_projetado_restante

    # Estrutura Base (Tabela Principal)
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
    
    # Injetar a matriz de parcelas horizontal
    resultado.update(parcelas)
    
    return resultado

# --- INTERFACE WEB (STREAMLIT) ---
st.title("🚀 Big Data Fiscal & Motor de Projeções")
st.markdown("O sistema calcula a **Estimativa de Quitação**, projeta o **Custo Final** e expande o histórico completo na Matriz.")

arquivos = st.file_uploader("Suba seus Extratos (PDF)", type=["pdf"], accept_multiple_files=True)

if arquivos and st.button("Auditar, Projetar e Expandir"):
    dados = []
    bar = st.progress(0)
    for i, arq in enumerate(arquivos):
        dados.append(processar(arq))
        bar.progress((i+1)/len(arquivos))
        
    df = pd.DataFrame(dados)
    
    # --- ORDENAÇÃO INTELIGENTE DE COLUNAS ---
    base_cols = [
        "Órgão", "Município", "Data Adesão", "Total Concedido", "Meses Já Pagos", 
        "Parcelas Restantes", "Saldo Devedor Atual", "Valor Última Parcela", 
        "Estimativa Quitação", "Custo Projetado (Restante)", "Projeção em 300x", "Diferença (Defasagem)"
    ]
    parcela_cols = [c for c in df.columns if c.startswith("Parcela ")]
    parcela_cols.sort(key=lambda x: int(x.split(" ")[1])) # Classifica matematicamente (1, 2... 10... 240)
    
    final_cols = base_cols + parcela_cols
    df = df[final_cols]
    
    # Preencher células vazias da matriz com traço
    df.fillna("-", inplace=True)
    
    st.success(f"Projeções e Matriz de {len(parcela_cols)} colunas geradas com sucesso!")
    
    # Exibir na tela as métricas formatadas em BRL
    st.dataframe(df.style.format({
        "Saldo Devedor Atual": "R$ {:,.2f}",
        "Valor Última Parcela": "R$ {:,.2f}",
        "Custo Projetado (Restante)": "R$ {:,.2f}",
        "Projeção em 300x": "R$ {:,.2f}",
        "Diferença (Defasagem)": "R$ {:,.2f}"
    }), use_container_width=True)
    
    # --- EXPORTAÇÃO EXCEL BLINDADA ---
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Projeções_e_Matriz')
        ws = writer.sheets['Projeções_e_Matriz']
        
        # Formatar larguras (Ajustado para o novo número de colunas)
        ws.set_column('A:A', 10) # Órgão
        ws.set_column('B:B', 30) # Município
        ws.set_column('C:F', 16) # Métricas Básicas
        ws.set_column('G:L', 22) # Valores de Saldo e Projeções Financeiras
        
        # Formatar matriz infinita
        if len(parcela_cols) > 0:
            ws.set_column(12, 12 + len(parcela_cols), 25) # Expande todas as parcelas
            
    st.download_button("⬇️ Baixar Projeções em Excel", buffer.getvalue(), f"Projecao_Fiscal_{datetime.now().strftime('%d_%m_%H%M')}.xlsx")
