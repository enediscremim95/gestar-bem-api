# -*- coding: utf-8 -*-
"""
main.py — Railway Flask API — Gestar Bem
Recebe dados do formulario via Apps Script, calcula TMB/macros,
gera plano com Claude, converte em PDF e envia por email.

Formula TMB: Mifflin-St Jeor
  Mulheres: (10 x peso) + (6,25 x altura) - (5 x idade) - 161
Fator atividade:
  Sedentaria     = 1.2
  Leve           = 1.375
  Moderada       = 1.55
  Avancada/Intensa = 1.725
"""

import os, logging, re, threading, base64, json, traceback, atexit
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json as PgJson
from apscheduler.schedulers.background import BackgroundScheduler

from flask import Flask, request, jsonify, send_from_directory, abort
import anthropic
from pdf_generator import gerar_pdf_base64, nome_arquivo_pdf

app = Flask(__name__)
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Cliente Anthropic — criado uma vez na inicializacao do servidor
# timeout=120s: se o Claude nao responder em 2 min, a thread nao fica pendurada para sempre
_anthropic_client = anthropic.Anthropic(
    api_key=os.environ.get('ANTHROPIC_API_KEY'),
    timeout=300.0
)

# Maximo de tentativas antes de desistir de um job com erro
MAX_TENTATIVAS = 3

# Delay em horas antes de enviar o plano (padrão: 5 minutos para testes)
try:
    DELAY_HORAS = float(os.environ.get('DELAY_HORAS', '0.083'))
except (ValueError, TypeError):
    log.warning("DELAY_HORAS invalido no ambiente — usando 0.083 (5 minutos)")
    DELAY_HORAS = 0.083


# ── Banco de dados ────────────────────────────────────────────────────────────

def get_db():
    """Retorna conexão com PostgreSQL."""
    url = os.environ.get('DATABASE_URL', '')
    if not url:
        raise ValueError("DATABASE_URL nao configurado")
    if 'sslmode' not in url:
        url += ('&' if '?' in url else '?') + 'sslmode=require'
    return psycopg2.connect(url)


def init_db():
    """Cria tabela de fila se nao existir."""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planos_agendados (
                id            SERIAL PRIMARY KEY,
                dados         JSONB        NOT NULL,
                agendado_para TIMESTAMP    NOT NULL,
                processado    BOOLEAN      DEFAULT FALSE,
                tentativas    INTEGER      DEFAULT 0,
                criado_em     TIMESTAMP    DEFAULT NOW(),
                processado_em TIMESTAMP,
                erro          TEXT
            )
        """)
        # Adiciona coluna tentativas se a tabela ja existia sem ela
        cur.execute("""
            ALTER TABLE planos_agendados
            ADD COLUMN IF NOT EXISTS tentativas INTEGER DEFAULT 0
        """)
        # Indice para o agendador nao fazer full-scan a cada minuto
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fila_pendente
            ON planos_agendados (agendado_para)
            WHERE processado = FALSE
        """)
        conn.commit()
        cur.close()
        log.info("Banco inicializado com sucesso")
    except Exception as e:
        log.error(f"Erro ao inicializar banco: {e}")
    finally:
        if conn:
            conn.close()


def verificar_fila():
    """Verifica fila e processa planos agendados cujo horario chegou."""
    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, dados, tentativas FROM planos_agendados
            WHERE processado = FALSE AND agendado_para <= NOW()
            AND tentativas < %s
            ORDER BY agendado_para
            LIMIT 5
            FOR UPDATE SKIP LOCKED
        """, (MAX_TENTATIVAS,))
        jobs = cur.fetchall()
        cur.close()
    except Exception as e:
        log.error(f"[FILA] Erro ao verificar fila: {e}")
        return
    finally:
        if conn:
            conn.close()

    for job_id, dados, tentativas in jobs:
        nome = dados.get('nome', '?')
        log.info(f"[FILA] Processando job {job_id} — {nome} (tentativa {tentativas + 1}/{MAX_TENTATIVAS})")
        conn2 = None
        try:
            with app.app_context():
                _gerar_plano_interno(dados)

            conn2 = get_db()
            cur2  = conn2.cursor()
            cur2.execute("""
                UPDATE planos_agendados
                SET processado = TRUE, processado_em = NOW()
                WHERE id = %s
            """, (job_id,))
            conn2.commit()
            cur2.close()
            log.info(f"[FILA] Job {job_id} concluido com sucesso")

        except Exception as e:
            nova_tentativa = tentativas + 1
            desistir = nova_tentativa >= MAX_TENTATIVAS
            log.error(f"[FILA] Erro no job {job_id} (tentativa {nova_tentativa}/{MAX_TENTATIVAS})"
                      f"{' — desistindo' if desistir else ' — vai tentar de novo'}: {traceback.format_exc()}")
            conn3 = None
            try:
                conn3 = get_db()
                cur3  = conn3.cursor()
                cur3.execute("""
                    UPDATE planos_agendados
                    SET tentativas    = %s,
                        processado    = %s,
                        erro          = %s,
                        processado_em = CASE WHEN %s THEN NOW() ELSE NULL END
                    WHERE id = %s
                """, (nova_tentativa, desistir, str(e)[:500], desistir, job_id))
                conn3.commit()
                cur3.close()
            except Exception:
                pass
            finally:
                if conn3:
                    conn3.close()
        finally:
            if conn2:
                conn2.close()


# Inicializar banco e agendador ao subir o servidor
init_db()
_scheduler = BackgroundScheduler(timezone='America/Sao_Paulo')
_scheduler.add_job(verificar_fila, 'interval', minutes=1, id='verificar_fila', max_instances=1)
_scheduler.start()
atexit.register(lambda: _scheduler.shutdown(wait=False))


# ── Funcao de envio de email ─────────────────────────────────────────────────

def enviar_email_pdf(destinatario, nome_paciente, pdfs_lista, links_treino=None):
    """Envia PDF de nutricao (anexo) + links de treino (corpo) via SendGrid."""
    sg_key    = os.environ.get('SENDGRID_API_KEY', '')
    remetente = 'planosgestarbem@gmail.com'

    if not sg_key:
        raise ValueError("SENDGRID_API_KEY nao configurado no ambiente")

    if not pdfs_lista:
        raise ValueError("Nenhum PDF gerado — email nao sera enviado sem anexo")

    # Montar bloco de links de treino
    bloco_treino = ""
    if links_treino:
        bloco_treino = "\n\n" + "—" * 40 + "\n📋 SEUS PLANOS DE TREINO\n\n"
        for url, label in links_treino:
            bloco_treino += f"▶ {label}:\n{url}\n\n"
        bloco_treino += "Clique no link acima para abrir o PDF no navegador.\nVocê também pode salvar no seu celular para consultar offline."

    corpo = f"""Olá, {nome_paciente}! 💜

Seu plano personalizado do programa Gestar Bem está pronto!

Em anexo você encontra o seu Plano de Nutrição completo, preparado especialmente para você com muito carinho e cuidado.{bloco_treino}

—————————————————————————
Leia com atenção e siga as orientações. Qualquer dúvida, fale com nossa equipe.

Com carinho,
Equipe Gestar Bem 🌸"""

    anexos = []
    for pdf_bytes, nome_arquivo in pdfs_lista:
        anexos.append({
            "content":     base64.b64encode(pdf_bytes).decode(),
            "filename":    nome_arquivo,
            "type":        "application/pdf",
            "disposition": "attachment"
        })

    payload = {
        "personalizations": [{"to": [{"email": destinatario}]}],
        "from":    {"email": remetente, "name": "Gestar Bem"},
        "subject": "Seu Plano Personalizado — Gestar Bem",
        "content": [{"type": "text/plain", "value": corpo}],
        "attachments": anexos,
        "tracking_settings": {
            "click_tracking": {"enable": False, "enable_text": False}
        }
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {sg_key}",
            "Content-Type":  "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            log.info(f"Email enviado via SendGrid para {destinatario} com {len(anexos)} PDF(s) — status {resp.status}")
    except urllib.error.HTTPError as e:
        corpo_erro = e.read().decode('utf-8', errors='ignore')
        raise Exception(f"SendGrid erro HTTP {e.code}: {corpo_erro}")
    except urllib.error.URLError as e:
        raise Exception(f"SendGrid erro de rede: {e.reason}")


# ── Selecao do PDF de exercicios ─────────────────────────────────────────────

PDF_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pdfs')

def _base_url():
    """URL base do servidor (Railway usa RAILWAY_PUBLIC_DOMAIN)."""
    dominio = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'web-production-94437.up.railway.app')
    return f"https://{dominio}"


def encurtar_url(url):
    """Encurta URL via TinyURL. Retorna URL original se falhar."""
    try:
        api = f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(api, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            curta = resp.read().decode('utf-8').strip()
            if curta.startswith('http'):
                log.info(f"URL encurtada: {curta}")
                return curta
    except Exception as e:
        log.warning(f"TinyURL falhou, usando URL original: {e}")
    return url

def selecionar_pdf_limitacao(limitacao, nivel, tri):
    """Seleciona PDF de limitacao com base no tipo de limitacao relatada."""
    lim = limitacao.lower()

    if 'joelho' in lim:
        arq = 'joelho_avancado_III.pdf' if tri == 'III' else 'joelho_avancado.pdf'
        return os.path.join(PDF_BASE, 'limitacao', arq)

    if 'pulso' in lim or 'punho' in lim or 'mao' in lim or 'mão' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'pulso_iniciante.pdf')

    if 'afundo' in lim:
        arq = 'sem_afundo_iniciante_III.pdf' if tri == 'III' else 'sem_afundo_iniciante.pdf'
        return os.path.join(PDF_BASE, 'limitacao', arq)

    if ('agachamento' in lim or 'agachar' in lim) and 'leg' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'sem_agachamento_leg_avancado.pdf')

    if 'agachamento' in lim or 'agachar' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'sem_agachamento_avancado.pdf')

    if 'leg' in lim and ('eleva' in lim or 'ombro' in lim):
        # Apenas versao _III disponivel para esta combinacao (todos os trimestres usam o mesmo arquivo)
        arq = ('sem_leg_elevacao_intermediario_III.pdf'
               if nivel == 'intermediario' else 'sem_leg_elevacao_iniciante_III.pdf')
        return os.path.join(PDF_BASE, 'limitacao', arq)

    if 'leg' in lim and tri == 'III':
        return os.path.join(PDF_BASE, 'limitacao', 'sem_leg_elevacao_iniciante_III.pdf')

    if 'eleva' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'sem_elevacao_avancado.pdf')

    if 'extensora' in lim or 'extensor' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'sem_extensora_intermediario_I.pdf')

    if 'maquina' in lim or 'máquina' in lim:
        return os.path.join(PDF_BASE, 'limitacao', 'somente_maquinas.pdf')

    # Limitacao generica / multipla
    return os.path.join(PDF_BASE, 'limitacao', 'multipla.pdf')




def selecionar_links_exercicio(dados, trimestre):
    """
    Retorna lista de (url, label) para os PDFs de exercicio.
    Ex: [("https://.../treino/academia/academia_I_iniciante.pdf", "Plano Academia")]
    """
    liberado = str(dados.get('liberado_exercicio', '')).lower()
    if 'nao' in liberado or 'não' in liberado or not liberado.strip():
        log.info("Paciente nao liberada para exercicios — sem link de treino")
        return []

    rotina  = str(dados.get('rotina_exercicio', '')).lower()
    nivel_r = str(dados.get('nivel_exercicio', '')).lower()
    limit   = str(dados.get('limitacao_exercicio', '')).strip()

    if 'iniciante' in nivel_r or 'leve' in nivel_r:
        nivel = 'iniciante'
    elif 'intermediar' in nivel_r or 'moder' in nivel_r:
        nivel = 'intermediario'
    elif 'avan' in nivel_r or 'intens' in nivel_r:
        nivel = 'avancado'
    else:
        nivel = 'iniciante'

    tri = trimestre
    eh_academia = any(p in rotina for p in ('academia', 'muscula', 'gym', 'palestra'))
    eh_casa     = any(p in rotina for p in ('casa', 'home', 'apartamento'))
    tem_limit   = bool(limit and limit.lower() not in
                       ('nao', 'não', 'nenhuma', 'nenhum', 'sem limitacao',
                        'sem limitação', 'nao tenho', 'não tenho', ''))

    base  = _base_url()
    links = []

    if eh_academia or (not eh_academia and not eh_casa):
        if tem_limit:
            full_path = selecionar_pdf_limitacao(limit, nivel, tri)
            rel = os.path.relpath(full_path, PDF_BASE).replace('\\', '/')
        else:
            rel = f"academia/academia_{tri}_{nivel}.pdf"

        caminho_local = os.path.join(PDF_BASE, rel.replace('/', os.sep))
        if os.path.exists(caminho_local):
            links.append((encurtar_url(f"{base}/treino/{rel}"), "Plano de Treinos — Academia"))
        else:
            log.warning(f"PDF de treino nao encontrado: {caminho_local}")

    if eh_casa:
        rel = f"casa/casa_{tri}.pdf"
        caminho_local = os.path.join(PDF_BASE, rel)
        if os.path.exists(caminho_local):
            links.append((encurtar_url(f"{base}/treino/{rel}"), "Plano de Treinos — Casa"))
        else:
            log.warning(f"PDF de treino nao encontrado: {caminho_local}")

    log.info(f"Links de treino selecionados: {[l for _, l in links]}")
    return links


# ── Calculos clinicos (TMB, macros, hidratacao) ──────────────────────────────

def _extrair_numero(valor, inteiro=False):
    """Extrai o primeiro numero de uma string. Lança ValueError se não encontrar."""
    match = re.search(r'\d+(?:[,\.]\d+)?', str(valor or ''))
    if not match:
        raise ValueError(f"Nao foi possivel extrair numero de: {repr(valor)}")
    numero = float(match.group().replace(',', '.'))
    return int(numero) if inteiro else numero


def calcular_dados_clinicos(dados):
    """
    Calcula TMB (Mifflin-St Jeor), manutencao, calorias alvo,
    macros em gramas e hidratacao.
    Retorna dict com todos os valores ou None se nao for possivel calcular.
    """
    try:
        # Validar campos obrigatorios antes de calcular
        for campo in ['peso_atual', 'altura', 'idade', 'semanas_gestacao']:
            if not dados.get(campo):
                log.warning(f"Campo obrigatorio ausente ou vazio: {campo}")
                return None

        peso    = _extrair_numero(dados.get('peso_atual'))
        alt     = _extrair_numero(dados.get('altura'))
        idade   = _extrair_numero(dados.get('idade'))
        semanas = _extrair_numero(dados.get('semanas_gestacao'), inteiro=True)
        nivel   = str(dados.get('nivel_exercicio', '')).lower()

        # Normalizar altura: se vier em metros (ex: 1.65), converter para cm
        if alt < 3:
            log.warning(f"Altura parece estar em metros ({alt}m) — convertendo para cm ({alt*100}cm)")
            alt = alt * 100

        # Validar ranges razoaveis
        if not (30 <= peso <= 200):
            log.warning(f"Peso fora do range esperado: {peso}kg")
        if not (140 <= alt <= 220):
            log.warning(f"Altura fora do range esperado: {alt}cm")
        if not (1 <= semanas <= 42):
            log.warning(f"Semanas fora do range esperado: {semanas}")

        # Trimestre
        if semanas <= 13:
            trimestre = "I"
            tri_nome  = "Primeiro Trimestre (semanas 1–13)"
        elif semanas <= 26:
            trimestre = "II"
            tri_nome  = "Segundo Trimestre (semanas 14–26)"
        else:
            trimestre = "III"
            tri_nome  = "Terceiro Trimestre (semanas 27–40)"

        # TMB — Mifflin-St Jeor para mulheres
        tmb = (10 * peso) + (6.25 * alt) - (5 * idade) - 161

        # Fator de atividade
        if 'sedent' in nivel:
            fator = 1.2;   fator_nome = "Sedentaria (x1,2)"
        elif 'leve' in nivel or 'iniciante' in nivel:
            fator = 1.375; fator_nome = "Levemente ativa (x1,375)"
        elif 'moder' in nivel or 'intermedi' in nivel:
            fator = 1.55;  fator_nome = "Moderadamente ativa (x1,55)"
        elif 'avan' in nivel or 'intens' in nivel:
            fator = 1.725; fator_nome = "Muito ativa (x1,725)"
        else:
            log.warning(f"nivel_exercicio nao reconhecido: '{nivel}' — usando fallback 1.375")
            fator = 1.375; fator_nome = "Levemente ativa (x1,375)"

        manutencao = tmb * fator

        # Peso ideal estimado (formula 22 x altura^2) para definir estrategia
        altura_m   = alt / 100
        peso_ideal = 22 * (altura_m ** 2)
        excesso    = peso - peso_ideal

        # Estrategia calorica
        if excesso > 5:
            # Sobrepeso/obesidade: deficit de 300-500 kcal
            calorias_alvo = manutencao - 400
            estrategia = (
                f"SOBREPESO/OBESIDADE — deficit de 400 kcal. "
                f"Peso atual {peso:.1f}kg, peso ideal estimado {peso_ideal:.1f}kg "
                f"(excesso de {excesso:.1f}kg). Objetivo: emagrecimento controlado e seguro."
            )
        else:
            if trimestre == "I":
                calorias_alvo = manutencao
                estrategia = "PESO ADEQUADO — 1o trimestre: manutencao de peso."
            elif trimestre == "II":
                calorias_alvo = manutencao + 340
                estrategia = "PESO ADEQUADO — 2o trimestre: +340 kcal acima da manutencao."
            else:
                calorias_alvo = manutencao + 450
                estrategia = "PESO ADEQUADO — 3o trimestre: +450 kcal acima da manutencao."

        # Macronutrientes (35% prot / 40% carb / 25% gord)
        prot_kcal = calorias_alvo * 0.35
        carb_kcal = calorias_alvo * 0.40
        gord_kcal = calorias_alvo * 0.25
        prot_g    = prot_kcal / 4
        carb_g    = carb_kcal / 4
        gord_g    = gord_kcal / 9

        # Hidratacao (1o e 2o tri: peso x 35ml | 3o tri: peso x 40ml)
        agua_ml   = peso * 40 if trimestre == "III" else peso * 35
        agua_l    = agua_ml / 1000

        return {
            "tmb":           round(tmb),
            "fator_nome":    fator_nome,
            "manutencao":    round(manutencao),
            "calorias_alvo": round(calorias_alvo),
            "estrategia":    estrategia,
            "prot_g":        round(prot_g),
            "carb_g":        round(carb_g),
            "gord_g":        round(gord_g),
            "agua_l":        round(agua_l, 1),
            "trimestre":     trimestre,
            "tri_nome":      tri_nome,
        }

    except Exception as e:
        log.warning(f"Nao foi possivel calcular dados clinicos: {e} | "
                    f"peso={dados.get('peso_atual')} alt={dados.get('altura')} "
                    f"idade={dados.get('idade')} semanas={dados.get('semanas_gestacao')}")
        return None


# ── Endpoint principal ───────────────────────────────────────────────────────

@app.route('/treino/<path:filename>')
def servir_treino(filename):
    """Serve os PDFs de treino publicamente via link."""
    caminho = os.path.abspath(os.path.join(PDF_BASE, filename))
    if not caminho.startswith(os.path.abspath(PDF_BASE)):
        abort(403)
    return send_from_directory(PDF_BASE, filename)


@app.route('/gerar-plano', methods=['POST'])
def gerar_plano():
    """Recebe os dados e agenda o plano no banco de dados."""
    dados         = request.get_json(force=True) or {}
    nome          = dados.get('nome', 'Paciente')
    email         = dados.get('email', '')
    agendado_para = datetime.now(timezone.utc) + timedelta(hours=DELAY_HORAS)
    minutos       = round(DELAY_HORAS * 60)

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO planos_agendados (dados, agendado_para)
            VALUES (%s, %s)
        """, (PgJson(dados), agendado_para))
        conn.commit()
        cur.close()
        log.info(f"Plano de {nome} agendado para {agendado_para.strftime('%d/%m/%Y %H:%M')}")
        return jsonify({
            "status":   "agendado",
            "mensagem": f"Plano de {nome} agendado. Email sera enviado em {minutos} minuto(s).",
            "nome":     nome,
            "email":    email,
        })

    except Exception as e:
        log.error(f"Erro ao agendar no banco — processando direto: {e}")
        thread = threading.Thread(target=_processar_em_background, args=(dados,))
        thread.daemon = True
        thread.start()
        return jsonify({
            "status":   "aceito",
            "mensagem": f"Plano de {nome} sendo gerado (modo direto).",
            "nome":     nome,
            "email":    email,
        })

    finally:
        if conn:
            conn.close()


def _processar_em_background(dados):
    """Executa todo o processamento (Claude + PDF + email) em thread separada."""
    nome  = dados.get('nome', 'Paciente')
    email = dados.get('email', '')
    try:
        log.info(f"[BG] Iniciando processamento para {nome}")
        with app.app_context():
            _gerar_plano_interno(dados)
        log.info(f"[BG] Processamento concluido para {nome}")
    except Exception as e:
        log.error(f"[BG] ERRO para {nome} ({email}): {traceback.format_exc()}")


def _gerar_plano_interno(dados):

    # Validar email ANTES de qualquer processamento caro (Claude + PDF)
    email = dados.get('email', '').strip()
    if not email:
        log.warning(f"[INTERNO] Email vazio para '{dados.get('nome', 'Paciente')}' — abortando sem chamar Claude")
        return

    # Extrair campos do formulario
    nome               = dados.get('nome', 'Paciente')
    # email ja extraido e validado acima
    pais               = dados.get('pais', 'Brasil')
    idade              = dados.get('idade', '')
    altura             = dados.get('altura', '')
    semanas_gestacao   = dados.get('semanas_gestacao', '')
    peso_atual         = dados.get('peso_atual', '')
    peso_antes         = dados.get('peso_antes', '')
    peso_primeira      = dados.get('peso_primeira_consulta', '')
    complicacoes       = dados.get('complicacoes', 'Nenhuma')
    medicamentos       = dados.get('medicamentos', 'Nenhum')
    suplementos        = dados.get('suplementos', 'Nenhum')
    gravidez_planejada = dados.get('gravidez_planejada', '')
    sintomas           = dados.get('sintomas', '')
    outros_sintomas    = dados.get('outros_sintomas', '')
    sono               = dados.get('sono', '')
    medo_gravidez      = dados.get('medo_gravidez', '')
    liberado_exercicio = dados.get('liberado_exercicio', '')
    nivel_exercicio    = dados.get('nivel_exercicio', '')
    periodo_exercicio  = dados.get('periodo_exercicio', '')
    rotina_exercicio   = dados.get('rotina_exercicio', '')
    limitacao_exercicio= dados.get('limitacao_exercicio', '')
    rotina_alimentacao = dados.get('rotina_alimentacao', '')
    hidratacao         = dados.get('hidratacao', '')
    intolerancia       = dados.get('intolerancia', '')
    nivel_intolerancia = dados.get('nivel_intolerancia', '')
    horario_fome       = dados.get('horario_fome', '')
    observacoes        = dados.get('observacoes', '')
    exames_anexo       = dados.get('exames_anexo', '')

    # Calculos clinicos automaticos
    calculos = calcular_dados_clinicos(dados)

    if calculos:
        bloco_calculos = f"""
CALCULOS CLINICOS JA REALIZADOS (use estes valores exatos no plano):
- Trimestre: {calculos['tri_nome']}
- TMB (Mifflin-St Jeor): {calculos['tmb']} kcal
- Nivel de atividade: {calculos['fator_nome']}
- Calorias de manutencao: {calculos['manutencao']} kcal
- Calorias alvo do plano: {calculos['calorias_alvo']} kcal
- Estrategia: {calculos['estrategia']}
- Proteina: {calculos['prot_g']}g/dia (35% das calorias — 4 kcal/g)
- Carboidrato: {calculos['carb_g']}g/dia (40% das calorias — 4 kcal/g)
- Gordura: {calculos['gord_g']}g/dia (25% das calorias — 9 kcal/g)
- Meta de agua: {calculos['agua_l']}L/dia"""
    else:
        bloco_calculos = """
CALCULOS CLINICOS: Nao foi possivel calcular automaticamente.
Use sua experiencia clinica para estimar calorias e macros com base nos dados fornecidos.
Padrao: 35% proteina / 40% carboidrato / 25% gordura."""

    # ── Bloco de contexto do trimestre ──────────────────────────────────────────
    if calculos:
        trimestre_codigo = calculos['trimestre']
    else:
        # calculos falhou — extrair trimestre direto das semanas para nao errar o PDF de treino
        try:
            _s = _extrair_numero(dados.get('semanas_gestacao', '1'), inteiro=True)
            trimestre_codigo = 'III' if _s > 26 else ('II' if _s > 13 else 'I')
        except Exception:
            trimestre_codigo = 'I'

    if trimestre_codigo == 'I':
        contexto_trimestre = """CONTEXTO DO 1o TRIMESTRE (semanas 1 a 13):
Este e um periodo de grandes adaptacoes hormonais. E muito comum:
- Enjoos e nauseas (principalmente pela manha ou ao longo do dia)
- Aversao a certos alimentos e odores
- Fadiga intensa
- Constipacao intestinal
- Alteracoes de humor

CONDUTAS ESPECIFICAS PARA O 1o TRIMESTRE:
- Refeicoes MENORES e mais frequentes para minimizar enjoos
- Alimentos secos no cafe da manha (torrada integral, biscoito de agua)
- Gengibre em quantidades moderadas pode ajudar com nauseas
- Evitar alimentos de odor forte (frituras, ovos mexidos muito cozidos)
- Hidratacao fracionada (pequenos goles ao longo do dia)
- Acido folico E ESSENCIAL neste periodo — verificar se esta em uso
- Estrategia calorica: MANUTENCAO DE PESO (nao e momento de ganhar muito)
- Se houve perda de peso por enjoos: priorizar alimentos tolerados e nutritivos
- Tom da carta: acolher a vulnerabilidade e inseguranca do inicio da gestacao"""

    elif trimestre_codigo == 'II':
        contexto_trimestre = """CONTEXTO DO 2o TRIMESTRE (semanas 14 a 26):
E o trimestre do "renascimento" — os enjoos costumam diminuir,
a energia volta e a barriga comeca a aparecer de forma bonita.
E o melhor momento para estabelecer habitos solidos.

CONDUTAS ESPECIFICAS PARA O 2o TRIMESTRE:
- Acrescentar +340 kcal ao dia em relacao a manutencao (ja calculado)
- O bebe esta em fase de crescimento acelerado — proteina e FUNDAMENTAL
- Ferro e calcio tornam-se ainda mais importantes neste periodo
- Constipacao pode continuar — fibras, agua e movimento sao essenciais
- Exercicios fisicos sao geralmente bem tolerados (com liberacao medica)
- Hidratacao: peso x 35ml/dia
- Inchazo leve pode comecar — monitorar ingestao de sodio
- Omega-3 DHA e crucial para desenvolvimento cerebral fetal
- Tom da carta: celebrar a fase de energia e estimular a construcao de habitos"""

    else:
        contexto_trimestre = """CONTEXTO DO 3o TRIMESTRE (semanas 27 a 40):
A reta final da gestacao. O bebe esta crescendo rapidamente e o corpo
da mae esta se preparando para o parto. E normal sentir:
- Maior dificuldade para comer grandes volumes (bebe ocupa espaco)
- Refluxo e azia mais frequentes
- Inchazo nos pes e maos
- Dificuldade para dormir
- Maior cansaco e falta de ar

CONDUTAS ESPECIFICAS PARA O 3o TRIMESTRE:
- Refeicoes MENORES e mais frequentes — o estomago tem menos espaco
- Acrescentar +450 kcal ao dia em relacao a manutencao (ja calculado)
- Hidratacao: peso x 40ml/dia (aumenta em relacao aos trimestres anteriores)
- Evitar alimentos que pioram refluxo: frituras, acidos, cafe em excesso
- Calcio e vitamina D sao criticos para mineralizacao ossea do bebe
- Ferro: verificar ferritina — anemia no 3o trimestre e mais perigosa
- Proteina alta para suportar crescimento fetal e preparar o perineo
- CEIA OBRIGATORIA — impede hipoglicemia noturna
- Exercicios de baixo impacto (caminhada, hidroginastica pre-natal se liberado)
- Tom da carta: encorajar a chegada da reta final, celebrar a jornada,
  preparar emocionalmente para o parto"""

    # ── Prompt clinico completo para o Claude ────────────────────────────────
    prompt = f"""Voce e Dra. Ana, nutricionista especialista em gestacao da equipe Gestar Bem.
Seu metodo e clinico, estrategico e individualizado — nunca generico.

DADOS DA GESTANTE:
- Nome: {nome}
- Idade: {idade} anos
- Pais: {pais}
- Semanas de gestacao: {semanas_gestacao}
- Peso atual: {peso_atual} kg
- Peso antes da gestacao: {peso_antes} kg
- Peso na primeira consulta: {peso_primeira} kg
- Altura: {altura} cm
- Complicacoes: {complicacoes}
- Medicamentos: {medicamentos}
- Suplementos em uso: {suplementos}
- Gravidez planejada: {gravidez_planejada}
- Sintomas atuais: {sintomas}
- Outros sintomas: {outros_sintomas}
- Qualidade do sono: {sono}
- Medos e preocupacoes: {medo_gravidez}
- Liberada pelo medico para exercicios: {liberado_exercicio}
- Nivel de exercicio habitual: {nivel_exercicio}
- Periodo preferido para exercicios: {periodo_exercicio}
- Rotina de exercicios atual: {rotina_exercicio}
- Limitacoes fisicas para exercicios: {limitacao_exercicio}
- Rotina alimentar atual: {rotina_alimentacao}
- Hidratacao atual: {hidratacao}
- Intolerancia alimentar: {intolerancia}
- Nivel da intolerancia: {nivel_intolerancia}
- Horario de mais fome: {horario_fome}
- Observacoes adicionais: {observacoes}
- Exames / arquivos enviados: {exames_anexo}

{bloco_calculos}

{contexto_trimestre}

PROTOCOLO CLINICO — REGRAS QUE VOCE SEGUE RIGOROSAMENTE:

1. ANALISE DE EXAMES (aplique estas condutas se houver valores informados):
   - Glicose >= 92 mg/dL → Diabetes gestacional: plano com controle glicemico rigoroso,
     reducao de carboidratos simples, ceia obrigatoria, orientar monitoramento com glicosimetro
   - Glicose 90-91 mg/dL → Risco: dieta preventiva com controle de carboidratos simples
   - Glicose < 90 mg/dL → Normal: plano flexivel
   - Vitamina D < 50 → Orientar suplementacao + alimentos fontes (sardinha, ovos, funghi)
   - B12 < 600 → Orientar suplementacao (especialmente se vegetariana/vegana)
   - Ferritina < 70 → Estrategia alimentar com ferro heme + vitamina C + suplemento

2. ESTRUTURA DAS REFEICOES (obrigatoria):
   - 5 a 7 refeicoes por dia
   - Intervalo maximo de 3 horas entre refeicoes
   - PROTEINA OBRIGATORIA EM TODAS AS REFEICOES — nunca so carboidrato
   - Sem jejum — sem longos periodos sem comer
   - Se treina cedo: incluir pre-treino antes do exercicio
   - Se diabetes gestacional: incluir CEIA obrigatoria
   - Estrutura basica:
     * Cafe da manha: proteina + carboidrato + fibras
     * Almoco: proteina + carboidrato + gordura boa + salada + legumes
     * Lanches: proteina + algo leve (nunca so fruta/carboidrato)
     * Jantar: completo ou mais leve conforme rotina

3. SINTOMAS — AJUSTES:
   - Enjoo/nausea: refeicoes menores e mais frequentes, alimentos secos no cafe,
     evitar odores fortes, gengibre em quantidades seguras
   - Constipacao: aumentar fibras, agua e movimento
   - Desejo por doce: proteina + gordura boa nas refeicoes para estabilizar glicemia
   - Refluxo/azia (3o tri): evitar frituras, acidos, refeicoes grandes a noite

4. SUPLEMENTACAO BASE PARA GESTANTES:
   - Acido folico (verificar se ja usa — essencial no 1o tri)
   - Vitamina D3 (verificar exame)
   - Omega-3 DHA (seguro e importante para cerebro fetal)
   - Ferro (conforme necessidade — verificar ferritina)
   - Calcio (se baixa ingestao de laticinios)
   - Sempre: "confirme com seu medico antes de iniciar qualquer suplemento"

5. LINGUAGEM E TOM:
   - Acolhedor, pessoal, cristao
   - Trate sempre pelo primeiro nome
   - Palavras de encorajamento, proposito e fe
   - Nunca tom clinico frio — sempre humanizado
   - Adapte o tom ao momento do trimestre (ver contexto acima)

INSTRUCOES DE FORMATO — use EXATAMENTE estes marcadores (o PDF e gerado automaticamente):

## Titulo principal → roxo com linha separadora
### Subtitulo → negrito escuro
- item de lista → bullet normal
+ item positivo → bullet VERDE (coisas para FAZER)
x item negativo → bullet VERMELHO (coisas para NAO FAZER)
ATENCAO: texto → alerta vermelho em negrito
"texto entre aspas" → italico centralizado roxo (para citacoes biblicas)
--- → quebra de pagina (use entre secoes grandes)
**palavra** → negrito inline

SECOES OBRIGATORIAS (nesta ordem exata):

## CARTA DE BOAS-VINDAS
Carta calorosa e personalizada para {nome}. Mencione o trimestre especifico,
como ela pode estar se sentindo neste momento, e as preocupacoes que ela relatou.
Inclua citacao biblica relevante e palavras de encorajamento. 3 a 4 paragrafos.

---

## SOBRE O SEU PLANO
Explique brevemente o metodo Gestar Bem: clinico, individualizado, pensado so para ela.
Mencione que os calculos foram feitos especificamente para o seu corpo e momento.
Mencione o app Fat Secret para registrar refeicoes e a plataforma Kiwify para materiais.

## SEUS CALCULOS PERSONALIZADOS
Apresente os calculos de forma didatica e humanizada (nao robotica).
Explique o que e TMB, por que as calorias foram definidas assim, o que cada macro faz.
Use os valores ja calculados acima — nao invente outros.

## SUPLEMENTACAO RECOMENDADA
Liste suplementos com marcas sugeridas (ex: Vitamine-se, Max Titanium, Sundown, Puravida).
Orientacao de horario e forma de uso. Sempre finalizar: "Confirme com seu medico antes de iniciar."

---

## OBJETIVOS DO SEU PLANO
Lista dos objetivos personalizados para {nome} neste trimestre.
Seja especifico: nao "emagrecer" mas "controlar o ganho de peso dentro da faixa saudavel para voce".
Inclua objetivos especificos do trimestre atual.

## INFORMACOES IMPORTANTES ANTES DE COMECAR
Como pesar alimentos, usar o Fat Secret, horarios ideais, como substituir alimentos.
Dicas praticas do dia a dia. Inclua dicas especificas para os desafios do trimestre atual.

---

## PLANO ALIMENTAR COMPLETO
Para cada refeicao: opcao principal + MINIMO 5 opcoes de substituicao.
Inclua porcoes em gramas em todas as opcoes. Proteina em TODAS as refeicoes.
Refeicoes: Cafe da manha / Lanche da manha / Almoco / Lanche da tarde / Jantar / Ceia (se necessario).
Adapte conforme horario de fome, rotina, intolerancia alimentar e desafios do trimestre.
As substituicoes devem ser variadas: opcoes praticas, opcoes economicas, opcoes rapidas,
opcoes para quem tem enjoo, opcoes vegetarianas — sempre mantendo a proteina e as calorias equivalentes.

---

## ORIENTACOES DE EXERCICIOS
Adapte conforme liberacao medica, trimestre atual, nivel atual e limitacoes fisicas.
Se nao liberada: orientacoes de movimento leve (caminhada, alongamento).
Se liberada: programa semanal com tipo, duracao e frequencia adequados ao trimestre.
Sempre incluir orientacoes de seguranca para gestantes.

---

## CONSIDERACOES FINAIS
Encerramento com encorajamento especifico para o momento do trimestre,
lembretes dos pontos mais importantes do plano,
e informacoes de contato da equipe Gestar Bem.

Gere o plano COMPLETO, detalhado e personalizado. Minimo de 1800 palavras.
Use os calculos clinicos ja fornecidos — nao recalcule, nao mude os valores."""

    # ── Chamar o Claude ───────────────────────────────────────────────────────
    log.info(f"Chamando Claude para: {nome} ({semanas_gestacao} semanas)")
    message = _anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    if not message.content:
        raise ValueError("Claude retornou resposta vazia — abortando")
    plano_texto = message.content[0].text
    log.info(f"Plano gerado: {len(plano_texto)} chars")

    # ── Gerar PDF nutricional ─────────────────────────────────────────────────
    pdf_b64      = gerar_pdf_base64(dados, plano_texto)
    nome_pdf     = nome_arquivo_pdf(nome, semanas_gestacao)
    pdf_nutri    = base64.b64decode(pdf_b64)

    # ── Selecionar links de treino ────────────────────────────────────────────
    links_treino = selecionar_links_exercicio(dados, trimestre_codigo)

    # ── Montar lista de PDFs para o email (apenas nutricao como anexo) ────────
    pdfs_email = [(pdf_nutri, nome_pdf)]

    # ── Enviar email ──────────────────────────────────────────────────────────
    # email ja validado no inicio da funcao — sempre presente aqui
    email_enviado = False
    email_erro    = ''

    try:
        enviar_email_pdf(email, nome, pdfs_email, links_treino=links_treino)
        email_enviado = True
        log.info(f"Email enviado para {email} — PDF nutricao + {len(links_treino)} link(s) de treino")
    except Exception as e:
        email_erro = str(e)
        log.error(f"Erro ao enviar email: {e}")

    log.info(f"[BG] Concluido para {nome} — email_enviado={email_enviado} erro='{email_erro}'")


# ── Endpoint de teste de email ────────────────────────────────────────────────

@app.route('/testar-email', methods=['POST'])
def testar_email():
    """Testa o envio de email sem gerar plano completo."""
    dados = request.get_json() or {}
    destinatario = dados.get('email', '').strip()
    nome_teste   = dados.get('nome', 'Teste')

    if not destinatario:
        return jsonify({"status": "erro", "mensagem": "Campo 'email' obrigatorio"}), 400

    try:
        pdf_fake = b'%PDF-1.4 teste'
        enviar_email_pdf(destinatario, nome_teste, [(pdf_fake, 'teste.pdf')])
        return jsonify({"status": "ok", "mensagem": f"Email enviado para {destinatario}"})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route('/')
def index():
    return 'API Gestar Bem operando!', 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
