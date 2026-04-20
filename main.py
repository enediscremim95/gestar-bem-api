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

import os, logging, re, threading, base64, json, traceback, atexit, time
import urllib.request, urllib.error
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
# timeout=300s: mesmo padrao do gunicorn, evita thread pendurada para sempre
_anthropic_client = anthropic.Anthropic(
    api_key=os.environ.get('ANTHROPIC_API_KEY'),
    timeout=300.0
)

# Tentativas por rodada e numero de rodadas antes de desistir
# Total: 3 rodadas x 3 tentativas = 9 tentativas ao longo de ~6 horas
TENTATIVAS_POR_RODADA = 3
MAX_RODADAS           = 3
MAX_TENTATIVAS_TOTAL  = TENTATIVAS_POR_RODADA * MAX_RODADAS  # 9
INTERVALO_RODADA_H    = 2  # horas de espera entre rodadas

# Email de alerta quando um job falha definitivamente
EMAIL_ALERTA = os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com')

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
        # Adiciona colunas novas se a tabela ja existia sem elas
        cur.execute("""
            ALTER TABLE planos_agendados
            ADD COLUMN IF NOT EXISTS tentativas INTEGER DEFAULT 0
        """)
        cur.execute("""
            ALTER TABLE planos_agendados
            ADD COLUMN IF NOT EXISTS proxima_tentativa TIMESTAMP
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
            WHERE processado = FALSE
            AND agendado_para <= NOW()
            AND tentativas < %s
            AND (proxima_tentativa IS NULL OR proxima_tentativa <= NOW())
            ORDER BY agendado_para
            LIMIT 5
            FOR UPDATE SKIP LOCKED
        """, (MAX_TENTATIVAS_TOTAL,))
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
        log.info(f"[FILA] Processando job {job_id} — {nome} (tentativa {tentativas + 1}/{MAX_TENTATIVAS_TOTAL})")
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
            nova_tentativa  = tentativas + 1
            desistir        = nova_tentativa >= MAX_TENTATIVAS_TOTAL
            completou_rodada = (nova_tentativa % TENTATIVAS_POR_RODADA == 0) and not desistir

            if desistir:
                status_log = "DESISTINDO definitivamente — enviando alerta"
                proxima    = None
            elif completou_rodada:
                proxima    = datetime.now(timezone.utc) + timedelta(hours=INTERVALO_RODADA_H)
                status_log = f"rodada completa — proxima tentativa em {INTERVALO_RODADA_H}h"
            else:
                proxima    = None
                status_log = "vai tentar de novo em ~1 min"

            log.error(f"[FILA] Erro no job {job_id} (tentativa {nova_tentativa}/{MAX_TENTATIVAS_TOTAL}) "
                      f"— {status_log}: {traceback.format_exc()}")

            conn3 = None
            try:
                conn3 = get_db()
                cur3  = conn3.cursor()
                cur3.execute("""
                    UPDATE planos_agendados
                    SET tentativas        = %s,
                        processado        = %s,
                        erro              = %s,
                        proxima_tentativa = %s,
                        processado_em     = CASE WHEN %s THEN NOW() ELSE NULL END
                    WHERE id = %s
                """, (nova_tentativa, desistir, str(e)[:500], proxima, desistir, job_id))
                conn3.commit()
                cur3.close()

                if desistir:
                    _enviar_alerta_falha(job_id, dados, nova_tentativa, str(e))

            except Exception:
                pass
            finally:
                if conn3:
                    conn3.close()
        finally:
            if conn2:
                conn2.close()


def _enviar_alerta_falha(job_id, dados, tentativas, erro):
    """Envia email de alerta para os responsaveis quando um job falha definitivamente."""
    sg_key    = os.environ.get('SENDGRID_API_KEY', '')
    remetente = 'planosgestarbem@gmail.com'
    # Destinatarios separados por virgula na variavel EMAIL_ALERTA
    destinatarios_str = os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com')
    destinatarios = [e.strip() for e in destinatarios_str.split(',') if e.strip()]

    if not sg_key:
        log.error("[ALERTA] SENDGRID_API_KEY nao configurado — nao foi possivel enviar alerta")
        return

    nome  = dados.get('nome', '?')
    email = dados.get('email', '?')

    corpo = f"""⚠️ ALERTA — Plano nao entregue após {tentativas} tentativas

Paciente: {nome}
Email: {email}
Job ID: {job_id}
Tentativas: {tentativas}
Ultimo erro: {erro[:300]}

Acesse o painel do Railway para verificar os logs e reprocessar manualmente se necessario."""

    payload = {
        "personalizations": [{"to": [{"email": d} for d in destinatarios]}],
        "from":    {"email": remetente, "name": "Gestar Bem — Sistema"},
        "subject": f"⚠️ FALHA: Plano de {nome} nao entregue",
        "content": [{"type": "text/plain", "value": corpo}],
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode('utf-8'),
        headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info(f"[ALERTA] Email de falha enviado para {destinatarios} — job {job_id} ({nome})")
    except Exception as ex:
        log.error(f"[ALERTA] Falha ao enviar alerta: {ex}")


def limpar_banco():
    """Remove registros processados com mais de 30 dias para nao acumular lixo no banco."""
    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            DELETE FROM planos_agendados
            WHERE processado = TRUE
            AND criado_em < NOW() - INTERVAL '270 days'
        """)
        deletados = cur.rowcount
        conn.commit()
        cur.close()
        if deletados > 0:
            log.info(f"[LIMPEZA] {deletados} registro(s) antigo(s) removido(s) do banco")
        else:
            log.info("[LIMPEZA] Nenhum registro para remover hoje")
    except Exception as e:
        log.error(f"[LIMPEZA] Erro ao limpar banco: {e}")
    finally:
        if conn:
            conn.close()


def check_diario():
    """Roda todo dia as 10h — verifica saude do sistema e envia relatorio por email."""
    log.info("[CHECK] Iniciando check diario do sistema")
    alertas   = []
    linhas    = []
    emoji_geral = "✅"

    # ── 1. Banco de dados ──────────────────────────────────────────────────
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE processado = FALSE)                         AS pendentes,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas > 0)      AS com_falha,
                COUNT(*) FILTER (WHERE processado = TRUE
                                 AND processado_em >= NOW() - INTERVAL '24 hours') AS enviados_24h
            FROM planos_agendados
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        pendentes    = row[0]
        com_falha    = row[1]
        enviados_24h = row[2]
        linhas.append(f"Banco: OK | Enviados hoje: {enviados_24h} | Pendentes: {pendentes} | Com falha: {com_falha}")
        if com_falha > 0:
            alertas.append(f"⚠️ {com_falha} plano(s) com falha — verificar logs no Railway")
            emoji_geral = "⚠️"
    except Exception as e:
        linhas.append(f"Banco: ERRO — {str(e)[:100]}")
        alertas.append("🔴 URGENTE: banco de dados inacessivel")
        emoji_geral = "🔴"
        enviados_24h = pendentes = com_falha = "?"

    # ── 2. Agendador ───────────────────────────────────────────────────────
    if _scheduler.running:
        linhas.append("Agendador: rodando")
    else:
        linhas.append("Agendador: PARADO")
        alertas.append("🔴 URGENTE: agendador parou — nenhum plano sera processado")
        emoji_geral = "🔴"

    # ── 3. SendGrid — uso do dia ───────────────────────────────────────────
    try:
        sg_key = os.environ.get('SENDGRID_API_KEY', '')
        hoje   = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        req_sg = urllib.request.Request(
            f"https://api.sendgrid.com/v3/stats?start_date={hoje}&end_date={hoje}",
            headers={"Authorization": f"Bearer {sg_key}"},
            method="GET"
        )
        with urllib.request.urlopen(req_sg, timeout=15) as resp:
            stats     = json.loads(resp.read().decode())
            emails_sg = stats[0]['stats'][0]['metrics']['emails_sent'] if stats and stats[0].get('stats') else 0
        pct = round(emails_sg / 100 * 100)
        linhas.append(f"SendGrid: {emails_sg}/100 emails hoje ({pct}%)")
        if emails_sg >= 80:
            alertas.append(f"⚠️ SendGrid: {emails_sg}/100 emails usados — proximo do limite diario")
            if emoji_geral == "✅":
                emoji_geral = "⚠️"
    except Exception as e:
        linhas.append(f"SendGrid: nao verificado ({str(e)[:80]})")

    # ── 4. Anthropic — chave valida ────────────────────────────────────────
    try:
        _anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}]
        )
        linhas.append("Anthropic API: OK")
    except Exception as e:
        err_str = str(e).lower()
        if 'credit' in err_str or 'billing' in err_str or 'quota' in err_str:
            linhas.append("Anthropic API: SEM CREDITO")
            alertas.append("🔴 URGENTE: credito Anthropic esgotado — recarregar em console.anthropic.com")
            emoji_geral = "🔴"
        elif '401' in err_str or 'invalid' in err_str or 'auth' in err_str:
            linhas.append("Anthropic API: CHAVE INVALIDA")
            alertas.append("🔴 URGENTE: ANTHROPIC_API_KEY invalida — verificar no Railway")
            emoji_geral = "🔴"
        else:
            linhas.append(f"Anthropic API: erro ({str(e)[:80]})")

    # ── 5. Montar e enviar relatorio ───────────────────────────────────────
    data_hora = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')
    situacao  = "tudo certo" if not alertas else "\n".join(alertas)
    acao      = "nenhuma"    if not alertas else "ver alertas acima"

    corpo = f"""{emoji_geral} CHECK DIARIO — Gestar Bem | {data_hora}

{chr(10).join(linhas)}

Situacao: {situacao}
Acao necessaria: {acao}

---
https://web-production-94437.up.railway.app/health"""

    # Enviar para todos os destinatarios de alerta
    sg_key2    = os.environ.get('SENDGRID_API_KEY', '')
    remetente  = 'planosgestarbem@gmail.com'
    dest_str   = os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com')
    destinatarios = [e.strip() for e in dest_str.split(',') if e.strip()]
    assunto    = f"{emoji_geral} Check diario Gestar Bem — {data_hora}"

    payload = {
        "personalizations": [{"to": [{"email": d} for d in destinatarios]}],
        "from":    {"email": remetente, "name": "Gestar Bem — Sistema"},
        "subject": assunto,
        "content": [{"type": "text/plain", "value": corpo}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode('utf-8'),
        headers={"Authorization": f"Bearer {sg_key2}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info(f"[CHECK] Relatorio enviado para {destinatarios} — {emoji_geral} {situacao}")
    except Exception as ex:
        log.error(f"[CHECK] Falha ao enviar relatorio: {ex}")


# Inicializar banco e agendador ao subir o servidor
init_db()
_scheduler = BackgroundScheduler(timezone='America/Sao_Paulo')
_scheduler.add_job(verificar_fila, 'interval', minutes=1, id='verificar_fila', max_instances=1)
_scheduler.add_job(limpar_banco,   'cron', hour=3,  minute=0,  id='limpar_banco',   max_instances=1)
_scheduler.add_job(check_diario,   'cron', hour=10, minute=7,  id='check_diario',   max_instances=1)
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    Retorna sempre os dois links de treino (academia + casa).
    Se houver limitacao fisica, o link de academia e substituido pelo PDF adaptado.
    Retorna lista vazia se paciente nao estiver liberada para exercicios.
    """
    liberado = str(dados.get('liberado_exercicio', '')).lower()
    if 'nao' in liberado or 'não' in liberado or not liberado.strip():
        log.info("Paciente nao liberada para exercicios — sem link de treino")
        return []

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

    tem_limit = bool(limit and limit.lower() not in
                     ('nao', 'não', 'nenhuma', 'nenhum', 'sem limitacao',
                      'sem limitação', 'nao tenho', 'não tenho', ''))

    base  = _base_url()
    links = []

    # ── Link 1: Academia (ou adaptado se houver limitacao) ──
    if tem_limit:
        full_path = selecionar_pdf_limitacao(limit, nivel, trimestre)
        rel   = os.path.relpath(full_path, PDF_BASE).replace('\\', '/')
        label = "Plano de Treinos — Academia (adaptado para sua limitacao)"
    else:
        rel   = f"academia/academia_{trimestre}_{nivel}.pdf"
        label = "Plano de Treinos — Academia"

    caminho_local = os.path.join(PDF_BASE, rel.replace('/', os.sep))
    if os.path.exists(caminho_local):
        links.append((f"{base}/treino/{rel}", label))
    else:
        log.warning(f"PDF academia nao encontrado: {caminho_local}")

    # ── Link 2: Casa — sempre enviado ──
    rel_casa      = f"casa/casa_{trimestre}.pdf"
    caminho_casa  = os.path.join(PDF_BASE, rel_casa)
    if os.path.exists(caminho_casa):
        links.append((f"{base}/treino/{rel_casa}", "Plano de Treinos — Casa"))
    else:
        log.warning(f"PDF casa nao encontrado: {caminho_casa}")

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


IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images')

@app.route('/imagens/<filename>')
def servir_imagem(filename):
    """Serve imagens do projeto (logo, ilustracao) para uso interno no painel."""
    caminho = os.path.abspath(os.path.join(IMAGES_DIR, filename))
    if not caminho.startswith(os.path.abspath(IMAGES_DIR)):
        abort(403)
    return send_from_directory(IMAGES_DIR, filename)


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


MAX_TENTATIVAS_BG = 3  # tentativas no modo fallback (sem banco)

def _processar_em_background(dados):
    """Executa processamento em thread separada (modo fallback sem banco).
    Retenta automaticamente ate MAX_TENTATIVAS_BG vezes com intervalo de 60s."""
    nome  = dados.get('nome', 'Paciente')
    email = dados.get('email', '')
    for tentativa in range(1, MAX_TENTATIVAS_BG + 1):
        try:
            log.info(f"[BG] Tentativa {tentativa}/{MAX_TENTATIVAS_BG} para {nome}")
            with app.app_context():
                _gerar_plano_interno(dados)
            log.info(f"[BG] Concluido com sucesso para {nome}")
            return
        except Exception:
            log.error(f"[BG] Erro na tentativa {tentativa}/{MAX_TENTATIVAS_BG} para {nome} ({email}): {traceback.format_exc()}")
            if tentativa < MAX_TENTATIVAS_BG:
                log.info(f"[BG] Aguardando 60s antes da proxima tentativa...")
                time.sleep(60)
    log.error(f"[BG] Desistindo apos {MAX_TENTATIVAS_BG} tentativas para {nome} ({email})")


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
    usa_insulina       = dados.get('usa_insulina', '')
    quadros_clinicos   = dados.get('quadros_clinicos', '')
    alergia_alimentos  = dados.get('alergia_alimentos', '')
    preferencia        = dados.get('preferencia', '')

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
    prompt = f"""Voce e Dra. Jessica D'Agostini, nutricionista especialista em gestacao da equipe Gestar Bem.
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
- Alergia a alimentos: {alergia_alimentos}
- Horario de mais fome: {horario_fome}
- Observacoes adicionais: {observacoes}
- Quadros clinicos relatados pela paciente: {quadros_clinicos}
- Usa insulina para diabetes gestacional: {usa_insulina}
- Preferencia da paciente: {preferencia}

{bloco_calculos}

{contexto_trimestre}

PROTOCOLO CLINICO — REGRAS QUE VOCE SEGUE RIGOROSAMENTE:

1. ANALISE DE EXAMES E QUADROS CLINICOS:
   DETECCAO DE DIABETES GESTACIONAL: aplique as condutas de DG se QUALQUER uma das condicoes abaixo for verdadeira:
   a) "DIABETES GESTACIONAL" aparece no campo "Quadros clinicos relatados pela paciente"
   b) Glicose em jejum >= 92 mg/dL (se valor informado em texto)
   Se DG confirmado: plano com controle glicemico rigoroso, reducao de carboidratos simples,
   ceia obrigatoria, alertas de medicao em vermelho (ver regra especial DG).
   - Glicose 90-91 mg/dL (se valor informado) → Risco: dieta preventiva com controle de carboidratos simples
   - Glicose < 90 mg/dL (se valor informado) → Normal: plano flexivel

   OUTROS QUADROS CLINICOS: aplique condutas especificas para qualquer condicao informada:
   - PRE-ECLAMPSIA / HIPERTENSAO → reducao de sodio, alimentos anti-inflamatorios, hidratacao
   - ANEMIA → ferro heme + vitamina C + suplemento de ferro (ver protocolo)
   - OBESIDADE / SOBREPESO → deficit calorico controlado e seguro (nunca abaixo do minimo gestacional)
   - HIPOTIREOIDISMO → considerar alimentos que suportam funcao tireoidiana; evitar excesso de brassicas cruas
   - SOP / ENDOMETRIOSE → anti-inflamatorio, baixo glicemico
   - Vitamina D < 50 → Indicar suplementacao (ver protocolo de suplementacao) + alimentos fontes (sardinha, ovos, funghi)
   - Vitamina D >= 50 → NAO indicar suplemento de vitamina D
   - B12 < 600 → Indicar suplementacao (ver protocolo) — especialmente se vegetariana/vegana
   - B12 >= 600 → NAO indicar suplemento de B12
   - Ferritina < 70 → Estrategia alimentar com ferro heme + vitamina C + indicar suplemento de ferro
   - Ferritina >= 70 → NAO indicar suplemento de ferro

2. ESTRUTURA DAS REFEICOES (obrigatoria):
   - 5 a 7 refeicoes por dia
   - Intervalo maximo de 3 horas entre refeicoes
   - PROTEINA OBRIGATORIA EM TODAS AS REFEICOES — nunca so carboidrato
   - Sem jejum — sem longos periodos sem comer
   - Se treina cedo: incluir pre-treino antes do exercicio
   - Se diabetes gestacional: incluir CEIA obrigatoria
   - Se diabetes gestacional COM USO DE INSULINA: regras adicionais OBRIGATORIAS:
     * CEIA obrigatoria (nunca dormir sem comer)
     * Refeicoes a cada 3 horas sem excecao — nenhum intervalo maior que 3h
     * Distribuir carboidratos de forma uniforme ao longo do dia (evitar pico glicemico)
     * Sempre proteina + gordura boa junto com carboidrato para retardar absorcao
     * Nunca refeicao so de carboidrato — risco de hipoglicemia
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

4. SUPLEMENTACAO — PROTOCOLO OFICIAL DRA. JESSICA:
   Marcas preferidas (priorizar nesta ordem): DUX (cupom: PACJESSICADAGOSTINI), Vitafor, Puravida, Essential.

   REGRAS POR TRIMESTRE:
   - 1o TRIMESTRE: Metilfolato 400mcg ao dia (nao acido folico comum — usar metilfolato).
   - 2o e 3o TRIMESTRE: Polivitaminico especifico para gestantes.
     Marcas indicadas: Regenesis, Ogestan, Femibion ou Feminis.
     Posologia: conforme embalagem do fabricante de cada marca.

   SUPLEMENTOS CONDICIONAIS (somente se exame indicar necessidade):
   - Vitamina D: suplementar SOMENTE SE exame abaixo de 50 ng/mL.
     Marcas: Addera, DUX, Vitafor ou outras das marcas preferidas acima.
   - Vitamina B12: suplementar SOMENTE SE exame abaixo de 600 pg/mL.
     Marcas das preferidas acima.
   - Ferro: suplementar SOMENTE SE ferritina abaixo de 70 ng/mL.
     Marcas das preferidas acima.

   SUPLEMENTO SEMPRE INDICADO:
   - Omega-3 DHA: seguro e importante para o desenvolvimento cerebral do bebe.
     Marcas das preferidas acima.

   IMPORTANTE: NAO inclua a frase "confirme com seu medico antes de iniciar qualquer suplemento".
   Este protocolo ja foi validado clinicamente pela Dra. Jessica D'Agostini.

5. LINGUAGEM E TOM:
   - Acolhedor, pessoal, cristao
   - Trate sempre pelo primeiro nome
   - Palavras de encorajamento, proposito e fe
   - Nunca tom clinico frio — sempre humanizado
   - Adapte o tom ao momento do trimestre (ver contexto acima)

6. CONSISTENCIA DO PLANO — REGRA CRITICA:
   ANTES de gerar o plano, defina o perfil alimentar da paciente:
   - Se ela relatou intolerancia a lactose: NENHUMA refeicao pode ter leite, queijo, iogurte convencional
   - Se ela relatou ser vegetariana ou vegana: NENHUMA refeicao pode ter carne, frango, peixe ou ovos (vegana)
   - Se ela come carne/frango/peixe (onivora): TODAS as refeicoes devem ter proteina animal coerente
   - NUNCA misture perfis: se o almoco tem frango, o jantar nao pode parecer vegetariano
   - As substituicoes devem ser do MESMO perfil alimentar que a opcao principal
   - Antes de finalizar, releia o plano completo e verifique:
     * Todas as refeicoes tem proteina?
     * Todas as refeicoes sao coerentes com o perfil alimentar definido?
     * Nenhuma refeicao contradiz outra?
   - Se ela relatou ALERGIA a algum alimento: esse alimento e COMPLETAMENTE PROIBIDO em todo o plano,
     inclusive nas substituicoes. Alergia e diferente de intolerancia — risco de reacao grave.
   - Um plano inconsistente e INACEITAVEL e passa falta de profissionalismo

7. EXERCICIOS — REGRA ABSOLUTA:
   NUNCA gere secao, paragrafo ou qualquer orientacao sobre exercicios fisicos neste PDF.
   Os planos de treino sao enviados separadamente como arquivos proprios (academia e casa).
   Os dados de exercicio informados (nivel, periodo, limitacoes) servem APENAS para
   ajustar horarios e composicao das refeicoes (ex: pre-treino, pos-treino).
   Qualquer mencao a exercicios fora do contexto nutricional e PROIBIDA neste documento.

8. ALIMENTOS ESSENCIAIS POR TRIMESTRE — OBRIGATORIOS NO CARDAPIO:
   Estes alimentos NAO PODEM FALTAR no plano alimentar, exceto se a paciente relatou
   alergia, intolerancia ou pediu para nao incluir. Adapte para o perfil alimentar dela
   (ex: para vegetarianas, substitua carnes/ovos por equivalentes vegetais de igual valor nutricional).

   1o TRIMESTRE — obrigatorios:
   Folhosos verde-escuros (espinafre, couve, brocolis, rucula), ovos, carnes magras (bovina, frango),
   figado, frutas ricas em vitamina C (laranja, acerola, kiwi, morango), abacate,
   peixes seguros (sardinha, salmao, tilapia), oleaginosas (castanha-do-para, nozes, amendoas),
   leguminosas (feijao, lentilha, grao-de-bico), laticinios ou fontes de calcio (leite, iogurte, queijo pasteurizado).

   2o TRIMESTRE — obrigatorios:
   Folhosos verde-escuros (couve, espinafre, brocolis, rucula), ovos, figado e coracao de galinha,
   carnes magras (bovina, frango), peixes seguros (tilapia, sardinha, salmao),
   frutas variadas (laranja, banana, maca, mamao, frutas vermelhas),
   leguminosas (feijao, lentilha, grao-de-bico), oleaginosas (castanha-do-para, nozes, amendoas),
   abacate, laticinios ou fontes de calcio (leite, iogurte, queijo pasteurizado),
   cereais integrais (aveia, arroz integral, quinoa), sementes (chia, linhaca),
   tuberculos e raizes (batata-doce, mandioca, inhame),
   verduras e legumes variados (cenoura, abobrinha, beterraba, tomate), azeite de oliva.

   3o TRIMESTRE — obrigatorios:
   Folhosos verde-escuros (couve, espinafre, brocolis), ovos, figado e coracao de galinha,
   carnes magras (bovina, frango), peixes seguros (tilapia, sardinha, salmao),
   leguminosas (feijao, lentilha, grao-de-bico), laticinios ou fontes de calcio (leite, iogurte, queijo pasteurizado),
   frutas (banana, pera, maca, mamao), frutas ricas em vitamina C (laranja, acerola, kiwi),
   oleaginosas (castanha-do-para, nozes, amendoas), sementes (chia, linhaca),
   cereais integrais (aveia, arroz integral), tuberculos (batata-doce, mandioca, inhame),
   verduras e legumes variados, azeite de oliva, agua de coco.

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
Mencione que os materiais de apoio estao disponiveis na plataforma The Members, onde ela recebeu acesso por email no ato da compra.
NAO mencione a plataforma Kiwify.

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
Como pesar alimentos, horarios ideais, como substituir alimentos.
Dicas praticas do dia a dia. Inclua dicas especificas para os desafios do trimestre atual.
Mencione o app Fat Secret APENAS como recurso opcional para quem nao conseguir seguir o cardapio proposto e quiser registrar o que comeu no lugar — nao e obrigatorio e nao e o metodo principal.

---

## PLANO ALIMENTAR COMPLETO
Para cada refeicao: opcao principal + MINIMO 5 opcoes de substituicao.
Inclua porcoes em gramas em todas as opcoes. Proteina em TODAS as refeicoes.
Refeicoes: Cafe da manha / Lanche da manha / Almoco / Lanche da tarde / Jantar / Ceia (se necessario).
Adapte conforme horario de fome, rotina, intolerancia alimentar e desafios do trimestre.
As substituicoes devem ser coerentes com o perfil alimentar da paciente (ver regra 6).
NUNCA coloque substituicao vegetariana se o perfil da paciente e onivoro — mantenha proteina animal.
NUNCA coloque carne/frango se a paciente for vegetariana ou tiver intolerancia informada.

REGRA ESPECIAL — DIABETES GESTACIONAL (somente se glicose >= 92 mg/dL):
Se a paciente tiver diabetes gestacional, inclua os seguintes alertas de medicao em vermelho
EXATAMENTE nestes momentos do plano, usando o marcador "ATENCAO:" para cada um:

Antes do Cafe da manha:
ATENCAO: Medicao em jejum — objetivo: menos de 95 mg/dL

Apos o Cafe da manha (logo depois das opcoes da refeicao):
ATENCAO: Medicao 1h apos a primeira garfada — objetivo: menos de 140 mg/dL

Apos o Almoco (logo depois das opcoes da refeicao):
ATENCAO: Medicao 1h apos a primeira garfada — objetivo: menos de 140 mg/dL

Apos o Jantar (logo depois das opcoes da refeicao):
ATENCAO: Medicao 1h apos a primeira garfada — objetivo: menos de 140 mg/dL

Se a paciente NAO tiver diabetes gestacional, NAO inclua esses alertas.

---

## CONSIDERACOES FINAIS
Encerramento com encorajamento especifico para o momento do trimestre,
lembretes dos pontos mais importantes do plano,
e informacoes de contato da equipe Gestar Bem.

Gere o plano COMPLETO, detalhado e personalizado. Minimo de 1800 palavras.
Use os calculos clinicos ja fornecidos — nao recalcule, nao mude os valores.

ANTES DE ENTREGAR O PLANO, FACA UMA REVISAO INTERNA:
1. O perfil alimentar e consistente do inicio ao fim? (se usou frango no almoco, o jantar tambem tem proteina animal?)
2. Todas as refeicoes tem proteina em gramas especificadas?
3. As intolerancias informadas foram respeitadas em TODAS as refeicoes e substituicoes?
4. Nenhuma refeicao contradiz outra em termos de perfil alimentar?
Se encontrar qualquer inconsistencia, corrija antes de entregar."""

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
    # NAO capturamos a excecao aqui: se o email falhar, o erro sobe para
    # verificar_fila() que vai retentar o job automaticamente (ate MAX_TENTATIVAS)
    enviar_email_pdf(email, nome, pdfs_email, links_treino=links_treino)
    log.info(f"[INTERNO] Concluido para {nome} — email enviado para {email} com {len(links_treino)} link(s) de treino")


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


@app.route('/painel')
def painel():
    """Dashboard simples — protegido por PAINEL_TOKEN."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2 style="font-family:sans-serif;color:#c00">Acesso negado. Informe ?token=SENHA na URL.</h2>', 403

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()

        # Resumo geral
        cur.execute("""
            SELECT
                COUNT(*)                                                                    AS total,
                COUNT(*) FILTER (WHERE processado = TRUE
                                 AND processado_em >= NOW() - INTERVAL '24 hours')         AS hoje,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas = 0
                                 AND (proxima_tentativa IS NULL OR proxima_tentativa <= NOW())) AS pendentes,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas > 0)              AS com_falha,
                COUNT(*) FILTER (WHERE processado = TRUE)                                  AS concluidos
            FROM planos_agendados
        """)
        r = cur.fetchone()
        total, hoje, pendentes, com_falha, concluidos = r

        # Ultimos 20 registros
        cur.execute("""
            SELECT
                id,
                dados->>'nome'  AS nome,
                dados->>'email' AS email,
                agendado_para,
                processado,
                tentativas,
                processado_em,
                erro
            FROM planos_agendados
            ORDER BY criado_em DESC
            LIMIT 20
        """)
        registros = cur.fetchall()
        cur.close()
    except Exception as e:
        return f'<h2>Erro ao consultar banco: {e}</h2>', 500
    finally:
        if conn:
            conn.close()

    def linha_cor(processado, tentativas, erro):
        if processado and not erro:
            return '#e8f5e9'  # verde claro
        if not processado and tentativas > 0:
            return '#fff3e0'  # laranja claro
        if erro:
            return '#ffebee'  # vermelho claro
        return '#ffffff'

    linhas_html = ''
    for reg in registros:
        rid, nome, email, agendado, processado, tentativas, processado_em, erro = reg
        status = '✅ Enviado' if processado and not erro else (f'⚠️ Tentativas: {tentativas}' if not processado else '❌ Falhou')
        cor    = linha_cor(processado, tentativas, erro)
        ag_str = agendado.strftime('%d/%m %H:%M') if agendado else '-'
        pr_str = processado_em.strftime('%d/%m %H:%M') if processado_em else '-'
        erro_str = (erro[:60] + '...') if erro and len(erro) > 60 else (erro or '')
        linhas_html += f"""
        <tr style="background:{cor}">
            <td>{rid}</td>
            <td>{nome or '-'}</td>
            <td style="font-size:12px">{email or '-'}</td>
            <td>{ag_str}</td>
            <td>{pr_str}</td>
            <td>{status}</td>
            <td style="font-size:11px;color:#c00">{erro_str}</td>
            <td><a href="/painel/detalhes/{rid}?token={token_recebido}" style="color:#9B27AF;text-decoration:none;font-size:18px;" title="Ver detalhes">👁</a></td>
        </tr>"""

    agora = datetime.now(timezone(timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Painel — Gestar Bem</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: 'Inter', sans-serif; background: #f8f4fb; margin: 0; padding: 0; }}
    .header {{ background: #fff; padding: 16px 28px; display: flex; align-items: center; gap: 16px; box-shadow: 0 2px 8px rgba(155,39,175,.15); border-bottom: 3px solid #9B27AF; }}
    .header .titulo {{ color: #9B27AF; font-family: 'Playfair Display', serif; font-size: 22px; letter-spacing: 0.5px; }}
    .header .sub-header {{ color: #aaa; font-size: 12px; margin-top: 2px; }}
    .content {{ padding: 24px 28px; }}
    .sub {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
    .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }}
    .card  {{ background: #fff; border-radius: 12px; padding: 20px 28px;
               box-shadow: 0 1px 6px rgba(155,39,175,.1); min-width: 120px; text-align: center; border-top: 3px solid #9B27AF; }}
    .card .num  {{ font-size: 36px; font-weight: bold; color: #9B27AF; }}
    .card .lab  {{ font-size: 13px; color: #555; margin-top: 4px; }}
    .card.alerta .num {{ color: #e65100; }}
    .card.alerta     {{ border-top-color: #e65100; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff;
              border-radius: 12px; overflow: hidden;
              box-shadow: 0 1px 6px rgba(155,39,175,.1); }}
    th    {{ background: #9B27AF; color: #fff; padding: 11px 14px;
              text-align: left; font-size: 13px; font-weight: 500; }}
    .btn-voltar {{ display:inline-block;margin-bottom:20px;color:#9B27AF;text-decoration:none;font-size:14px; }}
    td    {{ padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #f0e6f6; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fdf6ff; }}
  </style>
</head>
<body>
  <div class="header">
    <div style="background:#fff;border-radius:50%;width:60px;height:60px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
      <img src="/imagens/gestar_ilustracao.png" alt="Logo" style="height:52px;">
    </div>
    <img src="/imagens/gestar_bem_svg.png" alt="Gestar Bem" style="height:44px;">
    <div>
      <div class="titulo">Painel de Controle</div>
      <div class="sub-header">Sistema Gestar Bem</div>
    </div>
  </div>
  <div class="content">
  <div class="sub">Atualizado em {agora} (Brasília) &nbsp;|&nbsp; Atualiza automaticamente a cada 60s</div>

  <form method="GET" action="/painel/buscar" style="margin-bottom:28px;display:flex;gap:10px;align-items:center;">
    <input type="hidden" name="token" value="{token_recebido}">
    <input type="email" name="email" placeholder="Buscar paciente por email..." required
           style="flex:1;max-width:400px;padding:10px 14px;border:1px solid #d8b4e8;border-radius:8px;font-size:14px;outline:none;">
    <button type="submit"
            style="background:#9B27AF;color:#fff;border:none;padding:10px 22px;border-radius:8px;font-size:14px;cursor:pointer;">
      🔍 Buscar
    </button>
  </form>

  <div class="cards">
    <div class="card"><div class="num">{hoje}</div><div class="lab">Enviados hoje</div></div>
    <div class="card"><div class="num">{pendentes}</div><div class="lab">Pendentes</div></div>
    <div class="card {'alerta' if com_falha > 0 else ''}"><div class="num">{com_falha}</div><div class="lab">Com falha</div></div>
    <div class="card"><div class="num">{concluidos}</div><div class="lab">Total concluídos</div></div>
    <div class="card"><div class="num">{total}</div><div class="lab">Total geral</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>Paciente</th><th>Email</th>
        <th>Agendado</th><th>Processado</th><th>Status</th><th>Erro</th>
      </tr>
    </thead>
    <tbody>{linhas_html}</tbody>
  </table>
  </div>
</body>
</html>"""
    return html, 200


@app.route('/health')
def health():
    """
    Checagem completa da saude do sistema.
    Retorna 200 se tudo ok, 500 se qualquer componente critico falhar.
    Util para monitoramento externo (Railway, UptimeRobot, etc.).
    """
    resultado = {
        "status":     "ok",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "componentes": {}
    }
    status_geral = 200

    # ── 1. Banco de dados ───────────────────────────────────────────────────
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE processado = FALSE)                          AS pendentes,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas > 0)       AS com_falha,
                COUNT(*) FILTER (WHERE processado = TRUE
                                 AND processado_em >= NOW() - INTERVAL '24 hours')  AS enviados_24h
            FROM planos_agendados
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        resultado["componentes"]["banco"] = {
            "status":        "ok",
            "pendentes":     row[0],
            "com_falha":     row[1],
            "enviados_24h":  row[2],
        }
    except Exception as e:
        resultado["componentes"]["banco"] = {"status": "ERRO", "detalhe": str(e)[:200]}
        resultado["status"] = "degradado"
        status_geral = 500

    # ── 2. Agendador (APScheduler) ──────────────────────────────────────────
    try:
        if _scheduler.running:
            jobs = {j.id: str(j.next_run_time) for j in _scheduler.get_jobs()}
            resultado["componentes"]["agendador"] = {"status": "ok", "jobs": jobs}
        else:
            resultado["componentes"]["agendador"] = {"status": "PARADO"}
            resultado["status"] = "degradado"
            status_geral = 500
    except Exception as e:
        resultado["componentes"]["agendador"] = {"status": "ERRO", "detalhe": str(e)[:200]}
        resultado["status"] = "degradado"
        status_geral = 500

    # ── 3. Variaveis de ambiente criticas ────────────────────────────────────
    vars_criticas = {
        "ANTHROPIC_API_KEY": bool(os.environ.get('ANTHROPIC_API_KEY')),
        "SENDGRID_API_KEY":  bool(os.environ.get('SENDGRID_API_KEY')),
        "DATABASE_URL":      bool(os.environ.get('DATABASE_URL')),
    }
    vars_ausentes = [k for k, v in vars_criticas.items() if not v]
    if vars_ausentes:
        resultado["componentes"]["env_vars"] = {
            "status":   "ERRO",
            "ausentes": vars_ausentes
        }
        resultado["status"] = "degradado"
        status_geral = 500
    else:
        resultado["componentes"]["env_vars"] = {"status": "ok", "todas_configuradas": True}

    # ── 4. Configuracoes ─────────────────────────────────────────────────────
    resultado["componentes"]["config"] = {
        "delay_horas":           DELAY_HORAS,
        "max_tentativas_total":  MAX_TENTATIVAS_TOTAL,
        "tentativas_por_rodada": TENTATIVAS_POR_RODADA,
        "intervalo_rodada_h":    INTERVALO_RODADA_H,
        "email_alerta":          bool(os.environ.get('EMAIL_ALERTA')),
    }

    return jsonify(resultado), status_geral


def _painel_html_base(token, conteudo_html):
    """Retorna o HTML completo do painel com header padrao."""
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Painel — Gestar Bem</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
  <style>
    body {{ font-family:'Inter',sans-serif; background:#f8f4fb; margin:0; padding:0; }}
    .header {{ background:#fff; padding:16px 28px; display:flex; align-items:center; gap:16px; box-shadow:0 2px 8px rgba(155,39,175,.15); border-bottom:3px solid #9B27AF; }}
    .header .titulo {{ color:#9B27AF; font-family:'Playfair Display',serif; font-size:22px; }}
    .header .sub-header {{ color:#aaa; font-size:12px; margin-top:2px; }}
    .content {{ padding:24px 28px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 6px rgba(155,39,175,.1); }}
    th {{ background:#9B27AF; color:#fff; padding:11px 14px; text-align:left; font-size:13px; }}
    td {{ padding:10px 14px; font-size:13px; border-bottom:1px solid #f0e6f6; vertical-align:top; }}
    tr:last-child td {{ border-bottom:none; }}
    tr:hover td {{ background:#fdf6ff; }}
    .btn {{ display:inline-block; background:#9B27AF; color:#fff; padding:8px 18px; border-radius:8px; text-decoration:none; font-size:13px; }}
    .btn-voltar {{ display:inline-block; margin-bottom:20px; color:#9B27AF; text-decoration:none; font-size:14px; }}
    .label {{ color:#888; font-size:12px; }}
    .valor {{ font-weight:500; }}
  </style>
</head>
<body>
  <div class="header">
    <div style="background:#fff;border-radius:50%;width:60px;height:60px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
      <img src="/imagens/gestar_ilustracao.png" alt="Logo" style="height:52px;">
    </div>
    <img src="/imagens/gestar_bem_svg.png" alt="Gestar Bem" style="height:44px;">
    <div>
      <div class="titulo">Painel de Controle</div>
      <div class="sub-header">Sistema Gestar Bem</div>
    </div>
  </div>
  <div class="content">
    {conteudo_html}
  </div>
</body></html>"""


@app.route('/painel/buscar')
def painel_buscar():
    """Busca historico de envios por email da paciente."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2>Acesso negado.</h2>', 403

    email_busca = request.args.get('email', '').strip().lower()
    if not email_busca:
        return painel()

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT id,
                   dados->>'nome'               AS nome,
                   dados->>'email'              AS email,
                   dados->>'semanas_gestacao'   AS semanas,
                   dados->>'peso_atual'         AS peso,
                   dados->>'complicacoes'       AS complicacoes,
                   dados->>'sintomas'           AS sintomas,
                   dados->>'medicamentos'       AS medicamentos,
                   processado,
                   tentativas,
                   criado_em
            FROM planos_agendados
            WHERE LOWER(dados->>'email') = %s
            ORDER BY criado_em ASC
        """, (email_busca,))
        registros = cur.fetchall()
        cur.close()
    except Exception as e:
        return f'<h2>Erro: {e}</h2>', 500
    finally:
        if conn: conn.close()

    if not registros:
        conteudo = f"""
        <a href="/painel?token={token_recebido}" class="btn-voltar">← Voltar ao painel</a>
        <h3 style="color:#9B27AF">Nenhum registro encontrado para: {email_busca}</h3>"""
        return _painel_html_base(token_recebido, conteudo), 200

    nome_paciente = registros[-1][1] or email_busca
    peso_inicial  = registros[0][4]  or '?'
    peso_atual    = registros[-1][4] or '?'

    linhas = ''
    for i, reg in enumerate(registros):
        rid, nome, email, semanas, peso, complic, sintomas, medic, processado, tentativas, criado_em = reg
        status   = '✅' if processado else f'⚠️ {tentativas}x'
        data_str = criado_em.strftime('%d/%m/%Y') if criado_em else '-'
        tri      = 'III' if semanas and int(''.join(filter(str.isdigit, semanas or '0')) or '0') > 26 else ('II' if semanas and int(''.join(filter(str.isdigit, semanas or '0')) or '0') > 13 else 'I')
        linhas  += f"""
        <tr>
          <td>{data_str}</td>
          <td>{semanas or '-'} sem &nbsp;<span style="color:#9B27AF;font-size:11px">{tri}º tri</span></td>
          <td>{peso or '-'} kg</td>
          <td style="font-size:12px">{(complic or '-')[:60]}</td>
          <td style="font-size:12px">{(sintomas or '-')[:60]}</td>
          <td style="font-size:12px">{(medic or '-')[:40]}</td>
          <td>{status}</td>
          <td><a href="/painel/detalhes/{rid}?token={token_recebido}" title="Ver detalhes" style="color:#9B27AF;font-size:18px;text-decoration:none;">👁</a></td>
        </tr>"""

    conteudo = f"""
    <a href="/painel?token={token_recebido}" class="btn-voltar">← Voltar ao painel</a>
    <h2 style="color:#9B27AF;margin-bottom:4px">🌸 {nome_paciente}</h2>
    <p style="color:#888;font-size:13px;margin-top:0">{email_busca} &nbsp;|&nbsp; {len(registros)} envio(s) &nbsp;|&nbsp; Peso inicial: {peso_inicial}kg → Atual: {peso_atual}kg</p>
    <table>
      <thead><tr>
        <th>Data</th><th>Semanas</th><th>Peso</th><th>Complicações</th><th>Sintomas</th><th>Medicamentos</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>{linhas}</tbody>
    </table>"""

    return _painel_html_base(token_recebido, conteudo), 200


@app.route('/painel/detalhes/<int:job_id>')
def painel_detalhes(job_id):
    """Exibe todos os dados de um envio especifico."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2>Acesso negado.</h2>', 403

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT dados, criado_em, processado, tentativas, erro FROM planos_agendados WHERE id = %s", (job_id,))
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        return f'<h2>Erro: {e}</h2>', 500
    finally:
        if conn: conn.close()

    if not row:
        return '<h2>Registro nao encontrado.</h2>', 404

    dados, criado_em, processado, tentativas, erro = row
    nome  = dados.get('nome', '-')
    email = dados.get('email', '-')

    CAMPOS_LABELS = [
        ('nome', 'Nome'), ('email', 'Email'), ('pais', 'País'),
        ('idade', 'Idade'), ('altura', 'Altura'), ('semanas_gestacao', 'Semanas de gestação'),
        ('peso_atual', 'Peso atual'), ('peso_antes', 'Peso antes da gestação'),
        ('peso_primeira_consulta', 'Peso 1ª consulta'), ('complicacoes', 'Complicações'),
        ('medicamentos', 'Medicamentos'), ('suplementos', 'Suplementos'),
        ('gravidez_planejada', 'Gravidez planejada'), ('sintomas', 'Sintomas'),
        ('outros_sintomas', 'Outros sintomas'), ('sono', 'Qualidade do sono'),
        ('medo_gravidez', 'Medos/preocupações'), ('liberado_exercicio', 'Liberada p/ exercícios'),
        ('nivel_exercicio', 'Nível de exercício'), ('periodo_exercicio', 'Período preferido'),
        ('limitacao_exercicio', 'Limitações físicas'), ('rotina_alimentacao', 'Rotina alimentar'),
        ('hidratacao', 'Hidratação atual'), ('intolerancia', 'Intolerância alimentar'),
        ('horario_fome', 'Horário de mais fome'), ('observacoes', 'Observações'),
    ]

    linhas = ''
    for chave, label in CAMPOS_LABELS:
        valor = dados.get(chave, '')
        if valor:
            linhas += f'<tr><td class="label">{label}</td><td class="valor">{valor}</td></tr>'

    status  = '✅ Enviado com sucesso' if processado and not erro else (f'⚠️ {tentativas} tentativa(s)' if not processado else f'❌ Falhou após {tentativas}x')
    data_str = criado_em.strftime('%d/%m/%Y às %H:%M') if criado_em else '-'
    email_enc = dados.get('email', '')
    voltar_busca = f"/painel/buscar?token={token_recebido}&email={email_enc}"

    conteudo = f"""
    <a href="{voltar_busca}" class="btn-voltar">← Voltar ao histórico de {nome}</a>
    <h2 style="color:#9B27AF;margin-bottom:4px">📋 Detalhes do envio #{job_id}</h2>
    <p style="color:#888;font-size:13px;margin-top:0">{data_str} &nbsp;|&nbsp; {status}</p>
    <table style="max-width:700px">
      <tbody>{linhas}</tbody>
    </table>
    {'<p style="color:#c00;margin-top:16px;font-size:13px"><strong>Erro:</strong> ' + erro + '</p>' if erro else ''}"""

    return _painel_html_base(token_recebido, conteudo), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
