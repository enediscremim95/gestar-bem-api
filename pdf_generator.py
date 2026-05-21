# -*- coding: utf-8 -*-
"""
pdf_generator.py — Gestar Bem
Gera o PDF do plano personalizado com layout identico ao modelo aprovado.
Chamado pelo main.py do Fly.io com os dados do formulario e o texto do Claude.
"""
import os, io, re, base64, unicodedata, html as _html

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT

# ── Caminhos das imagens (relativo ao arquivo) ─────────────────────────────────
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
LOGO_ILUSTRACAO   = os.path.join(BASE_DIR, "images", "gestar_ilustracao.png")
LOGO_WATERMARK    = os.path.join(BASE_DIR, "images", "gestar_logo_transparente.png")
GESTAR_BEM_SCRIPT = os.path.join(BASE_DIR, "images", "gestar_bem_svg.png")

# ── Cores ─────────────────────────────────────────────────────────────────────
COR_ROXA     = HexColor('#9B27AF')
COR_VERDE    = HexColor('#16A34A')
COR_VERMELHO = HexColor('#DC2626')
COR_DOURADA  = HexColor('#C4A882')
COR_TEXTO    = HexColor('#2D2D2D')
COR_FOOTER   = HexColor('#888888')
COR_CINZA    = HexColor('#555555')

# ── Medidas ────────────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4
MARGEM         = 1.5 * cm
HEADER_HEIGHT  = 4.5 * cm
FOOTER_HEIGHT  = 1.1 * cm

LOGO_RATIO = 260 / 146     # gestar_ilustracao.png  (260x146 — largura/altura)
GB_RATIO   = 1053 / 269    # gestar_bem_svg.png     (1053x269)
WM_RATIO   = 146 / 260     # gestar_logo_transparente.png (260x146 — altura/largura)
# NOTA: logo antiga era 2475x2730 (quase quadrada). Nova e 260x146 (paisagem).
# Ajuste de tamanhos feito em 20/05/2026 para compensar nova proporcao.


# ══════════════════════════════════════════════════════════════════════════════
# FUNDO, HEADER e FOOTER (desenhados em TODAS as paginas)
# ══════════════════════════════════════════════════════════════════════════════

def draw_background(canvas, doc):
    canvas.saveState()

    # Fundo branco
    canvas.setFillColor(HexColor('#FFFFFF'))
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Watermark (7% opacidade)
    if os.path.exists(LOGO_WATERMARK):
        canvas.setFillAlpha(0.07)
        wm_w = PAGE_W - 60  # quase full-width
        wm_h = wm_w * WM_RATIO
        wm_x = (PAGE_W - wm_w) / 2
        wm_y = (PAGE_H - wm_h) / 2  # centralizado verticalmente na pagina
        canvas.drawImage(
            LOGO_WATERMARK,
            wm_x, wm_y,
            width=wm_w, height=wm_h, mask='auto'
        )
        canvas.setFillAlpha(1.0)

    canvas.restoreState()
    draw_header(canvas, doc)
    draw_footer(canvas, doc)


def draw_header(canvas, doc):
    canvas.saveState()

    # Icone ilustracao — logo nova 260x146 (paisagem), aumentar altura
    logo_h = 85.0
    logo_w = logo_h * LOGO_RATIO
    logo_x = MARGEM
    logo_y = PAGE_H - logo_h - 2

    if os.path.exists(LOGO_ILUSTRACAO):
        canvas.drawImage(
            LOGO_ILUSTRACAO, logo_x, logo_y,
            width=logo_w, height=logo_h,
            mask='auto', preserveAspectRatio=True
        )

    # "Gestar Bem" cursiva — reduzida para nao sair da borda
    gb_h = 70.0
    gb_w = gb_h * GB_RATIO
    gb_x = logo_x + logo_w + 0.3 * cm
    gb_y = logo_y + (logo_h - gb_h) / 2  # centralizado verticalmente com a logo

    if os.path.exists(GESTAR_BEM_SCRIPT):
        canvas.drawImage(
            GESTAR_BEM_SCRIPT, gb_x, gb_y,
            width=gb_w, height=gb_h,
            mask='auto', preserveAspectRatio=True
        )

    # Tagline
    tagline_y = PAGE_H - 103 - 8
    canvas.setFont('Helvetica', 9)
    canvas.setFillColor(COR_CINZA)
    canvas.drawCentredString(PAGE_W / 2, tagline_y,
                             'Se cuidar é o melhor presente para seu bebê.')

    # Linha dourada separadora
    canvas.setStrokeColor(COR_DOURADA)
    canvas.setLineWidth(1.0)
    canvas.line(MARGEM, PAGE_H - 126.5, PAGE_W - MARGEM, PAGE_H - 126.5)

    canvas.restoreState()


def draw_footer(canvas, doc):
    canvas.saveState()

    canvas.setStrokeColor(COR_DOURADA)
    canvas.setLineWidth(0.8)
    canvas.line(MARGEM, FOOTER_HEIGHT, PAGE_W - MARGEM, FOOTER_HEIGHT)

    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(COR_FOOTER)
    canvas.drawCentredString(
        PAGE_W / 2, FOOTER_HEIGHT / 2 - 2,
        '(41) 99992-0539  |  @gestarbem_'
    )

    canvas.restoreState()


# ══════════════════════════════════════════════════════════════════════════════
# ESTILOS
# ══════════════════════════════════════════════════════════════════════════════

def criar_estilos():
    return {
        'normal': ParagraphStyle('NormalGB',
            fontName='Helvetica', fontSize=9.5, leading=14,
            textColor=COR_TEXTO, spaceAfter=4),

        'justificado': ParagraphStyle('JustGB',
            fontName='Helvetica', fontSize=9.5, leading=14,
            textColor=COR_TEXTO, spaceAfter=6, alignment=TA_JUSTIFY),

        'titulo_roxo': ParagraphStyle('TituloRoxo',
            fontName='Helvetica-Bold', fontSize=12, leading=16,
            textColor=COR_ROXA, spaceBefore=10, spaceAfter=6),

        'titulo_roxo_grande': ParagraphStyle('TituloRoxoGrande',
            fontName='Helvetica-Bold', fontSize=14, leading=18,
            textColor=COR_ROXA, spaceBefore=12, spaceAfter=8,
            alignment=TA_CENTER),

        'subtitulo': ParagraphStyle('SubtituloGB',
            fontName='Helvetica-Bold', fontSize=10, leading=14,
            textColor=COR_TEXTO, spaceBefore=8, spaceAfter=4),

        'bullet': ParagraphStyle('BulletGB',
            fontName='Helvetica', fontSize=9.2, leading=13.5,
            textColor=COR_TEXTO, spaceAfter=3, leftIndent=14),

        'bullet_verde': ParagraphStyle('BulletVerde',
            fontName='Helvetica', fontSize=9.2, leading=13.5,
            textColor=COR_VERDE, spaceAfter=3, leftIndent=14),

        'bullet_vermelho': ParagraphStyle('BulletVermelho',
            fontName='Helvetica', fontSize=9.2, leading=13.5,
            textColor=COR_VERMELHO, spaceAfter=3, leftIndent=14),

        'alerta_vermelho': ParagraphStyle('AlertaVermelho',
            fontName='Helvetica-Bold', fontSize=9.5, leading=14,
            textColor=COR_VERMELHO, spaceAfter=6, alignment=TA_JUSTIFY),

        'carta': ParagraphStyle('CartaGB',
            fontName='Helvetica', fontSize=9.8, leading=15,
            textColor=COR_TEXTO, spaceAfter=8, alignment=TA_JUSTIFY),

        'citacao': ParagraphStyle('CitacaoGB',
            fontName='Helvetica-Oblique', fontSize=9.5, leading=14,
            textColor=COR_ROXA, spaceAfter=8, alignment=TA_CENTER,
            leftIndent=30, rightIndent=30),

        'dados': ParagraphStyle('DadosGB',
            fontName='Helvetica', fontSize=9.5, leading=13,
            textColor=COR_TEXTO, spaceAfter=2),

        'macro': ParagraphStyle('MacroGB',
            fontName='Helvetica-Bold', fontSize=10, leading=14,
            textColor=COR_TEXTO, spaceAfter=3),

        'centrado': ParagraphStyle('CentradoGB',
            fontName='Helvetica', fontSize=9.5, leading=14,
            textColor=COR_TEXTO, spaceAfter=4, alignment=TA_CENTER),

        'centrado_bold': ParagraphStyle('CentradoBold',
            fontName='Helvetica-Bold', fontSize=10, leading=14,
            textColor=COR_ROXA, spaceAfter=4, alignment=TA_CENTER),
    }


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sp(h=6):
    return Spacer(1, h)


def secao(titulo, estilos):
    return [
        Paragraph(titulo, estilos['titulo_roxo']),
        HRFlowable(width='100%', thickness=0.5, color=COR_ROXA, spaceAfter=4),
    ]


def apply_inline_markup(text):
    """
    Converte **negrito**, *italico* e URLs clicaveis para tags ReportLab.
    Escapa caracteres XML especiais (<, >, &) fora das marcacoes geradas,
    para que o parser do ReportLab nao quebre com texto como "glicose < 92 mg/dL".
    """
    # Passo 1: extrair blocos com marcacao e substituir por placeholders
    # para nao escapar as proprias tags que vamos gerar
    PLACEHOLDER = '\x00{}' # caracter nulo como delimitador — nunca aparece no texto do Claude

    partes = []  # lista de (texto_raw, e_markup)
    restante = text

    # Processar **negrito**
    def substituir_bold(m):
        conteudo = _html.escape(m.group(1))
        idx = len(partes)
        partes.append(f'<b>{conteudo}</b>')
        return PLACEHOLDER.format(idx)

    restante = re.sub(r'\*\*(.+?)\*\*', substituir_bold, restante)

    # Processar *italico* (apenas asterisco simples que sobrou)
    def substituir_italic(m):
        conteudo = _html.escape(m.group(1))
        idx = len(partes)
        partes.append(f'<i>{conteudo}</i>')
        return PLACEHOLDER.format(idx)

    restante = re.sub(r'\*(.+?)\*', substituir_italic, restante)

    # Processar URLs
    def substituir_url(m):
        url = m.group(1)
        url_escaped = _html.escape(url)
        idx = len(partes)
        partes.append(f'<a href="{url_escaped}" color="#7B1FA2">{url_escaped}</a>')
        return PLACEHOLDER.format(idx)

    restante = re.sub(r'(?<!href=")(https?://[^\s\)\]<>"]+)', substituir_url, restante)

    # Passo 2: escapar o texto restante (fora das marcacoes)
    restante = _html.escape(restante)

    # Passo 3: restaurar os placeholders com as tags geradas
    for idx, tag in enumerate(partes):
        restante = restante.replace(_html.escape(PLACEHOLDER.format(idx)), tag)

    return restante


def calcular_trimestre(semanas_str):
    try:
        s = int(str(semanas_str).strip().split()[0])
        if s <= 13:
            return "I TRIMESTRE"
        elif s <= 27:
            return "II TRIMESTRE"
        else:
            return "III TRIMESTRE"
    except Exception:
        return "GESTACAO"


def nome_arquivo_pdf(nome, semanas_str):
    """Gera nome de arquivo seguro para o PDF, preservando acentos normalizados."""
    # Normaliza Unicode → ASCII (ex: "Lúcia" → "Lucia")
    nome_ascii = unicodedata.normalize('NFKD', nome).encode('ascii', 'ignore').decode('ascii')
    nome_limpo = re.sub(r'[^A-Za-z\s]', '', nome_ascii).strip().replace(' ', '_')
    if not nome_limpo:
        nome_limpo = 'Paciente'
    trimestre = calcular_trimestre(semanas_str).replace(' ', '_')
    return f"{nome_limpo}_{trimestre}.pdf"


# ══════════════════════════════════════════════════════════════════════════════
# PARSER DO TEXTO DO CLAUDE
# ══════════════════════════════════════════════════════════════════════════════

def render_texto_claude(texto, estilos):
    """
    Converte o texto gerado pelo Claude em flowables do ReportLab.

    Marcadores suportados:
      ## Titulo        → secao roxa com linha HR
      ### Subtitulo    → negrito escuro
      - item           → bullet normal
      + item           → bullet verde (FAZER)
      x item           → bullet vermelho (NAO FAZER)
      ATENCAO:         → paragrafo vermelho bold
      "citacao"        → estilo italico centralizado
      ---              → linha separadora visual (NAO e quebra de pagina)
      ===              → quebra de pagina explicita
      linha vazia      → espacador
      texto normal     → paragrafo justificado
    """
    elements = []
    linhas = texto.strip().split('\n')

    for linha in linhas:
        linha_strip = linha.strip()

        if not linha_strip:
            elements.append(sp(4))
            continue

        # Quebra de pagina APENAS com === ou ---PAGE---
        if linha_strip in ('---PAGE---', '==='):
            # Evita page break duplo (gera pagina em branco)
            if elements and not isinstance(elements[-1], PageBreak):
                elements.append(PageBreak())
            continue

        # --- e tratado como separador visual, NAO como quebra de pagina
        if linha_strip == '---' or re.match(r'^-{3,}$', linha_strip):
            elements.append(sp(6))
            elements.append(HRFlowable(width='100%', thickness=0.5,
                                       color=COR_DOURADA, spaceAfter=6))
            continue

        # Secao principal: ## Titulo
        if linha_strip.startswith('## '):
            titulo = _html.escape(linha_strip[3:].strip())
            elements += secao(titulo, estilos)
            continue

        # Subsecao: ### Titulo
        if linha_strip.startswith('### '):
            titulo = linha_strip[4:].strip()
            titulo = apply_inline_markup(titulo)
            elements.append(Paragraph(f'<b>{titulo}</b>', estilos['subtitulo']))
            continue

        # Sub-opcao: #### Titulo (ex: opcoes de lanche)
        if linha_strip.startswith('#### '):
            titulo = linha_strip[5:].strip()
            titulo = apply_inline_markup(titulo)
            elements.append(sp(4))
            elements.append(Paragraph(f'<b>{titulo}</b>', estilos['dados']))
            continue

        # Sub-sub-opcao: ##### Titulo
        if linha_strip.startswith('##### '):
            titulo = linha_strip[6:].strip()
            titulo = apply_inline_markup(titulo)
            elements.append(Paragraph(f'<i>{titulo}</i>', estilos['normal']))
            continue

        # Bullet verde (FAZER): + item
        if linha_strip.startswith('+ '):
            conteudo = apply_inline_markup(linha_strip[2:].strip())
            elements.append(Paragraph(f'✓  {conteudo}', estilos['bullet_verde']))
            elements.append(sp(2))
            continue

        # Bullet vermelho (NAO FAZER): x item
        if linha_strip.lower().startswith('x ') and len(linha_strip) > 2:
            conteudo = apply_inline_markup(linha_strip[2:].strip())
            elements.append(Paragraph(f'X  {conteudo}', estilos['bullet_vermelho']))
            elements.append(sp(2))
            continue

        # Bullet normal: - item ou • item
        if linha_strip.startswith('- ') or linha_strip.startswith('* ') or linha_strip.startswith('\u2022 '):
            conteudo = apply_inline_markup(linha_strip[2:].strip())
            elements.append(Paragraph(f'\u2022  {conteudo}', estilos['bullet']))
            elements.append(sp(2))
            continue

        # Alerta vermelho: ATENCAO: ou ATENCAO!
        linha_upper = linha_strip.upper()
        if linha_upper.startswith('ATEN') and (
            'ATEN\u00c7\u00c3O:' in linha_upper or 'ATENCAO:' in linha_upper or
            'ATEN\u00c7\u00c3O!' in linha_upper or 'ATENCAO!' in linha_upper
        ):
            conteudo = apply_inline_markup(linha_strip)
            elements.append(Paragraph(conteudo, estilos['alerta_vermelho']))
            continue

        # Citacao biblica: linha entre aspas
        if (linha_strip.startswith('"') and linha_strip.endswith('"')) or \
           (linha_strip.startswith('\u201c') and linha_strip.endswith('\u201d')):
            elements.append(Paragraph(_html.escape(linha_strip), estilos['citacao']))
            continue

        # Paragrafo normal
        conteudo = apply_inline_markup(linha_strip)
        elements.append(Paragraph(conteudo, estilos['justificado']))

    return elements


# ══════════════════════════════════════════════════════════════════════════════
# FUNCAO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def gerar_pdf(dados, plano_texto):
    """
    Gera o PDF completo.
    Retorna bytes do PDF.

    Args:
        dados: dict com campos do formulario (nome, semanas_gestacao, peso_atual, etc.)
        plano_texto: str com o plano gerado pelo Claude
    """
    buffer = io.BytesIO()

    estilos = criar_estilos()

    nome        = dados.get('nome', 'Paciente')
    semanas     = dados.get('semanas_gestacao', '')
    peso_atual  = dados.get('peso_atual', '')
    peso_antes  = dados.get('peso_antes', '')
    trimestre   = calcular_trimestre(semanas)

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGEM,
        rightMargin=MARGEM,
        topMargin=HEADER_HEIGHT + 0.4 * cm,
        bottomMargin=FOOTER_HEIGHT + 0.5 * cm,
    )

    story = []

    # ── Pagina 1: Dados da Gestante ───────────────────────────────────────────
    story += secao('Dados da Gestante', estilos)
    story.append(sp(6))

    campos = [
        ('Gestante:', _html.escape(str(nome))),
        ('Periodo de Gestacao:', f'{_html.escape(str(trimestre))} ({_html.escape(str(semanas))} semanas)'),
        ('Peso atual:', f'{_html.escape(str(peso_atual))} kg' if peso_atual else '-'),
        ('Peso pre-gestacao:', f'{_html.escape(str(peso_antes))} kg' if peso_antes else '-'),
    ]
    for chave, valor in campos:
        story.append(Paragraph(f'<b>{chave}</b>  {valor}', estilos['dados']))
        story.append(sp(2))

    story.append(sp(14))

    # ── Conteudo gerado pelo Claude ───────────────────────────────────────────
    story += render_texto_claude(plano_texto, estilos)

    # ── Assinatura final ──────────────────────────────────────────────────────
    # NAO adicionar assinatura fixa — Claude ja gera o fechamento com assinatura
    story.append(sp(12))

    doc.build(story, onFirstPage=draw_background, onLaterPages=draw_background)

    buffer.seek(0)
    return buffer.read()


def gerar_pdf_base64(dados, plano_texto):
    """Gera o PDF e retorna como string base64."""
    pdf_bytes = gerar_pdf(dados, plano_texto)
    return base64.b64encode(pdf_bytes).decode('utf-8')
