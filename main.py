# -*- coding: utf-8 -*-
"""
main.py — Fly.io Flask API — Gestar Bem
Recebe dados do formulario via Apps Script, calcula TMB/macros,
gera plano com Claude, converte em PDF e envia por email.

Formula TMB: Mifflin-St Jeor
  Mulheres: (10 x peso) + (6,25 x altura) - (5 x idade) - 161
Fator atividade:
  Sedentaria     = 1.2
  Leve           = 1.37
  Moderada       = 1.55
  Avancada/Intensa = 1.7
"""

import os, logging, re, threading, base64, json, traceback, atexit, time, secrets, html as _html, unicodedata
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json as PgJson
from apscheduler.schedulers.background import BackgroundScheduler

from flask import Flask, request, jsonify, send_from_directory, abort, redirect
import anthropic
from pdf_generator import gerar_pdf_base64, nome_arquivo_pdf

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024  # 60MB — suporta ~20 imagens de exame
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Fuso horário de Brasília (UTC-3) — usado em exibições de data/hora no painel e relatórios
TZ_SP = timezone(timedelta(hours=-3))

# Cliente Anthropic — criado uma vez na inicializacao do servidor
# timeout=300s: mesmo padrao do gunicorn, evita thread pendurada para sempre
_anthropic_client = anthropic.Anthropic(
    api_key=os.environ.get('ANTHROPIC_API_KEY'),
    timeout=300.0
)

class DadosInvalidosError(Exception):
    """Levantada quando os dados do formulário têm erro crítico que impede gerar o plano."""
    pass


class AguardandoAprovacaoError(Exception):
    """Plano gerado mas aguardando aprovação da equipe antes do envio à paciente."""
    def __init__(self, motivo, pdf_b64):
        super().__init__(motivo)
        self.pdf_b64 = pdf_b64


def _enviar_email_sg(destinatarios, assunto, corpo_txt, corpo_html=None,
                     nome_remetente='Gestar Bem — Sistema'):
    """
    Helper central para envio de email via SendGrid.
    `destinatarios` pode ser str (um dest) ou list[str].
    Retorna True se enviou com sucesso, False se falhou.
    """
    sg_key = os.environ.get('SENDGRID_API_KEY', '')
    if not sg_key:
        log.error(f"[EMAIL] SENDGRID_API_KEY não configurado — não foi possível enviar: {assunto[:60]}")
        return False
    if isinstance(destinatarios, str):
        destinatarios = [destinatarios]
    destinatarios = [d for d in destinatarios if d and '@' in d]
    if not destinatarios:
        log.error(f"[EMAIL] Nenhum destinatário válido para: {assunto[:60]}")
        return False

    content = [{"type": "text/plain", "value": corpo_txt}]
    if corpo_html:
        content.append({"type": "text/html", "value": corpo_html})

    payload = {
        "personalizations": [{"to": [{"email": d} for d in destinatarios]}],
        "from":    {"email": "planos@programagestarbem.com.br", "name": nome_remetente},
        "reply_to": {"email": "planosgestarbem@gmail.com", "name": nome_remetente},
        "subject": assunto,
        "content": content,
        "tracking_settings": {"click_tracking": {"enable": False}},
    }
    try:
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode('utf-8'),
            headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=30)
        log.info(f"[EMAIL] '{assunto[:60]}' → {destinatarios}")
        return True
    except Exception as ex:
        log.error(f"[EMAIL] Falha ao enviar '{assunto[:60]}': {ex}")
        return False


# Tentativas por rodada e numero de rodadas antes de desistir
# Total: 3 rodadas x 3 tentativas = 9 tentativas ao longo de ~6 horas
TENTATIVAS_POR_RODADA = 3
MAX_RODADAS           = 3
MAX_TENTATIVAS_TOTAL  = TENTATIVAS_POR_RODADA * MAX_RODADAS  # 9
INTERVALO_RODADA_H    = 2  # horas de espera entre rodadas

# Delay em horas antes de enviar o plano.
# Produção usa 48h via env var DELAY_HORAS. Fallback de 5 min (0.083) só é ativado se a env var estiver ausente.
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
        # Cobre tanto o filtro por agendado_para quanto por proxima_tentativa (retentativas)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fila_pendente
            ON planos_agendados (agendado_para, proxima_tentativa)
            WHERE processado = FALSE
        """)
        # Tabela de tokens para links de treino personalizados
        cur.execute("""
            CREATE TABLE IF NOT EXISTS treino_tokens (
                token      VARCHAR(24)  PRIMARY KEY,
                pdf_path   TEXT         NOT NULL,
                label      TEXT         NOT NULL,
                email      TEXT,
                expires_at TIMESTAMP    NOT NULL,
                acessos    INTEGER      DEFAULT 0,
                criado_em  TIMESTAMP    DEFAULT NOW()
            )
        """)
        # Tabela de imagens de exames enviadas pelo formulário
        cur.execute("""
            CREATE TABLE IF NOT EXISTS exames_imagens (
                id              SERIAL PRIMARY KEY,
                plano_id        INTEGER REFERENCES planos_agendados(id) ON DELETE CASCADE,
                campo           VARCHAR(60)  NOT NULL,
                imagem_bytes    BYTEA,
                mime_type       VARCHAR(50)  DEFAULT 'image/jpeg',
                nome_extraido   TEXT,
                valor_extraido  TEXT,
                unidade         TEXT,
                alerta_nome     BOOLEAN      DEFAULT FALSE,
                processado      BOOLEAN      DEFAULT FALSE,
                criado_em       TIMESTAMP    DEFAULT NOW()
            )
        """)
        # Permite NULL em imagem_bytes (bytes limpos após Vision processar, para economizar espaço)
        cur.execute("""
            ALTER TABLE exames_imagens
            ALTER COLUMN imagem_bytes DROP NOT NULL
        """)
        cur.execute("""
            ALTER TABLE planos_agendados ADD COLUMN IF NOT EXISTS pdf_base64 TEXT
        """)
        cur.execute("""
            ALTER TABLE planos_agendados ADD COLUMN IF NOT EXISTS aguardando_aprovacao BOOLEAN DEFAULT FALSE
        """)
        cur.execute("""
            ALTER TABLE planos_agendados ADD COLUMN IF NOT EXISTS motivo_aprovacao TEXT
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
        dados['_plano_id'] = job_id  # injeta ID para processar imagens de exame
        conn2 = None
        try:
            with app.app_context():
                _gerar_plano_interno(dados)

            conn2 = get_db()
            cur2  = conn2.cursor()
            cur2.execute("""
                UPDATE planos_agendados
                SET processado = TRUE, processado_em = NOW(), erro = NULL
                WHERE id = %s
            """, (job_id,))
            conn2.commit()
            cur2.close()
            log.info(f"[FILA] Job {job_id} concluido com sucesso")

        except DadosInvalidosError as e:
            # Dados inválidos: fica na fila de aprovação para a equipe corrigir no painel
            problemas = str(e).replace('DADOS_INVALIDOS: ', '').split(' | ')
            log.warning(f"[FILA] Job {job_id} aguardando correção de dados — {problemas}")

            conn3 = None
            try:
                conn3 = get_db()
                cur3  = conn3.cursor()
                cur3.execute("""
                    UPDATE planos_agendados
                    SET processado           = TRUE,
                        processado_em        = NOW(),
                        aguardando_aprovacao = TRUE,
                        motivo_aprovacao     = %s,
                        erro                 = NULL
                    WHERE id = %s
                """, (str(e)[:500], job_id))
                conn3.commit()
                cur3.close()
            except Exception:
                pass
            finally:
                if conn3:
                    conn3.close()

            _enviar_alerta_dados_invalidos(job_id, dados, problemas)
            continue  # próximo job, sem incrementar tentativas

        except AguardandoAprovacaoError as e:
            log.info(f"[FILA] Plano #{job_id} aguardando aprovação da equipe")
            conn2 = get_db()
            try:
                cur2 = conn2.cursor()
                cur2.execute("""
                    UPDATE planos_agendados
                    SET processado            = TRUE,
                        processado_em         = NOW(),
                        aguardando_aprovacao  = TRUE,
                        motivo_aprovacao      = %s,
                        erro                  = NULL
                    WHERE id = %s
                """, (str(e), job_id))
                conn2.commit()
            finally:
                conn2.close()
            _enviar_alerta_aprovacao(job_id, dados, str(e))
            continue

        except Exception as e:
            err_str = str(e).lower()
            sem_credito = 'credit' in err_str or 'billing' in err_str or 'quota' in err_str

            if sem_credito:
                # Erro de crédito: NÃO conta tentativa, pausa 2h e tenta de novo automaticamente
                proxima = datetime.now(timezone.utc) + timedelta(hours=2)
                log.warning(f"[FILA] Job {job_id} pausado por falta de crédito Anthropic "
                            f"— proxima tentativa em 2h (tentativas nao consumidas: {tentativas}/{MAX_TENTATIVAS_TOTAL})")
                conn3 = None
                try:
                    conn3 = get_db()
                    cur3  = conn3.cursor()
                    cur3.execute("""
                        UPDATE planos_agendados
                        SET proxima_tentativa = %s,
                            erro              = %s
                        WHERE id = %s
                    """, (proxima, 'Aguardando credito Anthropic — tentativa automatica em 2h', job_id))
                    conn3.commit()
                    cur3.close()
                except Exception:
                    pass
                finally:
                    if conn3:
                        conn3.close()
                continue  # pula o resto do loop — nao desiste, nao incrementa tentativas

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


def _enviar_alerta_dados_invalidos(job_id, dados, problemas):
    """Notifica responsáveis quando o plano foi BLOQUEADO por dados inválidos."""
    nome     = dados.get('nome', '?')
    email    = dados.get('email', '?')
    whatsapp = dados.get('whatsapp', dados.get('telefone', 'não informado'))
    semanas  = dados.get('semanas_gestacao', '?')
    peso     = dados.get('peso_atual', '?')
    altura   = dados.get('altura', '?')

    lista_problemas = '\n'.join(f'  • {p}' for p in problemas)

    corpo = f"""⚠️ PLANO AGUARDANDO CORREÇÃO — Dados inválidos no formulário

O plano de {nome} ficou retido pois os dados parecem incorretos.
Acesse o painel, corrija os dados e clique em Reprocessar — sem precisar pedir para a paciente preencher o formulário de novo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTATO DA PACIENTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Nome:      {nome}
Email:     {email}
WhatsApp:  {whatsapp}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROBLEMA(S) DETECTADO(S)
━━━━━━━━━━━━━━━━━━━━━━━━━━━
{lista_problemas}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
DADOS COMO VIERAM NO FORMULÁRIO
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Semanas de gestação: {semanas}
Peso atual:          {peso}
Altura:              {altura}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRÓXIMOS PASSOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Acesse os detalhes do plano no painel
2. Clique em "Editar dados clínicos" e corrija o campo incorreto
3. Clique em "Reprocessar plano" — o plano será gerado e enviado automaticamente

Detalhes do plano: https://painel.programagestarbem.com.br/painel/detalhes/{job_id}?token={urllib.parse.quote(os.environ.get('PAINEL_TOKEN', ''), safe='')}
"""

    dest = [e.strip() for e in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if e.strip()]
    _enviar_email_sg(dest, f"🚫 PLANO BLOQUEADO — Dados inválidos — {nome}", corpo)


def _enviar_email_aguarde_paciente(dados):
    """Avisa a paciente que os dados precisam ser confirmados e a equipe entrará em contato."""
    email_paciente = dados.get('email', '').strip()
    if not email_paciente or '@' not in email_paciente:
        return
    nome     = dados.get('nome', 'Paciente')
    nome_html = _html.escape(nome)
    corpo_txt = (
        f"Olá, {nome}!\n\n"
        "Recebemos seu formulário do Gestar Bem com sucesso.\n\n"
        "Durante a análise das suas informações, identificamos que alguns dados precisam ser "
        "confirmados para que possamos montar seu plano com total segurança e precisão.\n\n"
        "Alguém da nossa equipe entrará em contato em breve pelo WhatsApp ou email "
        "para confirmar os dados e garantir que seu plano seja personalizado corretamente para você.\n\n"
        "Com carinho,\nEquipe Gestar Bem\nDra. Jessica D'Agostini e equipe"
    )
    corpo_html = f"""
<div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#333;">
  <h2 style="color:#7B1FA2;">Olá, {nome_html}!</h2>
  <p>Recebemos seu formulário do <strong>Gestar Bem</strong> com sucesso.</p>
  <p>Durante a análise das suas informações, identificamos que alguns dados precisam ser
  confirmados para que possamos montar seu plano com total segurança e precisão.</p>
  <p><strong>Alguém da nossa equipe entrará em contato em breve pelo WhatsApp ou email
  para confirmar os dados e garantir que seu plano seja personalizado corretamente para você.</strong></p>
  <p style="margin-top:24px;">Com carinho,<br>
  <strong>Equipe Gestar Bem</strong><br>
  <em>Dra. Jessica D'Agostini e equipe</em></p>
</div>"""
    _enviar_email_sg(email_paciente,
                     "Seu formulário foi recebido — confirmaremos seus dados em breve",
                     corpo_txt, corpo_html=corpo_html, nome_remetente='Gestar Bem')


def _enviar_alerta_dados_suspeitos(dados, problemas):
    """Avisa a equipe quando peso ou altura parecem incorretos (plano gerado com auto-correção)."""
    nome  = dados.get('nome', '?')
    email = dados.get('email', '?')
    peso  = dados.get('peso_atual', '?')
    alt   = dados.get('altura', '?')
    corpo = f"""⚠️ ATENÇÃO — Dados suspeitos no formulário da paciente

Paciente: {nome}
Email: {email}
Peso informado: {peso}
Altura informada: {alt}

Problema(s) detectado(s):
{chr(10).join(f'• {p}' for p in problemas)}

O plano foi gerado normalmente com os dados corrigidos automaticamente.
Por favor, confirme com a paciente se os dados estão corretos e reprocesse se necessário.

Painel: https://painel.programagestarbem.com.br/painel"""
    dest = [e.strip() for e in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if e.strip()]
    _enviar_email_sg(dest, f"⚠️ Dados suspeitos — {nome} (confirmar com paciente)", corpo)


def _requer_aprovacao(dados, calculos):
    """
    Retorna (True, motivo) se o plano deve aguardar aprovação da equipe antes do envio.
    Caso de uso principal: DG detectada pelos exames mas não declarada pela paciente
    (ela pode não saber do diagnóstico — a equipe precisa avisá-la antes do plano chegar).
    """
    if not calculos or not calculos.get('tem_dg'):
        return False, None
    quadros = str(dados.get('quadros_clinicos', '')).lower()
    dg_declarada = 'diabetes gestacional' in quadros or bool(re.search(r'\bdg\b', quadros))
    if not dg_declarada:
        glicose = dados.get('exame_glicose', '?')
        return True, (
            f"Glicose {glicose} mg/dL detectada — paciente possivelmente não sabe que tem DG. "
            f"Entre em contato com ela para informar o diagnóstico antes de enviar o plano."
        )
    return False, None


def _enviar_alerta_falha(job_id, dados, tentativas, erro):
    """Envia alerta para os responsáveis quando um job falha definitivamente."""
    nome  = dados.get('nome', '?')
    email = dados.get('email', '?')
    corpo = f"""⚠️ ALERTA — Plano não entregue após {tentativas} tentativas

Paciente: {nome}
Email: {email}
Job ID: {job_id}
Tentativas: {tentativas}
Ultimo erro: {erro[:300]}

Acesse o painel para verificar os logs e reprocessar manualmente se necessário.
Painel: https://painel.programagestarbem.com.br/painel?token={urllib.parse.quote(os.environ.get('PAINEL_TOKEN', ''), safe='')}
Detalhes: https://painel.programagestarbem.com.br/painel/detalhes/{job_id}?token={urllib.parse.quote(os.environ.get('PAINEL_TOKEN', ''), safe='')}"""
    dest = [e.strip() for e in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if e.strip()]
    _enviar_email_sg(dest, f"⚠️ FALHA: Plano de {nome} não entregue", corpo)


def _enviar_alerta_aprovacao(job_id, dados, motivo):
    """Notifica equipe que um plano foi gerado mas precisa de aprovação antes do envio."""
    nome  = dados.get('nome', '?')
    email = dados.get('email', '?')
    token_enc = urllib.parse.quote(os.environ.get('PAINEL_TOKEN', ''), safe='')
    corpo = f"""🔵 APROVAÇÃO NECESSÁRIA — Plano gerado mas não enviado

Paciente: {nome}
Email: {email}

MOTIVO:
{motivo}

O plano foi gerado com sucesso e está aguardando sua aprovação.
Acesse o painel, revise o plano e clique em "Aprovar e enviar" quando estiver pronto.

Detalhes: https://painel.programagestarbem.com.br/painel/detalhes/{job_id}?token={token_enc}"""
    dest = [e.strip() for e in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if e.strip()]
    _enviar_email_sg(dest, f"🔵 Aprovação necessária — plano de {nome}", corpo)


def limpar_banco():
    """Remove registros processados com mais de 270 dias para nao acumular lixo no banco."""
    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        # Zerar bytes de imagens processadas há mais de 30 dias (libera espaço no Neon)
        # Mantém nome_extraido/valor_extraido/unidade para histórico e reprocessamento
        cur.execute("""
            UPDATE exames_imagens
            SET imagem_bytes = NULL
            WHERE processado = TRUE
            AND criado_em < NOW() - INTERVAL '30 days'
            AND imagem_bytes IS NOT NULL
        """)
        imgs_zeradas = cur.rowcount
        if imgs_zeradas > 0:
            log.info(f"[LIMPEZA] {imgs_zeradas} imagem(ns) de exame com bytes zerados (>30 dias)")

        # Limpar PDF base64 de planos enviados há mais de 7 dias (libera espaço no Neon)
        cur.execute("""
            UPDATE planos_agendados
            SET pdf_base64 = NULL
            WHERE pdf_base64 IS NOT NULL
            AND (aguardando_aprovacao IS NULL OR aguardando_aprovacao = FALSE)
            AND criado_em < NOW() - INTERVAL '7 days'
        """)
        log.info("[LIMPAR] PDFs antigos removidos do banco")

        # Apagar planos processados com mais de 270 dias
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
            alertas.append(f"⚠️ {com_falha} plano(s) com falha — verificar logs no Fly.io ou no painel")
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
        linhas.append(f"SendGrid: {emails_sg}/100 emails hoje")
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
            alertas.append("🔴 URGENTE: ANTHROPIC_API_KEY invalida — verificar secrets no Fly.io")
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
https://gestar-bem-api.fly.dev/health"""

    dest = [e.strip() for e in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if e.strip()]
    _enviar_email_sg(dest, f"{emoji_geral} Check diário Gestar Bem — {data_hora}", corpo)


def check_anthropic():
    """Roda a cada 2h — verifica se a API Anthropic tem credito. Alerta imediato se nao tiver."""
    try:
        _anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}]
        )
        log.info("[ANTHROPIC] API OK")
    except Exception as e:
        err_str = str(e).lower()
        if 'credit' in err_str or 'billing' in err_str or 'quota' in err_str:
            log.error("[ANTHROPIC] CREDITO ESGOTADO — enviando alerta")
            dest = [d.strip() for d in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if d.strip()]
            _enviar_email_sg(dest,
                "🔴 URGENTE: Planos parados — crédito Anthropic zerado",
                "🔴 URGENTE — Crédito Anthropic esgotado\n\n"
                "Os planos do Gestar Bem PARARAM de ser gerados.\n\n"
                "AÇÃO NECESSÁRIA AGORA:\n"
                "Acesse console.anthropic.com → Plans & Billing → adicionar créditos.\n\n"
                "A chave está na conta planosgestarbem@gmail.com."
            )
        else:
            log.warning(f"[ANTHROPIC] Erro inesperado na checagem: {e}")


# Inicializar banco e agendador ao subir o servidor
init_db()
_scheduler = BackgroundScheduler(timezone='America/Sao_Paulo')
_scheduler.add_job(verificar_fila,    'interval', minutes=1,  id='verificar_fila',    max_instances=1)
_scheduler.add_job(limpar_banco,      'cron', hour=3,  minute=0,  id='limpar_banco',   max_instances=1)
_scheduler.add_job(check_diario,      'cron', hour=10, minute=7,  id='check_diario',   max_instances=1)
_scheduler.add_job(check_anthropic,   'interval', hours=2,    id='check_anthropic',   max_instances=1)
_scheduler.start()
atexit.register(lambda: _scheduler.shutdown(wait=False))


# ── Funcao de envio de email ─────────────────────────────────────────────────

def enviar_email_pdf(destinatario, nome_paciente, pdfs_lista, links_treino=None, treino_aguardando_liberacao=False):
    """Envia PDF de nutricao (anexo) + links de treino (corpo) via SendGrid."""
    sg_key    = os.environ.get('SENDGRID_API_KEY', '')
    remetente = 'planos@programagestarbem.com.br'

    if not sg_key:
        raise ValueError("SENDGRID_API_KEY nao configurado no ambiente")

    if not pdfs_lista:
        raise ValueError("Nenhum PDF gerado — email nao sera enviado sem anexo")

    # Montar blocos de treino (texto e HTML)
    bloco_treino_txt = ""
    bloco_treino_html = ""
    aviso = "⚠️ IMPORTANTE: salve os PDFs no seu celular agora! Os links ficam disponíveis por 90 dias — depois disso expiram. Se perder o email, entre em contato com nossa equipe para reenvio."
    if links_treino:
        if treino_aguardando_liberacao:
            titulo_txt  = "🏋️ PLANOS DE TREINO — DISPONÍVEIS PARA QUANDO VOCÊ FOR LIBERADA"
            intro_txt   = "Sabemos que no momento você está aguardando liberação médica para praticar exercícios. Deixamos os seus planos de treino aqui para que fiquem à sua disposição assim que você receber o sinal verde do seu médico! 💪"
            titulo_html = "🏋️ Planos de Treino — Disponíveis para quando você for liberada"
            intro_html  = "Sabemos que no momento você está aguardando liberação médica. Deixamos os planos aqui para quando você receber o sinal verde do seu médico! 💪"
        else:
            titulo_txt  = "📋 SEUS PLANOS DE TREINO"
            intro_txt   = ""
            titulo_html = "📋 Seus Planos de Treino"
            intro_html  = ""

        bloco_treino_txt = f"\n\n{'=' * 40}\n{titulo_txt}\n\n"
        if intro_txt:
            bloco_treino_txt += intro_txt + "\n\n"
        for url, label in links_treino:
            bloco_treino_txt += f"▶ {label}:\n{url}\n\n"
        bloco_treino_txt += aviso

        links_html = ""
        for url, label in links_treino:
            links_html += (
                f'<p style="margin:10px 0;">'
                f'<a href="{url}" style="display:inline-block;background:#9B27AF;color:#ffffff;'
                f'text-decoration:none;padding:10px 20px;border-radius:8px;font-size:14px;font-weight:600;">'
                f'▶ {label}</a></p>\n'
            )
        bloco_treino_html = f"""
<br><hr style="border:none;border-top:1px solid #e0d0f0;margin:24px 0;">
<h3 style="color:#9B27AF;margin-bottom:8px;">{titulo_html}</h3>
{'<p style="color:#555;font-size:14px;">' + intro_html + '</p>' if intro_html else ''}
{links_html}
<p style="font-size:12px;color:#888;margin-top:16px;">{aviso}</p>"""

    corpo_txt = f"""Olá, {nome_paciente}! 💜

Seu plano personalizado do programa Gestar Bem está pronto!

Em anexo você encontra o seu Plano de Nutrição completo, preparado especialmente para você com muito carinho e cuidado.{bloco_treino_txt}

=========================================================
Leia com atenção e siga as orientações. Qualquer dúvida, fale com nossa equipe.

Com carinho,
Equipe Gestar Bem 🌸"""

    nome_paciente_html = _html.escape(nome_paciente)
    corpo_html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;background:#fdf6ff;margin:0;padding:0;">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(155,39,175,.1);">
  <div style="background:#9B27AF;padding:24px 28px;text-align:center;">
    <h1 style="color:#fff;font-size:22px;margin:0;">Gestar Bem 🌸</h1>
  </div>
  <div style="padding:28px;">
    <p style="font-size:16px;color:#333;">Olá, <strong>{nome_paciente_html}</strong>! 💜</p>
    <p style="color:#555;font-size:14px;line-height:1.6;">Seu plano personalizado do programa Gestar Bem está pronto!</p>
    <p style="color:#555;font-size:14px;line-height:1.6;">Em anexo você encontra o seu <strong>Plano de Nutrição completo</strong>, preparado especialmente para você com muito carinho e cuidado.{bloco_treino_html}</p>
    <hr style="border:none;border-top:1px solid #e0d0f0;margin:24px 0;">
    <p style="color:#555;font-size:13px;">Leia com atenção e siga as orientações. Qualquer dúvida, fale com nossa equipe.</p>
    <p style="color:#9B27AF;font-size:14px;font-weight:600;">Com carinho, Equipe Gestar Bem 🌸</p>
  </div>
</div>
</body></html>"""

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
        "reply_to": {"email": "planosgestarbem@gmail.com", "name": "Gestar Bem"},
        "subject": "Seu Plano Personalizado — Gestar Bem",
        "content": [
            {"type": "text/plain", "value": corpo_txt},
            {"type": "text/html",  "value": corpo_html}
        ],
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

TREINOS_DOMAIN = "treinos.programagestarbem.com.br"

MAX_IMG_B64 = 15 * 1024 * 1024  # 15 MB em base64 (~11 MB decoded)


def criar_token_treino(pdf_path, label, email='', dias=90, conn=None):
    """
    Cria token único para acesso a um PDF de treino. Retorna a URL completa.
    Se `conn` for fornecida, usa-a sem abrir/fechar (útil para batch de múltiplos PDFs).
    """
    token = secrets.token_urlsafe(14)  # ~19 chars URL-safe
    expires = datetime.now(timezone.utc) + timedelta(days=dias)
    _owns_conn = conn is None
    if _owns_conn:
        conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO treino_tokens (token, pdf_path, label, email, expires_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (token, pdf_path, label, email, expires))
        if _owns_conn:
            conn.commit()
    finally:
        if _owns_conn:
            conn.close()
    return f"https://{TREINOS_DOMAIN}/t/{token}"


@app.route('/t/<token>')
def servir_treino_por_token(token):
    """Serve PDF de treino via token único."""
    if not re.match(r'^[A-Za-z0-9_\-]{10,30}$', token):
        abort(404)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE treino_tokens
            SET acessos = acessos + 1
            WHERE token = %s AND expires_at > NOW()
            RETURNING pdf_path, label
        """, (token,))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        abort(404)
    pdf_path = row[0]
    full_path = os.path.normpath(os.path.join(PDF_BASE, pdf_path.replace('/', os.sep)))
    # Impede path traversal: garante que o arquivo esta dentro de PDF_BASE
    if not full_path.startswith(os.path.abspath(PDF_BASE) + os.sep):
        abort(403)
    directory = os.path.dirname(full_path)
    filename  = os.path.basename(full_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(directory, filename)


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




def selecionar_pdfs_exercicio(dados, trimestre):
    """
    Retorna sempre os dois PDFs de treino (academia + casa) + flag nao_liberada.
    Se houver limitacao fisica, o PDF de academia e substituido pelo adaptado.
    Retorna ([(pdf_rel_path, label), ...], nao_liberada).
    pdf_rel_path é relativo a PDF_BASE (ex: "academia/academia_I_iniciante.pdf").
    """
    liberado = str(dados.get('liberado_exercicio', '')).lower()
    nao_liberada = 'nao' in liberado or 'não' in liberado or not liberado.strip()

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

    pdfs = []

    # ── PDF 1: Academia (ou adaptado se houver limitacao) ──
    if tem_limit:
        full_path = selecionar_pdf_limitacao(limit, nivel, trimestre)
        rel   = os.path.relpath(full_path, PDF_BASE).replace('\\', '/')
        label = "Plano de Treinos — Academia (adaptado para sua limitação)"
    else:
        rel   = f"academia/academia_{trimestre}_{nivel}.pdf"
        label = "Plano de Treinos — Academia"

    if os.path.exists(os.path.join(PDF_BASE, rel.replace('/', os.sep))):
        pdfs.append((rel, label))
    else:
        log.warning(f"PDF academia nao encontrado: {rel}")

    # ── PDF 2: Casa — sempre enviado ──
    rel_casa = f"casa/casa_{trimestre}.pdf"
    if os.path.exists(os.path.join(PDF_BASE, rel_casa)):
        pdfs.append((rel_casa, "Plano de Treinos — Casa"))
    else:
        log.warning(f"PDF casa nao encontrado: {rel_casa}")

    log.info(f"PDFs de treino selecionados: {[l for _, l in pdfs]}")
    return pdfs, nao_liberada


def selecionar_links_exercicio(dados, trimestre, email=''):
    """
    Wrapper que chama selecionar_pdfs_exercicio e gera tokens únicos para cada PDF.
    Usa uma única conexão para todos os PDFs (evita N conexões para N PDFs).
    Retorna ([(url_tokenizada, label), ...], nao_liberada).
    """
    pdfs, nao_liberada = selecionar_pdfs_exercicio(dados, trimestre)
    links = []
    if not pdfs:
        return links, nao_liberada
    conn = get_db()
    try:
        for rel_path, label in pdfs:
            url = criar_token_treino(rel_path, label, email=email, conn=conn)
            links.append((url, label))
        conn.commit()
    finally:
        conn.close()
    return links, nao_liberada


# ── Calculos clinicos (TMB, macros, hidratacao) ──────────────────────────────

def _extrair_numero(valor, inteiro=False):
    """Extrai o primeiro numero de uma string. Lança ValueError se não encontrar."""
    match = re.search(r'\d+(?:[,\.]\d+)?', str(valor or ''))
    if not match:
        raise ValueError(f"Nao foi possivel extrair numero de: {repr(valor)}")
    numero = float(match.group().replace(',', '.'))
    return int(numero) if inteiro else numero


def _validar_dados_criticos(dados):
    """
    Valida campos críticos do formulário ANTES de qualquer processamento caro.
    Levanta DadosInvalidosError se encontrar dados que impedem gerar o plano com segurança.
    Deve ser chamada no início de _gerar_plano_interno, antes de Vision e Claude.
    """
    erros = []

    # Email — sem email não tem como entregar
    email = str(dados.get('email', '')).strip()
    if not email or '@' not in email:
        erros.append("Email ausente ou inválido. Sem email não é possível entregar o plano.")

    # Semanas de gestação
    semanas_raw = str(dados.get('semanas_gestacao', '')).strip().replace(',', '.')
    if not semanas_raw:
        erros.append("Campo 'semanas de gestação' está vazio. Perguntar: 'Com quantas semanas você está grávida?'")
    else:
        try:
            sv = float(semanas_raw.split()[0])
            if 1.30 <= sv <= 2.20:
                erros.append(
                    f"Campo 'semanas de gestação' recebeu '{semanas_raw}', que parece ser a ALTURA "
                    f"em metros. Perguntar: 'Com quantas semanas você está grávida?'"
                )
            elif sv > 42:
                erros.append(
                    f"Semanas informadas ({semanas_raw}) estão acima de 42 — valor impossível. "
                    f"Pode ser a altura em cm. Perguntar: 'Com quantas semanas está?'"
                )
            elif sv <= 0:
                erros.append(
                    f"Semanas informadas ({semanas_raw}) são zero ou negativas. "
                    f"Perguntar: 'Com quantas semanas de gestação você está?'"
                )
        except Exception:
            erros.append(
                f"Campo 'semanas de gestação' com valor não numérico ('{semanas_raw}'). "
                f"Perguntar: 'Com quantas semanas você está grávida?'"
            )

    # Peso — acima de 130kg pode ser altura em cm digitada no campo errado
    try:
        peso_raw = _extrair_numero(dados.get('peso_atual', ''))
        if peso_raw > 130:
            erros.append(
                f"Peso informado ({peso_raw:.1f}kg) está acima de 130kg. "
                f"Pode ser a altura em cm digitada no campo errado. "
                f"Perguntar: 'Qual é o seu peso atual em quilogramas?'"
            )
    except Exception:
        pass  # peso inválido será detectado por calcular_dados_clinicos

    # Altura — valor impossível para uma adulta (< 100 cm após normalização)
    try:
        alt_raw = str(dados.get('altura', '')).strip().replace(',', '.')
        alt_val = float(alt_raw.split()[0]) if alt_raw else None
        if alt_val is not None:
            # Converte metros para cm se necessário (ex: 1.65 → 165)
            if 1.0 <= alt_val <= 2.5:
                alt_val = alt_val * 100
            if alt_val < 100:
                erros.append(
                    f"Altura informada ({dados.get('altura')}) parece incorreta — valor impossível para uma adulta. "
                    f"Perguntar: 'Qual é a sua altura em centímetros? (ex: 165)'"
                )
    except Exception:
        pass

    # Obesidade/sobrepeso sem glicose → não é possível determinar DG com segurança
    quadros_raw = str(dados.get('quadros_clinicos', '')).lower()
    tem_obesidade = any(p in quadros_raw for p in ('obesidade', 'sobrepeso'))
    glicose_raw = str(dados.get('exame_glicose', '')).strip()
    if tem_obesidade and not glicose_raw:
        erros.append(
            "Paciente com obesidade/sobrepeso e sem valor de glicose em jejum informado. "
            "Não é possível determinar com segurança se há Diabetes Gestacional. "
            "Perguntar: 'Você tem o resultado do seu exame de glicose em jejum?'"
        )

    # Idade — valores fora do razoável para uma gestante
    try:
        idade_raw = _extrair_numero(dados.get('idade', ''))
        if idade_raw < 12 or idade_raw > 55:
            erros.append(
                f"Idade informada ({int(idade_raw)} anos) está fora do esperado para uma gestante. "
                f"Perguntar: 'Qual é a sua idade?'"
            )
    except Exception:
        pass  # idade ausente/inválida será detectada por calcular_dados_clinicos

    if erros:
        raise DadosInvalidosError('DADOS_INVALIDOS: ' + ' | '.join(erros))


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

        # Corrigir erros de digitação comuns (ex: 985 no lugar de 98,5)
        if peso > 200:
            peso_original = peso
            fator = 10 ** (len(str(int(peso))) - 2)  # ex: 985 → fator=10 → 98.5
            peso = peso / fator
            log.warning(f"Peso corrigido automaticamente: {peso_original} -> {peso:.1f}kg (provavel erro de digitacao)")
        if alt > 220:
            alt_original = alt
            fator = 10 ** (len(str(int(alt))) - 3)  # ex: 1650 → fator=10 → 165.0
            alt = alt / fator
            log.warning(f"Altura corrigida automaticamente: {alt_original} -> {alt:.1f}cm (provavel erro de digitacao)")

        # ── Alertas suspeitos (avisa mas gera o plano normalmente) ────────────
        # Validações críticas já foram feitas em _validar_dados_criticos antes de chegar aqui.
        # Só chegam aqui casos que podem ser corrigidos automaticamente com alta confiança.
        alertas_dados = []

        # Inversão peso/altura (ex: peso=170, altura=60): matematicamente óbvio, corrige sozinho
        if (140 <= peso <= 220) and (alt < 100):
            log.warning(f"Possivel inversao peso/altura: peso={peso}, alt={alt} — corrigindo")
            peso, alt = alt, peso
            alertas_dados.append(
                f"Peso e altura parecem invertidos. "
                f"Peso original: {dados.get('peso_atual')} | Altura original: {dados.get('altura')}. "
                f"Corrigido automaticamente para peso={peso:.1f}kg / altura={alt:.1f}cm."
            )

        if alertas_dados:
            _enviar_alerta_dados_suspeitos(dados, alertas_dados)

        # Validar ranges razoaveis após correção
        if not (30 <= peso <= 200):
            log.warning(f"Peso fora do range esperado mesmo apos correcao: {peso}kg")
        if not (140 <= alt <= 220):
            log.warning(f"Altura fora do range esperado mesmo apos correcao: {alt}cm")
        if not (1 <= semanas <= 42):
            log.warning(f"Semanas fora do range esperado: {semanas}")

        # Trimestre
        if semanas <= 13:
            trimestre = "I"
            tri_nome  = "Primeiro Trimestre (semanas 1–13)"
        elif semanas <= 27:
            trimestre = "II"
            tri_nome  = "Segundo Trimestre (semanas 13–27)"
        else:
            trimestre = "III"
            tri_nome  = "Terceiro Trimestre (semanas 27–41)"

        # TMB — Mifflin-St Jeor para mulheres
        tmb = (10 * peso) + (6.25 * alt) - (5 * idade) - 161

        # Fator de atividade (protocolo oficial Dra. Jessica)
        # Sedentaria=1.2 | Iniciante=1.37 | Intermediario=1.55 | Avancado=1.7
        if 'sedent' in nivel:
            fator = 1.2;  fator_nome = "Sedentaria (x1,2)"
        elif 'leve' in nivel or 'iniciante' in nivel:
            fator = 1.37; fator_nome = "Levemente ativa / Iniciante (x1,37)"
        elif 'moder' in nivel or 'intermedi' in nivel:
            fator = 1.55; fator_nome = "Moderadamente ativa / Intermediaria (x1,55)"
        elif 'avan' in nivel or 'intens' in nivel:
            fator = 1.7;  fator_nome = "Muito ativa / Avancada (x1,7)"
        else:
            log.warning(f"nivel_exercicio nao reconhecido: '{nivel}' — usando fallback 1.37")
            fator = 1.37; fator_nome = "Levemente ativa / Iniciante (x1,37)"

        manutencao = tmb * fator

        # IMC e categoria de peso
        altura_m      = alt / 100
        imc           = peso / (altura_m ** 2)
        peso_ideal    = 22 * (altura_m ** 2)  # IMC 22 = centro da faixa ideal

        if imc < 18.5:
            categoria_peso = "ABAIXO_DO_PESO"
        elif imc <= 24.9:
            categoria_peso = "IDEAL"
        else:
            categoria_peso = "SOBREPESO_OBESIDADE"

        # Estrategia calorica por categoria de IMC e trimestre
        # Regras oficiais Dra. Jessica D'Agostini (protocolo Gestar Bem)
        if categoria_peso == "ABAIXO_DO_PESO":
            # Abaixo do peso: +150 kcal em todos os trimestres
            calorias_alvo = manutencao + 150
            estrategia = (
                f"ABAIXO DO PESO (IMC {imc:.1f}) — acrescimo de 150 kcal para ganho gradual e seguro."
            )
            if trimestre == "I":
                meta_peso = "manter o peso atual neste trimestre"
            elif trimestre == "II":
                meta_peso = "ganho de 3 a 3,5 kg neste trimestre"
            else:
                meta_peso = "ganho de 3,5 a 4 kg neste trimestre"

        elif categoria_peso == "SOBREPESO_OBESIDADE":
            # Sobrepeso/obesidade: deficit por trimestre (minimos de segurança)
            if trimestre == "I":
                calorias_alvo = max(manutencao - 450, 1500)  # deficit 300-600 kcal, min 1500
                estrategia = (
                    f"SOBREPESO/OBESIDADE (IMC {imc:.1f}) — plano calorico controlado para o 1o trimestre (deficit seguro)."
                )
                meta_peso = "perder ate 5 kg neste trimestre de forma gradual e segura"
            elif trimestre == "II":
                calorias_alvo = max(manutencao - 275, 1600)  # deficit 200-350 kcal, min 1600
                estrategia = (
                    f"SOBREPESO/OBESIDADE (IMC {imc:.1f}) — plano calorico controlado para o 2o trimestre."
                )
                meta_peso = "ganho de 1,5 a 2 kg neste trimestre de forma gradual e segura"
            else:
                calorias_alvo = max(manutencao - 275, 1500)  # deficit 200-350 kcal, min 1500
                estrategia = (
                    f"SOBREPESO/OBESIDADE (IMC {imc:.1f}) — plano calorico controlado para o 3o trimestre."
                )
                meta_peso = "ganho de 2 a 2,5 kg neste trimestre de forma gradual e segura"

        else:  # IDEAL
            # Peso ideal: leve deficit no I tri, acrescimo no II e III
            if trimestre == "I":
                calorias_alvo = manutencao - 175  # deficit leve 150-200 kcal
                estrategia = (
                    f"PESO IDEAL (IMC {imc:.1f}) — leve ajuste calorico para o 1o trimestre."
                )
                meta_peso = "manter o peso atual neste trimestre"
            elif trimestre == "II":
                calorias_alvo = manutencao + 175  # acrescimo 150-200 kcal
                estrategia = (
                    f"PESO IDEAL (IMC {imc:.1f}) — acrescimo calorico para o 2o trimestre."
                )
                meta_peso = "ganho de 3 a 3,5 kg neste trimestre"
            else:
                calorias_alvo = manutencao + 175  # acrescimo 150-200 kcal
                estrategia = (
                    f"PESO IDEAL (IMC {imc:.1f}) — acrescimo calorico para o 3o trimestre."
                )
                meta_peso = "ganho de 3,5 a 4 kg neste trimestre"

        # Detectar DG ou Percentil Baixo para macros especiais (40% prot / 35% carb / 25% gord)
        quadros    = str(dados.get('quadros_clinicos', '')).lower()
        observ_dg  = str(dados.get('observacoes', '')).lower() + str(dados.get('preferencia', '')).lower()
        glicose_rw = dados.get('exame_glicose', '')
        hba1c_rw   = dados.get('exame_hemoglobina_glicada', '')
        totg_rw    = dados.get('exame_totg', '')

        tem_dg = ('diabetes gestacional' in quadros or 'diabetes gestacional' in observ_dg
                  or bool(re.search(r'\bdg\b', quadros)))

        tem_percentil_b = ('percentil' in quadros or 'restricao de crescimento' in quadros
                           or 'crescimento fetal' in quadros)

        # Detectar DG pelos valores dos exames (paciente pode nao saber que tem)
        if not tem_dg:
            for valor_rw, limiar, descricao in [
                (glicose_rw, 92, 'glicose em jejum'),
                (hba1c_rw,  6.5, 'hemoglobina glicada'),
                (totg_rw,   140, 'TOTG'),
            ]:
                if valor_rw:
                    try:
                        if _extrair_numero(valor_rw) >= limiar:
                            tem_dg = True
                            log.info(f"[DG] Detectado via {descricao}={valor_rw} para {dados.get('nome','?')}")
                            break
                    except Exception:
                        pass

        if tem_dg or tem_percentil_b:
            prot_pct  = 0.40; carb_pct = 0.35; gord_pct = 0.25
            macro_label = "DG/Percentil baixo: 40% prot / 35% carb / 25% gord"
        else:
            prot_pct  = 0.35; carb_pct = 0.40; gord_pct = 0.25
            macro_label = "Padrao: 35% prot / 40% carb / 25% gord"

        prot_g = (calorias_alvo * prot_pct) / 4
        carb_g = (calorias_alvo * carb_pct) / 4
        gord_g = (calorias_alvo * gord_pct) / 9

        # Hidratacao: I tri = peso x 35ml | II e III tri = peso x 40ml
        agua_ml = peso * 35 if trimestre == "I" else peso * 40
        agua_l  = agua_ml / 1000

        return {
            "tmb":            round(tmb),
            "fator_nome":     fator_nome,
            "manutencao":     round(manutencao),
            "calorias_alvo":  round(calorias_alvo),
            "estrategia":     estrategia,
            "imc":            round(imc, 1),
            "categoria_peso": categoria_peso,
            "peso_ideal":     round(peso_ideal, 1),
            "prot_g":         round(prot_g),
            "carb_g":         round(carb_g),
            "gord_g":         round(gord_g),
            "macro_label":    macro_label,
            "prot_pct":       int(prot_pct * 100),
            "carb_pct":       int(carb_pct * 100),
            "gord_pct":       int(gord_pct * 100),
            "agua_l":         round(agua_l, 1),
            "meta_peso":      meta_peso,
            "trimestre":      trimestre,
            "tri_nome":       tri_nome,
            "tem_dg":         tem_dg,
            "tem_percentil_b": tem_percentil_b,
        }

    except Exception as e:
        log.warning(f"Nao foi possivel calcular dados clinicos: {e} | "
                    f"peso={dados.get('peso_atual')} alt={dados.get('altura')} "
                    f"idade={dados.get('idade')} semanas={dados.get('semanas_gestacao')}")
        return None


# ── Processamento de imagens de exames (Claude Vision) ───────────────────────

CAMPOS_EXAME_IMAGEM = {
    'img_glicose':             'exame_glicose',
    'img_hemoglobina_glicada': 'exame_hemoglobina_glicada',
    'img_vitamina_d':          'exame_vitamina_d',
    'img_vitamina_b12':        'exame_vitamina_b12',
    'img_ferritina':           'exame_ferritina',
    'img_tsh':                 'exame_tsh',
    'img_t4_livre':            'exame_t4_livre',
    'img_insulina_jejum':      'exame_insulina_jejum',
    'img_ferro_serico':        'exame_ferro_serico',
    'img_hemograma':           'exame_hemograma',
}


def _normalizar_nome(nome):
    return unicodedata.normalize('NFD', nome or '').encode('ascii', 'ignore').decode().lower().strip()


def _nomes_batem(nome_forms, nome_exame):
    """True se pelo menos uma palavra relevante do formulario aparece no nome do exame."""
    if not nome_forms or not nome_exame:
        return True
    words = {w for w in _normalizar_nome(nome_forms).split() if len(w) > 3}
    exame_norm = _normalizar_nome(nome_exame)
    return any(w in exame_norm for w in words)


def _enviar_alerta_exame_errado(plano_id, nome_paciente, whatsapp, campo, nome_no_exame):
    campo_legivel = campo.replace('img_', '').replace('_', ' ').upper()
    corpo = (
        f"ATENCAO: exame com nome diferente detectado!\n\n"
        f"Plano ID: {plano_id}\n"
        f"Paciente no formulario: {nome_paciente}\n"
        f"WhatsApp: {whatsapp or 'nao informado'}\n"
        f"Exame afetado: {campo_legivel}\n"
        f"Nome encontrado no exame: {nome_no_exame}\n\n"
        f"O plano foi gerado SEM os valores deste exame.\n"
        f"Entre em contato pelo WhatsApp e peca o exame correto.\n"
        f"Apos receber, acesse o painel e reprocesse o plano."
    )
    dest = [d.strip() for d in os.environ.get('EMAIL_ALERTA', 'enediscremim95@gmail.com').split(',') if d.strip()]
    ok = _enviar_email_sg(dest, f"EXAME DE OUTRA PESSOA — {nome_paciente} (plano #{plano_id})", corpo)
    if ok:
        log.info(f"Alerta exame errado enviado — plano {plano_id}")


def _detectar_mime_type(img_bytes: bytes) -> str:
    """
    Detecta o tipo MIME real do arquivo a partir dos magic bytes.
    Suporta PDF, JPEG, PNG e WebP. Padrão: image/jpeg.
    """
    if img_bytes[:4] == b'%PDF':
        return 'application/pdf'
    if img_bytes[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def processar_imagens_exames(plano_id, nome_paciente, whatsapp=''):
    """
    Processa imagens de exames salvas no banco com Claude Vision/PDF.
    Extrai valores, confere nome do paciente e retorna {campo: valor}.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, campo, imagem_bytes, mime_type, valor_extraido, unidade "
            "FROM exames_imagens "
            "WHERE plano_id = %s AND processado = FALSE",
            (plano_id,)
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {}

    valores = {}
    updates = []  # [(nome_exame, valor_extraido, unidade, alerta, img_id)]

    for (img_id, campo, imagem_bytes, mime_type, valor_pre, unidade_pre) in rows:
        # Bytes já limpos (>30 dias) mas valor pré-extraído disponível — reutilizar
        if not imagem_bytes:
            if valor_pre:
                campo_destino = CAMPOS_EXAME_IMAGEM.get(campo)
                if campo_destino:
                    valores[campo_destino] = f"{valor_pre} {unidade_pre or ''}".strip()
                    log.info(f"[VISION] {campo} → reutilizando valor pré-extraído: {valores[campo_destino]}")
            updates.append((None, valor_pre, unidade_pre, False, img_id))
            continue

        try:
            img_b64 = base64.b64encode(bytes(imagem_bytes)).decode('utf-8')
            label   = campo.replace('img_', '').replace('_', ' ').upper()

            # Monta o bloco de conteúdo correto conforme o tipo do arquivo
            if mime_type == 'application/pdf':
                content_block = {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": img_b64}
                }
                max_tok = 400  # PDFs podem ter vários exames numa página
            else:
                content_block = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": img_b64}
                }
                max_tok = 200

            msg = _anthropic_client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=max_tok,
                messages=[{"role": "user", "content": [
                    content_block,
                    {"type": "text",
                     "text": (
                         f"Documento de resultado de exame laboratorial.\n"
                         f"Extraia as seguintes informações:\n"
                         f"1. Nome completo do paciente\n"
                         f"2. Valor numérico do exame: {label}\n"
                         f"3. Unidade de medida desse exame\n\n"
                         f"Se for um PDF com múltiplos exames, procure especificamente por: {label}.\n"
                         f"Responda APENAS em JSON, sem texto adicional:\n"
                         f'{{\"nome\": \"...\", \"valor\": \"...\", \"unidade\": \"...\"}}\n'
                         f"Use null se não encontrar o campo."
                     )}
                ]}]
            )

            try:
                r = json.loads(msg.content[0].text.strip())
            except json.JSONDecodeError:
                log.warning(f"[VISION] Claude retornou resposta não-JSON para {campo} plano={plano_id}: {msg.content[0].text[:100]}")
                updates.append((None, None, None, False, img_id))
                continue

            nome_exame     = r.get('nome')
            valor_extraido = r.get('valor')
            unidade        = r.get('unidade')
            alerta         = False

            if nome_exame and not _nomes_batem(nome_paciente, nome_exame):
                alerta = True
                log.warning(f"[VISION] Exame errado — plano={plano_id} campo={campo} "
                             f"paciente='{nome_paciente}' exame='{nome_exame}'")
                _enviar_alerta_exame_errado(plano_id, nome_paciente, whatsapp, campo, nome_exame)

            updates.append((nome_exame, valor_extraido, unidade, alerta, img_id))

            if not alerta and valor_extraido:
                campo_destino = CAMPOS_EXAME_IMAGEM.get(campo)
                if campo_destino:
                    valores[campo_destino] = f"{valor_extraido} {unidade or ''}".strip()
                    log.info(f"[VISION] {campo} → {valores[campo_destino]}")

        except Exception as e:
            log.warning(f"[VISION] Erro ao processar {campo} plano={plano_id}: {e}")
            updates.append((None, None, None, False, img_id))

    # Batch update — uma única conexão para todos os resultados
    if updates:
        conn_upd = get_db()
        try:
            cur_upd = conn_upd.cursor()
            for (nome_exame, valor_extraido, unidade, alerta, img_id) in updates:
                cur_upd.execute(
                    "UPDATE exames_imagens SET nome_extraido=%s, valor_extraido=%s, "
                    "unidade=%s, alerta_nome=%s, processado=TRUE, imagem_bytes=NULL WHERE id=%s",
                    (nome_exame, valor_extraido, unidade, alerta, img_id)
                )
            conn_upd.commit()
            cur_upd.close()
        except Exception as e:
            log.error(f"[VISION] Erro no batch update de exames plano={plano_id}: {e}")
        finally:
            conn_upd.close()

    return valores


# ── Endpoint principal ───────────────────────────────────────────────────────


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

    # Extrair imagens de exames do payload antes de salvar (nao ficam no JSONB)
    imagens_exame = {}
    for campo_img in list(CAMPOS_EXAME_IMAGEM.keys()):
        if campo_img in dados:
            imagens_exame[campo_img] = dados.pop(campo_img)

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO planos_agendados (dados, agendado_para)
            VALUES (%s, %s) RETURNING id
        """, (PgJson(dados), agendado_para))
        plano_id = cur.fetchone()[0]

        # Salvar imagens de exames vinculadas ao plano
        for campo_img, b64_str in imagens_exame.items():
            try:
                if len(b64_str) > MAX_IMG_B64:
                    log.warning(f"[IMAGEM] {campo_img} excede limite ({len(b64_str)//1024}KB base64) — ignorando")
                    continue
                img_bytes = base64.b64decode(b64_str)
                mime_type_detectado = _detectar_mime_type(img_bytes)
                cur.execute("""
                    INSERT INTO exames_imagens (plano_id, campo, imagem_bytes, mime_type)
                    VALUES (%s, %s, %s, %s)
                """, (plano_id, campo_img, img_bytes, mime_type_detectado))
                log.info(f"Imagem {campo_img} salva para plano {plano_id} ({len(img_bytes)//1024}KB, {mime_type_detectado})")
            except Exception as e:
                log.warning(f"Erro ao salvar imagem {campo_img}: {e}")

        conn.commit()
        cur.close()
        n_imgs = len(imagens_exame)
        log.info(f"Plano de {nome} agendado (id={plano_id}, {n_imgs} imagem(s) de exame)")
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
        except DadosInvalidosError as e:
            # Dados inválidos: não adianta retentativa, encerra imediatamente
            log.error(f"[BG] Dados inválidos para {nome} ({email}) — sem retry: {e}")
            return
        except Exception:
            log.error(f"[BG] Erro na tentativa {tentativa}/{MAX_TENTATIVAS_BG} para {nome} ({email}): {traceback.format_exc()}")
            if tentativa < MAX_TENTATIVAS_BG:
                log.info(f"[BG] Aguardando 60s antes da proxima tentativa...")
                time.sleep(60)
    log.error(f"[BG] Desistindo apos {MAX_TENTATIVAS_BG} tentativas para {nome} ({email})")


def _gerar_plano_interno(dados):

    # Extrair campos do formulario
    nome               = dados.get('nome', 'Paciente')
    email              = dados.get('email', '').strip()
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

    # ── Validação crítica ANTES de qualquer custo (Vision + Claude) ─────────
    # Se os dados tiverem erro crítico, levanta DadosInvalidosError imediatamente.
    # Isso evita gastar tokens de Vision em planos que serão bloqueados.
    _validar_dados_criticos(dados)

    # ── Processar imagens de exames com Claude Vision (se houver) ────────────
    job_id   = dados.get('_plano_id')   # injetado por verificar_fila
    whatsapp = dados.get('whatsapp', '')
    if job_id:
        valores_vision = processar_imagens_exames(job_id, nome, whatsapp)
        if valores_vision:
            log.info(f"[VISION] {len(valores_vision)} valor(es) extraído(s) para {nome}: {list(valores_vision.keys())}")
            dados.update(valores_vision)  # enriquece dados com valores reais dos exames

    # ── Bloco de resultados de exames (Vision + texto) para injetar no prompt ──
    def _val(campo):
        v = dados.get(campo, '')
        return str(v).strip() if v else ''

    bloco_exames = f"""
RESULTADOS DE EXAMES LABORATORIAIS DA PACIENTE (use estes valores para personalizar doses):
- Glicose em jejum:       {_val('exame_glicose') or 'não informado'}
- Hemoglobina glicada:    {_val('exame_hemoglobina_glicada') or 'não informado'}
- Insulina em jejum:      {_val('exame_insulina_jejum') or 'não informado'}
- TOTG:                   {_val('exame_totg') or 'não informado'}
- Hemograma:              {_val('exame_hemograma') or 'não informado'}
- Ferritina:              {_val('exame_ferritina') or 'não informado'}
- Ferro sérico:           {_val('exame_ferro_serico') or 'não informado'}
- Saturação de transferrina: {_val('exame_sat_transferrina') or 'não informado'}
- Vitamina D:             {_val('exame_vitamina_d') or 'não informado'}
- Vitamina B12:           {_val('exame_vitamina_b12') or 'não informado'}
- TSH:                    {_val('exame_tsh') or 'não informado'}
- T4 livre:               {_val('exame_t4_livre') or 'não informado'}
- Ácido fólico:           {_val('exame_acido_folico') or 'não informado'}
- Cálcio:                 {_val('exame_calcio') or 'não informado'}
- Magnésio:               {_val('exame_magnesio') or 'não informado'}
- Zinco:                  {_val('exame_zinco') or 'não informado'}
- Creatinina:             {_val('exame_creatinina') or 'não informado'}
- Colesterol total:       {_val('exame_colesterol') or 'não informado'}
- Triglicerídeos:         {_val('exame_triglicerideos') or 'não informado'}
IMPORTANTE: quando "não informado", aplique a dose preventiva padrão do protocolo e escreva "Como você não realizou esse exame, vamos trabalhar com a dose preventiva padrão."
"""

    # Calculos clinicos automaticos
    calculos = calcular_dados_clinicos(dados)

    if calculos:
        bloco_calculos = f"""
CALCULOS CLINICOS JA REALIZADOS (use estes valores exatos no plano):
[DADOS INTERNOS — NAO mostrar no PDF: TMB={calculos['tmb']} kcal | Atividade={calculos['fator_nome']} | Manutencao={calculos['manutencao']} kcal]
- Trimestre: {calculos['tri_nome']}
- IMC: {calculos['imc']} — Categoria: {calculos['categoria_peso']}
- Peso ideal para gestar (IMC 22): {calculos['peso_ideal']} kg
- Calorias do plano: {calculos['calorias_alvo']} kcal/dia
- Estrategia (USO INTERNO — NAO mencionar deficit no PDF): {calculos['estrategia']}
- Meta de peso para este trimestre (INCLUIR no PDF): {calculos['meta_peso']}
- Distribuicao de macros: {calculos['macro_label']}
- Proteina: {calculos['prot_g']}g/dia ({calculos['prot_pct']}% das calorias)
- Carboidrato: {calculos['carb_g']}g/dia ({calculos['carb_pct']}% das calorias)
- Gordura: {calculos['gord_g']}g/dia ({calculos['gord_pct']}% das calorias)
- Meta de agua: {calculos['agua_l']}L/dia
- DIABETES GESTACIONAL: {"SIM — APLICAR PROTOCOLO DG COMPLETO (medicoes de glicose em cada refeicao, macros 40/35/25)" if calculos['tem_dg'] else "NAO"}
- PERCENTIL BAIXO / RESTRICAO DE CRESCIMENTO FETAL: {"SIM — macros 40/35/25" if calculos['tem_percentil_b'] else "NAO"}

REGRAS ABSOLUTAS PARA A SECAO DE CALCULOS NO PDF:
1. MOSTRAR apenas: IMC + categoria, peso ideal para gestar, calorias do plano, macros em g e %, meta de agua, meta de peso.
2. NAO mostrar: TMB, fator de atividade fisica, calorias de manutencao — sao dados internos de calculo, confundem a paciente.
3. Apresente as calorias do plano como "o valor ideal para nutrir voce e o bebe com seguranca neste trimestre" — SEM mencionar deficit, reducao ou corte.
4. A meta de peso deve ser apresentada conforme o protocolo: para sobrepeso no 1o tri, dizer que o objetivo é um controle de peso seguro para a gestação. EXCECAO: se a paciente mencionou em "Preferência" ou "Observações" que NÃO quer emagrecer ou tem resistência ao emagrecimento, respeite e reformule como "manter o peso estável neste trimestre, priorizando a saúde do bebê" — nunca imponha emagrecimento a quem não quer.
5. Nunca use as palavras "deficit", "reducao calorica" ou "corte de calorias" no PDF."""
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
            trimestre_codigo = 'III' if _s > 27 else ('II' if _s > 13 else 'I')
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
        contexto_trimestre = """CONTEXTO DO 2o TRIMESTRE (semanas 13 a 27):
E o trimestre do "renascimento" — os enjoos costumam diminuir,
a energia volta e a barriga comeca a aparecer de forma bonita.
E o melhor momento para estabelecer habitos solidos.

CONDUTAS ESPECIFICAS PARA O 2o TRIMESTRE:
- Acrescentar +175 kcal ao dia em relacao a manutencao (ja calculado)
- O bebe esta em fase de crescimento acelerado — proteina e FUNDAMENTAL
- Ferro e calcio tornam-se ainda mais importantes neste periodo
- Constipacao pode continuar — fibras, agua e movimento sao essenciais
- Exercicios fisicos sao geralmente bem tolerados (com liberacao medica)
- Hidratacao: peso x 40ml/dia
- Inchazo leve pode comecar — monitorar ingestao de sodio
- Omega-3 DHA e crucial para desenvolvimento cerebral fetal
- Tom da carta: celebrar a fase de energia e estimular a construcao de habitos"""

    else:
        contexto_trimestre = """CONTEXTO DO 3o TRIMESTRE (semanas 27 a 41):
A reta final da gestacao. O bebe esta crescendo rapidamente e o corpo
da mae esta se preparando para o parto. E normal sentir:
- Maior dificuldade para comer grandes volumes (bebe ocupa espaco)
- Refluxo e azia mais frequentes
- Inchazo nos pes e maos
- Dificuldade para dormir
- Maior cansaco e falta de ar

CONDUTAS ESPECIFICAS PARA O 3o TRIMESTRE:
- Refeicoes MENORES e mais frequentes — o estomago tem menos espaco
- Acrescentar +175 kcal ao dia em relacao a manutencao (ja calculado)
- Hidratacao: peso x 40ml/dia (mesmo do 2o trimestre — manter rigorosamente)
- Evitar alimentos que pioram refluxo: frituras, acidos, cafe em excesso
- Calcio e vitamina D sao criticos para mineralizacao ossea do bebe
- Ferro: verificar ferritina — anemia no 3o trimestre e mais perigosa
- Proteina alta para suportar crescimento fetal e preparar o perineo
- CEIA OBRIGATORIA — impede hipoglicemia noturna
- Exercicios de baixo impacto (caminhada, hidroginastica pre-natal se liberado)
- Tom da carta: encorajar a chegada da reta final, celebrar a jornada,
  preparar emocionalmente para o parto"""

    # ── Prompt clinico completo para o Claude ────────────────────────────────
    prompt = f"""Você é Dra. Jessica D'Agostini, nutricionista especialista em gestação da equipe Gestar Bem.
Seu método é clínico, estratégico e individualizado, nunca genérico.

REGRAS ABSOLUTAS DE ESCRITA (violação é inaceitável):
1. ACENTUAÇÃO: Todo o plano deve estar em português brasileiro correto, com acentos impecáveis. NUNCA escreva: "nao", "sao", "calcio", "magnesio", "proteina", "vitamina", "acucar", "tambem", "e" no lugar de "é", "a" no lugar de "à". Sempre: "não", "são", "cálcio", "magnésio", "proteína", "vitamina", "açúcar", "também", "é", "à". Um plano sem acentos é inaceitável.
2. SEM TRAVESSÕES: Nunca use travessão (—) no texto. Substitua sempre por vírgula, ponto ou parênteses (somente quando fizer sentido gramatical).

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

{bloco_exames}

{contexto_trimestre}

PROTOCOLO CLINICO — REGRAS QUE VOCE SEGUE RIGOROSAMENTE:

1. ANALISE DE EXAMES E QUADROS CLINICOS:
   DETECCAO DE DIABETES GESTACIONAL: aplique as condutas de DG se QUALQUER uma das condicoes abaixo for verdadeira:
   a) "DIABETES GESTACIONAL" aparece no campo "Quadros clínicos relatados pela paciente" ou Observações
   b) Glicose em jejum >= 92 mg/dL (campo RESULTADOS DE EXAMES acima)
   c) Hemoglobina glicada (HbA1c) >= 6,5% (campo RESULTADOS DE EXAMES acima)
   d) TOTG 2h >= 140 mg/dL (campo RESULTADOS DE EXAMES acima)
   ATENÇÃO: a paciente pode ter DG e NÃO SABER — confie nos valores dos exames, não apenas no que ela declarou
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
   - B12: SEMPRE indicar suplemento (e suplemento base de manutencao para todas as pacientes)
     - B12 < 500 → dose aumentada (ver protocolo de suplementacao)
     - B12 >= 500 → dose padrao de manutencao (ver protocolo de suplementacao)
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

3. BEBIDAS — REGRAS OBRIGATORIAS:
   - CAFE: LIMITADO a 2 xícaras pequenas por dia (cafeína máx. ~200mg/dia na gestação).
     NUNCA coloque café como alimento "livre" ou "à vontade". Se a paciente pergunta,
     oriente: máximo 2 xícaras pequenas ao dia, preferencialmente de manhã.
     Se ela tiver DG, hipertensão ou refluxo: reduzir ou eliminar.
   - CHA VERDE, CHA PRETO, MATE: também contêm cafeína — mesma regra do café.
   - REFRIGERANTE: evitar completamente (corante, açúcar, sódio, cafeína).
   - SUCOS NATURAIS: permitidos com moderação (máx. 1 copo/dia — preferir fruta inteira).
   - AGUA DE COCO: permitida e incentivada (hidratação + eletrólitos).

5. SINTOMAS — AJUSTES:
   - Enjoo/nausea: refeicoes menores e mais frequentes, alimentos secos no cafe,
     evitar odores fortes, gengibre em quantidades seguras
   - Constipacao: aumentar fibras, agua e movimento
   - Desejo por doce: proteina + gordura boa nas refeicoes para estabilizar glicemia
   - Refluxo/azia (3o tri): evitar frituras, acidos, refeicoes grandes a noite

6. SUPLEMENTACAO — PROTOCOLO OFICIAL DRA. JESSICA D'AGOSTINI:
   INCLUIR MARCAS, DOSES E LINK DA DUX conforme protocolo abaixo.
   SEMPRE incluir o link da DUX como texto clicavel: https://www.duxhumanhealth.com
   SEMPRE informar o cupom: PACJESSICADAGOSTINI (desconto especial para pacientes da Dra. Jessica).
   NAO inclua a frase "confirme com seu medico" — o protocolo ja foi validado clinicamente.

   ═══════════ I TRIMESTRE ═══════════

   SUPLEMENTOS BASE (sempre indicar, independente dos exames):

   1) Metilfolato (ou acido folico)
      Dose: 400-600 mcg/dia
      Marcas: Thorne Basic Prenatal | Pure Encapsulations Prenatal Nutrients | Natele Gest | Femibion 1

   2) DHA (Omega-3)
      Dose: 500-1000 mg DHA/dia
      Marcas: Nordic Naturals Prenatal DHA | Essential Nutrition Super Omega TG | Vitafor Omegafor Mom | Nutrify Omega 3 TG | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   3) EPA
      Dose: 300 mg/dia
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   4) Colina
      Dose: 250-500 mg/dia
      Marcas: Now Choline & Inositol | Thorne Prenatal | manipulado individualizado

   5) Magnesio bisglicinato
      Dose: 200-400 mg/dia
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   6) Metilcobalamina (B12)
      Dose base: 500 mcg/dia
      Marcas: Jarrow Methyl B12 | Now Methyl B12 | manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   SUPLEMENTOS CONDICIONAIS — I TRIMESTRE (ajustar dose conforme exame):

   7) Vitamina D
      SE exame_vitamina_d >= 50: dose 1000-2000 UI/dia (manutencao)
      SE exame_vitamina_d < 50 (ou exame nao realizado): dose 4000 UI/dia (correcao de deficiencia)
      Marcas: Addera D3 | Dprev | Vitafor Vita D3 | Essential Nutrition Vit D3 | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   8) Ferro bisglicinato
      SE exame_ferritina >= 70: dose 30-40 mg ferro elementar/dia (prevencao)
      SE exame_ferritina < 70 (ou ferritina baixa/anemia): dose 50-60 mg/dia
      Marcas: Ferrochel Albion | Cheltin Ferr | Blutforte Folico | Natele Ferro | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   9) Vitamina B12 (dose aumentada se deficiencia)
      SE exame_vitamina_b12 < 500: aumentar dose para 500-1000 mcg/dia (mesmas marcas do item 6 acima)

   FORMULA BASE COMPLETA — I TRIMESTRE (sem deficiencias):
   Metilfolato 400-600 mcg | Metilcobalamina 500 mcg | DHA 500-1000 mg | EPA 300 mg
   Vitamina D3 2000 UI | Colina 250 mg | Magnesio bisglicinato 200 mg | Ferro bisglicinato 30 mg

   ═══════════ II TRIMESTRE ═══════════

   SUPLEMENTOS BASE (sempre indicar):

   1) Metilfolato (ou acido folico)
      Dose: 400-600 mcg/dia
      Marcas: Thorne Basic Prenatal | Pure Encapsulations Prenatal Nutrients | Natele Gest | Femibion 2

   2) DHA (Omega-3)
      Dose: 700-1000 mg DHA/dia (dose maior que no 1o tri)
      Marcas: Nordic Naturals Prenatal DHA | Essential Nutrition Super Omega TG | Vitafor Omegafor Mom | Nutrify Omega 3 TG | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   3) EPA
      Dose: 300-500 mg/dia (modulacao inflamatoria e saude placentaria)
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   4) Colina
      Dose: 350-500 mg/dia
      Marcas: Now Choline & Inositol | Thorne Prenatal | manipulado individualizado | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   5) Magnesio bisglicinato
      Dose: 300-400 mg/dia
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   6) Metilcobalamina (B12)
      Dose base: 500 mcg/dia
      Marcas: Jarrow Methyl B12 | Now Methyl B12 | manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   7) Calcio
      Dose: 300-500 mg/dia (novo no 2o trimestre)
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   SUPLEMENTOS CONDICIONAIS — II TRIMESTRE (ajustar dose conforme exame):

   8) Vitamina D
      SE exame_vitamina_d >= 50: dose 2000 UI/dia
      SE exame_vitamina_d < 50 (ou exame nao realizado): dose 4000 UI/dia
      Marcas: Addera D3 | Dprev | Vitafor Vita D3 | Essential Nutrition Vit D3 | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   9) Ferro bisglicinato
      SE exame_ferritina >= 70: dose 30-40 mg ferro elementar/dia (prevencao)
      SE exame_ferritina < 70 (ou anemia): dose 60 mg/dia
      Marcas: Ferrochel Albion | Cheltin Ferr | Blutforte Folico | Natele Ferro | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   10) Vitamina B12 (dose aumentada se deficiencia)
      SE exame_vitamina_b12 < 500: aumentar dose para 500-1000 mcg/dia (mesmas marcas do item 6 acima)

   OPCAO ALTERNATIVA — II TRIMESTRE (multivitaminico completo):
   Se a paciente preferir um unico produto: Regenesis Premium | Femibion 2 | Materna Nestle | Ogestan Gold
   Neste caso, AINDA adicionar separadamente: DHA/EPA | Vitamina D | Magnesio | Colina | Ferro | Vitamina B12
   (conforme doses e condicoes acima)

   FORMULA BASE COMPLETA — II TRIMESTRE (sem deficiencias):
   Metilfolato 400-600 mcg | Metilcobalamina 500 mcg | DHA 700-1000 mg | EPA 300-500 mg
   Vitamina D3 2000 UI | Colina 350 mg | Magnesio bisglicinato 300 mg | Ferro bisglicinato 30-40 mg | Calcio 300-500 mg

   ═══════════ III TRIMESTRE ═══════════

   SUPLEMENTOS BASE (sempre indicar):

   1) Metilfolato (ou acido folico)
      Dose: 400-600 mcg/dia
      Marcas: Thorne Basic Prenatal | Pure Encapsulations Prenatal Nutrients | Natele Gest | Femibion 2

   2) DHA (Omega-3)
      Dose: 1000 mg DHA/dia (dose maxima — reta final de desenvolvimento cerebral do bebe)
      Marcas: Nordic Naturals Prenatal DHA | Essential Nutrition Super Omega TG | Vitafor Omegafor Mom | Nutrify Omega 3 TG | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   3) EPA
      Dose: 500 mg/dia (inflamacao, saude vascular, placenta, preparo metabolico materno)
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   4) Colina
      Dose: 450-500 mg/dia
      Marcas: Now Choline & Inositol | Thorne Prenatal | manipulado individualizado | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   5) Magnesio bisglicinato
      Dose: 300-400 mg/dia
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   6) Metilcobalamina (B12)
      Dose base: 500 mcg/dia
      Marcas: Jarrow Methyl B12 | Now Methyl B12 | manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   7) Calcio
      Dose: 500-1000 mg/dia (dose maior que no 2o tri — demanda fetal aumenta na reta final)
      Marcas: manipulado individualizado | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   SUPLEMENTOS CONDICIONAIS — III TRIMESTRE (ajustar dose conforme exame):

   8) Vitamina D
      SE exame_vitamina_d >= 50: dose 2000 UI/dia
      SE exame_vitamina_d < 50 (ou exame nao realizado): dose 4000 UI/dia
      Marcas: Addera D3 | Dprev | Vitafor Vita D3 | Essential Nutrition Vit D3 | Puravida | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   9) Ferro bisglicinato
      ATENCAO: demanda fetal por ferro aumenta bastante no 3o trimestre.
      SE exame_ferritina >= 70: dose 40 mg ferro elementar/dia (prevencao — dose maior que nos trimestres anteriores)
      SE exame_ferritina < 70 (ou anemia): dose 60 mg/dia
      Marcas: Ferrochel Albion | Cheltin Ferr | Blutforte Folico | Natele Ferro | Puravida | Essential | Vitafor | DUX (https://www.duxhumanhealth.com - CUPOM: PACJESSICADAGOSTINI)

   10) Vitamina B12 (dose aumentada se deficiencia)
      SE exame_vitamina_b12 < 500: aumentar dose para 500-1000 mcg/dia (mesmas marcas do item 6 acima)

   OPCAO ALTERNATIVA — III TRIMESTRE (multivitaminico completo):
   Se a paciente preferir um unico produto: Regenesis Premium | Femibion 2 | Materna Nestle | Ogestan Gold
   Neste caso, AINDA adicionar separadamente: DHA/EPA | Vitamina D | Magnesio | Colina | Ferro | Vitamina B12
   (conforme doses e condicoes acima)

   FORMULA BASE COMPLETA — III TRIMESTRE (sem deficiencias):
   Metilfolato 400-600 mcg | Metilcobalamina 500 mcg | DHA 1000 mg | EPA 500 mg
   Vitamina D3 2000 UI | Colina 450-500 mg | Magnesio bisglicinato 300-400 mg | Ferro bisglicinato 40 mg | Calcio 500-1000 mg

   IMPORTANTE: NAO inclua a frase "confirme com seu medico antes de iniciar qualquer suplemento".
   Este protocolo ja foi validado clinicamente pela Dra. Jessica D'Agostini.

7. LINGUAGEM E TOM:
   - Acolhedor, pessoal, cristao
   - Trate sempre pelo primeiro nome
   - Palavras de encorajamento, proposito e fe
   - Nunca tom clinico frio — sempre humanizado
   - Adapte o tom ao momento do trimestre (ver contexto acima)

8. CONSISTENCIA DO PLANO — REGRA CRITICA:
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

9. EXERCICIOS — REGRA ABSOLUTA:
   NUNCA gere secao, paragrafo ou qualquer orientacao sobre exercicios fisicos neste PDF.
   Os planos de treino sao enviados separadamente como arquivos proprios (academia e casa).
   Os dados de exercicio informados (nivel, periodo, limitacoes) servem APENAS para
   ajustar horarios e composicao das refeicoes (ex: pre-treino, pos-treino).
   Qualquer mencao a exercicios fora do contexto nutricional e PROIBIDA neste documento.

10. ALIMENTOS ESSENCIAIS POR TRIMESTRE — OBRIGATORIOS NO CARDAPIO:
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
X item negativo → bullet VERMELHO (coisas para NAO FAZER) — SEMPRE com X maiusculo
ATENCAO: texto → alerta vermelho em negrito
"texto entre aspas" → italico centralizado roxo (para citacoes biblicas)
=== → quebra de pagina (use entre secoes grandes para comecar numa nova pagina)
--- → separador visual (linha horizontal, NAO quebra de pagina)
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
Apresente de forma didatica e acolhedora, SEM jargao tecnico desnecessario.
MOSTRAR APENAS (nesta ordem):
- Trimestre atual e semanas
- IMC (valor + categoria em linguagem simples, ex: "Acima do peso ideal para gestar")
- Peso ideal para gestar (em kg)
- Calorias do seu plano (valor em kcal + frase explicando que e o valor ideal para nutrir ela e o bebe)
- Meta de peso para este trimestre (conforme calculado — para sobrepeso no 1o tri: falar em perder peso de forma segura)
- Distribuicao de macronutrientes (proteinas, carboidratos, gorduras — em gramas e percentual)
- Meta de agua (em litros)
NAO MOSTRAR: TMB, fator de atividade, calorias de manutencao — esses sao dados internos que confundem a paciente.
Use os valores ja calculados acima — nao invente outros.

## SUPLEMENTACAO RECOMENDADA
Siga o protocolo oficial da Dra. Jessica (regra 6 acima) considerando o trimestre ({trimestre_codigo}o TRIMESTRE).
Para cada suplemento: nome, motivo (relacionando com exames/sintomas dela), dose exata, como tomar, marcas recomendadas.
SEMPRE incluir o link da DUX (https://www.duxhumanhealth.com) e o cupom PACJESSICADAGOSTINI quando DUX aparecer nas marcas.
Use os valores dos exames ja informados para personalizar as doses condicionais (Vitamina D, Ferro, B12).
NAO inclua a frase "confirme com seu medico" — o protocolo ja foi validado clinicamente pela Dra. Jessica D'Agostini.

REGRA IMPORTANTE — SINTOMAS SEM EXAMES:
Se a paciente NAO informou exames laboratoriais MAS apresenta sintomas como fadiga/cansaco, queda de cabelo, acne/espinhas, unhas fracas ou alteracoes de humor:
- MENCIONAR EXPLICITAMENTE no plano que esses sintomas sao indicadores frequentes de deficiencias nutricionais (B12, Vitamina D, Ferritina, Ferro)
- ENFATIZAR que a suplementacao e ESSENCIAL mesmo sem confirmacao laboratorial
- RECOMENDAR com urgencia que ela realize os exames o quanto antes para ajustar as doses com precisao
- NAO omitir a suplementacao so porque os exames estao ausentes — usar as doses padrao do protocolo

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

REGRA ESPECIAL — DIABETES GESTACIONAL:
Se o campo "DIABETES GESTACIONAL: SIM" estiver nos calculos acima, OBRIGATORIAMENTE inclua
os seguintes alertas de medicao EXATAMENTE nestes momentos do plano, usando o marcador "ATENCAO:":
Esta regra e ABSOLUTA — plano de DG sem alertas de medicao e INACEITAVEL.

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
lembretes dos pontos mais importantes do plano.
Inclua ao final uma secao de contato com o seguinte texto exato (adaptando apenas o nome da paciente):
"Duvidas? Me chama no WhatsApp: https://wa.me/554199920539"
O link deve aparecer como texto clicavel no PDF.
Ao final de tudo, a assinatura obrigatoria e exata:
Dra. Jessica D'Agostini
Nutricionista | CRN 18978

Gere o plano COMPLETO, detalhado e personalizado. Minimo de 2000 palavras.
Use os calculos clinicos ja fornecidos — nao recalcule, nao mude os valores.
ATENCAO CRITICA: NUNCA corte uma secao no meio. Cada secao deve estar 100% completa antes de comecar a proxima.
Se a secao de "Alimentos a evitar" foi iniciada, ela DEVE ser encerrada com todos os itens relevantes antes de qualquer despedida ou assinatura.
A assinatura (Dra. Jessica D'Agostini) so aparece UMA VEZ, ao final das Consideracoes Finais.

ANTES DE ENTREGAR O PLANO, FACA UMA REVISAO INTERNA OBRIGATORIA (12 pontos):
1. O perfil alimentar e consistente do inicio ao fim? (onivora tem proteina animal em todas as refeicoes?)
2. Todas as refeicoes tem proteina em gramas especificadas?
3. As intolerancias foram respeitadas em TODAS as refeicoes E substituicoes?
4. Nenhuma refeicao contradiz outra em termos de perfil alimentar?
5. A secao SEUS CALCULOS PERSONALIZADOS NAO contem TMB, fator de atividade ou calorias de manutencao?
6. A meta de peso esta correta para o IMC e trimestre desta paciente?
7. Todas as secoes obrigatorias estao completas e nao foram cortadas no meio?
8. A secao de alimentos a evitar esta completa com TODOS os itens relevantes (intolerancia, alergia, DG, etc.)?
9. O link do WhatsApp esta presente nas Consideracoes Finais?
10. A assinatura da Dra. Jessica aparece apenas uma vez, ao final?
11. ACENTUACAO E PONTUACAO: Todo o texto esta em portugues correto com acentos (nao, sao, tambem, proteina, calcio, magnesio, vitamina, etc.)? Palavras sem acento onde deveriam ter sao INACEITAVEIS. Revise e corrija QUALQUER palavra com acento faltando antes de entregar.
12. PONTUACAO: Todas as frases terminam com ponto final, virgulas estao no lugar certo, listas usam hifens ou bullets consistentes?
Se encontrar qualquer inconsistencia nos 12 pontos acima, corrija antes de entregar."""

    # ── Chamar o Claude ───────────────────────────────────────────────────────
    log.info(f"Chamando Claude para: {nome} ({semanas_gestacao} semanas)")
    message = _anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=16000,
        system="Você é a Dra. Jessica D'Agostini, nutricionista especializada em gestação. Escreva SEMPRE em português do Brasil com acentuação correta (ã, ç, á, é, ó, ú, etc.). Nunca omita acentos. O PDF suporta Unicode completo.",
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

    # ── Salvar PDF no banco para preview no painel ────────────────────────────
    job_id_interno = dados.get('_plano_id')
    if job_id_interno:
        try:
            conn_pdf = get_db()
            try:
                cur_pdf = conn_pdf.cursor()
                cur_pdf.execute(
                    "UPDATE planos_agendados SET pdf_base64 = %s WHERE id = %s",
                    (pdf_b64, job_id_interno)
                )
                conn_pdf.commit()
            finally:
                conn_pdf.close()
        except Exception as ex:
            log.warning(f"[PDF] Falha ao salvar pdf_base64 no banco: {ex}")

    # ── Verificar se plano precisa de aprovação antes do envio ───────────────
    requer, motivo_aprov = _requer_aprovacao(dados, calculos)
    if requer:
        log.info(f"[APROVACAO] Plano de {nome} aguardando aprovação: {motivo_aprov}")
        raise AguardandoAprovacaoError(motivo_aprov, pdf_b64)

    # ── Selecionar links de treino (tokens únicos por paciente) ─────────────
    links_treino, treino_aguardando_liberacao = selecionar_links_exercicio(dados, trimestre_codigo, email=email)

    # ── Montar lista de PDFs para o email (apenas nutricao como anexo) ────────
    pdfs_email = [(pdf_nutri, nome_pdf)]

    # ── Enviar email ──────────────────────────────────────────────────────────
    # email ja validado no inicio da funcao — sempre presente aqui
    # NAO capturamos a excecao aqui: se o email falhar, o erro sobe para
    # verificar_fila() que vai retentar o job automaticamente (ate MAX_TENTATIVAS)
    enviar_email_pdf(email, nome, pdfs_email, links_treino=links_treino,
                     treino_aguardando_liberacao=treino_aguardando_liberacao)
    log.info(f"[INTERNO] Concluido para {nome} — email enviado para {email} com {len(links_treino)} link(s) de treino")


# ── Preview do PDF gerado ─────────────────────────────────────────────────────

@app.route('/painel/pdf/<int:job_id>')
def painel_pdf(job_id):
    """Serve o PDF gerado para preview no painel. Requer autenticação."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2>Acesso negado.</h2>', 403
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT pdf_base64, dados->>'nome' FROM planos_agendados WHERE id = %s", (job_id,))
        row = cur.fetchone()
    finally:
        if conn: conn.close()
    if not row or not row[0]:
        return '<h2 style="font-family:sans-serif">PDF não disponível para este plano.</h2>', 404
    pdf_bytes = base64.b64decode(row[0])
    nome_s = (row[1] or 'Plano').replace(' ', '_')
    from flask import Response
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{nome_s}_preview.pdf"'}
    )


# ── Editar dados clínicos de um plano ─────────────────────────────────────────

@app.route('/painel/dados/<int:job_id>', methods=['POST'])
def painel_editar_dados(job_id):
    """Atualiza campos clínicos no JSONB dados do plano. Não reprocessa — só salva."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2>Acesso negado.</h2>', 403

    CAMPOS_EDITAVEIS = [
        'semanas_gestacao', 'peso_atual', 'altura', 'idade',
        'exame_glicose', 'quadros_clinicos', 'intolerancia', 'observacoes',
    ]
    novos = {c: request.form.get(c, '').strip() for c in CAMPOS_EDITAVEIS if request.form.get(c, '').strip()}
    if not novos:
        token_safe = urllib.parse.quote(token_recebido, safe='')
        return redirect(f"/painel/detalhes/{job_id}?token={token_safe}")

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE planos_agendados SET dados = dados || %s::jsonb WHERE id = %s",
            (json.dumps(novos, ensure_ascii=False), job_id)
        )
        if cur.rowcount == 0:
            return '<h2>Plano não encontrado.</h2>', 404
        conn.commit()
        log.info(f"[PAINEL] Dados editados para plano #{job_id}: {list(novos.keys())}")
    except Exception as e:
        log.error(f"[PAINEL/DADOS] Erro: {e}")
        return '<h2>Erro ao salvar. Tente novamente.</h2>', 500
    finally:
        if conn: conn.close()

    token_safe = urllib.parse.quote(token_recebido, safe='')
    return redirect(f"/painel/detalhes/{job_id}?token={token_safe}")


# ── Aprovar e enviar plano ────────────────────────────────────────────────────

@app.route('/painel/aprovar/<int:job_id>', methods=['POST'])
def painel_aprovar(job_id):
    """Envia o plano gerado para a paciente após aprovação da equipe."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2>Acesso negado.</h2>', 403

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT dados, pdf_base64, aguardando_aprovacao
            FROM planos_agendados WHERE id = %s
        """, (job_id,))
        row = cur.fetchone()
    finally:
        if conn: conn.close()

    if not row:
        return '<h2>Plano não encontrado.</h2>', 404
    dados, pdf_b64, aguardando = row
    if not pdf_b64:
        token_safe = urllib.parse.quote(token_recebido, safe='')
        return redirect(f"/painel/detalhes/{job_id}?token={token_safe}&erro=pdf_ausente")

    nome  = dados.get('nome', 'Paciente')
    email = dados.get('email', '').strip()
    semanas = dados.get('semanas_gestacao', '1')

    # Calcular trimestre para selecionar treinos
    try:
        _s = _extrair_numero(semanas, inteiro=True)
        trimestre_codigo = 'III' if _s > 27 else ('II' if _s > 13 else 'I')
    except Exception:
        trimestre_codigo = 'I'

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        nome_pdf  = nome_arquivo_pdf(nome, semanas)
        links_treino, treino_aguardando = selecionar_links_exercicio(dados, trimestre_codigo, email=email)
        enviar_email_pdf(email, nome, [(pdf_bytes, nome_pdf)],
                         links_treino=links_treino,
                         treino_aguardando_liberacao=treino_aguardando)
        log.info(f"[APROVAR] Plano #{job_id} aprovado e enviado para {email}")
    except Exception as e:
        log.error(f"[APROVAR] Falha ao enviar plano #{job_id}: {e}")
        return f'<h2 style="font-family:sans-serif;color:#c00">Erro ao enviar: {_html.escape(str(e))}</h2>', 500

    # Marcar como enviado e limpar dados temporários
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE planos_agendados
            SET processado           = TRUE,
                processado_em        = NOW(),
                erro                 = NULL,
                aguardando_aprovacao = FALSE,
                motivo_aprovacao     = NULL,
                pdf_base64           = NULL
            WHERE id = %s
        """, (job_id,))
        conn.commit()
    finally:
        if conn: conn.close()

    token_safe = urllib.parse.quote(token_recebido, safe='')
    return redirect(f"/painel/detalhes/{job_id}?token={token_safe}")


# ── Endpoint de teste de email ────────────────────────────────────────────────

@app.route('/testar-email', methods=['POST'])
def testar_email():
    """Testa o envio de email sem gerar plano completo. Protegido por PAINEL_TOKEN."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return jsonify({"status": "erro", "mensagem": "Acesso negado"}), 403

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
                COUNT(*)                                                                       AS total,
                COUNT(*) FILTER (WHERE processado = TRUE
                                 AND processado_em >= NOW() - INTERVAL '24 hours'
                                 AND (erro IS NULL OR erro NOT LIKE 'DADOS_INVALIDOS%'))       AS hoje,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas = 0
                                 AND (proxima_tentativa IS NULL OR proxima_tentativa <= NOW())) AS pendentes,
                COUNT(*) FILTER (WHERE processado = FALSE AND tentativas > 0)                  AS com_falha,
                COUNT(*) FILTER (WHERE processado = TRUE
                                 AND erro IS NULL)                                             AS concluidos,
                COUNT(*) FILTER (WHERE erro LIKE 'DADOS_INVALIDOS%')                           AS dados_invalidos
            FROM planos_agendados
        """)
        r = cur.fetchone()
        total, hoje, pendentes, com_falha, concluidos, dados_invalidos = r

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
                erro,
                aguardando_aprovacao
            FROM planos_agendados
            ORDER BY criado_em DESC
            LIMIT 20
        """)
        registros = cur.fetchall()
        cur.close()
    except Exception as e:
        log.error(f"[PAINEL] Erro ao consultar banco: {e}")
        return '<h2 style="font-family:sans-serif;color:#c00">Erro interno. Tente novamente em instantes.</h2>', 500
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

    token_safe = urllib.parse.quote(token_recebido, safe='')
    linhas_html = ''
    for reg in registros:
        rid, nome, email, agendado, processado, tentativas, processado_em, erro, aguardando = reg
        # Escape para evitar XSS — campos vêm diretamente do formulário das pacientes
        nome_s  = _html.escape(nome  or '-')
        email_s = _html.escape(email or '-')
        erro_s  = _html.escape((erro[:60] + '...') if erro and len(erro) > 60 else (erro or ''))
        if aguardando:
            status = '🔵 Aprovação'
        elif processado and not erro:
            status = '✅ Enviado'
        elif processado and erro and erro.startswith('DADOS_INVALIDOS'):
            status = '🚫 Dados Inválidos'
        elif processado and erro:
            status = '❌ Falhou'
        elif not processado and tentativas == 0:
            status = '🕐 Agendado'
        else:
            status = f'⚠️ Tentativas: {tentativas}'
        cor    = linha_cor(processado, tentativas, erro)
        ag_str = agendado.strftime('%d/%m %H:%M') if agendado else '-'
        pr_str = processado_em.strftime('%d/%m %H:%M') if processado_em else '-'
        linhas_html += f"""
        <tr style="background:{cor}">
            <td>{rid}</td>
            <td>{nome_s}</td>
            <td style="font-size:12px">{email_s}</td>
            <td>{ag_str}</td>
            <td>{pr_str}</td>
            <td>{status}</td>
            <td style="font-size:11px;color:#c00">{erro_s}</td>
            <td><a href="/painel/detalhes/{rid}?token={token_safe}" style="color:#9B27AF;text-decoration:none;font-size:18px;" title="Ver detalhes">👁</a></td>
        </tr>"""

    agora = datetime.now(TZ_SP).strftime('%d/%m/%Y %H:%M')
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
    <a href="/painel?token={token_recebido}" style="display:flex;align-items:center;gap:12px;text-decoration:none;">
      <div style="background:#fff;border-radius:50%;width:60px;height:60px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        <img src="/imagens/gestar_ilustracao.png" alt="Logo" style="height:52px;">
      </div>
      <img src="/imagens/gestar_bem_svg.png" alt="Gestar Bem" style="height:44px;">
    </a>
    <div>
      <div class="titulo">Painel de Controle</div>
      <div class="sub-header">Sistema Gestar Bem</div>
    </div>
  </div>
  <div class="content">
  <div class="sub">Atualizado em {agora} (Brasília) &nbsp;|&nbsp; Atualiza automaticamente a cada 60s</div>

  <form method="GET" action="/painel/buscar" style="margin-bottom:28px;display:flex;gap:10px;align-items:center;">
    <input type="hidden" name="token" value="{_html.escape(token_recebido)}">
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
    <div class="card {'alerta' if dados_invalidos > 0 else ''}"><div class="num">{dados_invalidos}</div><div class="lab">Dados Inválidos</div></div>
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


@app.route('/reset-scheduler')
def reset_scheduler():
    """Reinicia o APScheduler sem precisar de redeploy. Util apos outages."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token = request.args.get('token', '')
    if not token_esperado or token != token_esperado:
        return jsonify({"erro": "nao autorizado"}), 403
    try:
        _scheduler.remove_job('verificar_fila')
        _scheduler.add_job(verificar_fila, 'interval', minutes=1, id='verificar_fila',
                           max_instances=1, coalesce=True)
        prox = _scheduler.get_job('verificar_fila').next_run_time
        log.info("Scheduler resetado manualmente via /reset-scheduler")
        return jsonify({"status": "ok", "mensagem": "scheduler resetado", "proxima_execucao": str(prox)}), 200
    except Exception as e:
        return jsonify({"status": "erro", "detalhe": str(e)}), 500


@app.route('/health')
def health():
    """
    Checagem completa da saude do sistema.
    Retorna 200 se tudo ok, 500 se qualquer componente critico falhar.
    Util para monitoramento externo (Fly.io, UptimeRobot, etc.).
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
    <a href="/painel?token={token_recebido}" style="display:flex;align-items:center;gap:12px;text-decoration:none;">
      <div style="background:#fff;border-radius:50%;width:60px;height:60px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        <img src="/imagens/gestar_ilustracao.png" alt="Logo" style="height:52px;">
      </div>
      <img src="/imagens/gestar_bem_svg.png" alt="Gestar Bem" style="height:44px;">
    </a>
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

    token_safe  = urllib.parse.quote(token_recebido, safe='')
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
        log.error(f"[PAINEL/BUSCAR] Erro ao consultar banco: {e}")
        return '<h2 style="font-family:sans-serif;color:#c00">Erro interno. Tente novamente em instantes.</h2>', 500
    finally:
        if conn: conn.close()

    if not registros:
        conteudo = f"""
        <a href="/painel?token={token_safe}" class="btn-voltar">← Voltar ao painel</a>
        <h3 style="color:#9B27AF">Nenhum registro encontrado para: {_html.escape(email_busca)}</h3>"""
        return _painel_html_base(token_recebido, conteudo), 200

    nome_paciente = registros[-1][1] or email_busca
    peso_inicial  = registros[0][4]  or '?'
    peso_atual    = registros[-1][4] or '?'

    linhas = ''
    for i, reg in enumerate(registros):
        rid, nome, email, semanas, peso, complic, sintomas, medic, processado, tentativas, criado_em = reg
        # Escape de todos os campos que vêm do banco (preenchidos pelas pacientes)
        semanas_s = _html.escape(str(semanas or '-'))
        peso_s    = _html.escape(str(peso    or '-'))
        complic_s = _html.escape((complic  or '-')[:60])
        sintomas_s= _html.escape((sintomas or '-')[:60])
        medic_s   = _html.escape((medic    or '-')[:40])
        status    = '✅' if processado else f'⚠️ {tentativas}x'
        data_str  = criado_em.strftime('%d/%m/%Y') if criado_em else '-'
        try:
            sw = int(''.join(filter(str.isdigit, semanas or '0')) or '0')
        except Exception:
            sw = 0
        tri = 'III' if sw > 27 else ('II' if sw > 13 else 'I')
        linhas  += f"""
        <tr>
          <td>{data_str}</td>
          <td>{semanas_s} sem &nbsp;<span style="color:#9B27AF;font-size:11px">{tri}º tri</span></td>
          <td>{peso_s} kg</td>
          <td style="font-size:12px">{complic_s}</td>
          <td style="font-size:12px">{sintomas_s}</td>
          <td style="font-size:12px">{medic_s}</td>
          <td>{status}</td>
          <td><a href="/painel/detalhes/{rid}?token={token_safe}" title="Ver detalhes" style="color:#9B27AF;font-size:18px;text-decoration:none;">👁</a></td>
        </tr>"""

    nome_paciente_s = _html.escape(str(nome_paciente))
    email_busca_s   = _html.escape(email_busca)
    conteudo = f"""
    <a href="/painel?token={token_safe}" class="btn-voltar">← Voltar ao painel</a>
    <h2 style="color:#9B27AF;margin-bottom:4px">🌸 {nome_paciente_s}</h2>
    <p style="color:#888;font-size:13px;margin-top:0">{email_busca_s} &nbsp;|&nbsp; {len(registros)} envio(s) &nbsp;|&nbsp; Peso inicial: {_html.escape(str(peso_inicial))}kg → Atual: {_html.escape(str(peso_atual))}kg</p>
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
        cur.execute("""
            SELECT dados, criado_em, processado, tentativas, erro,
                   aguardando_aprovacao, motivo_aprovacao,
                   (pdf_base64 IS NOT NULL) AS tem_pdf
            FROM planos_agendados WHERE id = %s
        """, (job_id,))
        row = cur.fetchone()
        cur.close()
    except Exception as e:
        log.error(f"[PAINEL/DETALHES] Erro ao consultar banco: {e}")
        return '<h2 style="font-family:sans-serif;color:#c00">Erro interno. Tente novamente em instantes.</h2>', 500
    finally:
        if conn: conn.close()

    if not row:
        return '<h2>Registro nao encontrado.</h2>', 404

    dados, criado_em, processado, tentativas, erro, aguardando, motivo_aprov, tem_pdf = row
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
            linhas += f'<tr><td class="label">{_html.escape(str(label))}</td><td class="valor">{_html.escape(str(valor))}</td></tr>'

    if aguardando:
        status = '🔵 Aguardando Aprovação'
    elif processado and not erro:
        status = '✅ Enviado com sucesso'
    elif processado and erro and erro.startswith('DADOS_INVALIDOS'):
        status = '🚫 Dados Inválidos — aguardando correção'
    elif processado and erro:
        status = f'❌ Falhou após {tentativas}x'
    else:
        status = f'⚠️ {tentativas} tentativa(s)'
    data_str   = criado_em.strftime('%d/%m/%Y às %H:%M') if criado_em else '-'
    nome_s     = _html.escape(nome)
    token_safe = urllib.parse.quote(token_recebido, safe='')
    email_enc  = urllib.parse.quote(dados.get('email', ''), safe='')
    voltar_busca = f"/painel/buscar?token={token_safe}&email={email_enc}"
    erro_s    = _html.escape(erro) if erro else ''

    btn_preview = ''
    if tem_pdf:
        btn_preview = f"""
    <a href="/painel/pdf/{job_id}?token={token_safe}" target="_blank"
       style="display:inline-block;margin-top:12px;background:#1976D2;color:#fff;
              border-radius:8px;padding:10px 22px;font-size:14px;font-weight:600;
              text-decoration:none;">
      👁 Visualizar plano
    </a>"""

    btn_aprovar = ''
    if aguardando:
        btn_aprovar = f"""
    <div style="background:#E3F2FD;border:1px solid #90CAF9;border-radius:8px;padding:16px;margin-top:20px;">
      <p style="margin:0 0 8px;font-weight:600;color:#1565C0">🔵 Aguardando aprovação</p>
      <p style="margin:0 0 12px;font-size:13px;color:#444">{_html.escape(motivo_aprov or '')}</p>
      <form method="POST" action="/painel/aprovar/{job_id}?token={token_safe}"
            onsubmit="return confirm('Aprovar e enviar o plano para {_html.escape(nome)}?');"
            style="display:inline-block;">
        <button type="submit"
          style="background:#2E7D32;color:#fff;border:none;border-radius:8px;
                 padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;">
          ✅ Aprovar e enviar
        </button>
      </form>
    </div>"""

    CAMPOS_EDITAVEIS_LABELS = [
        ('semanas_gestacao', 'Semanas de gestação'),
        ('peso_atual', 'Peso atual (kg)'),
        ('altura', 'Altura (cm)'),
        ('idade', 'Idade'),
        ('exame_glicose', 'Glicose em jejum (mg/dL)'),
        ('quadros_clinicos', 'Quadros clínicos'),
        ('intolerancia', 'Intolerância alimentar'),
        ('observacoes', 'Observações adicionais'),
    ]
    campos_edit_html = ''
    for chave, label in CAMPOS_EDITAVEIS_LABELS:
        valor_atual = _html.escape(str(dados.get(chave, '')))
        campos_edit_html += f"""
    <tr>
      <td class="label">{label}</td>
      <td><input type="text" name="{chave}" value="{valor_atual}"
                 style="width:100%;padding:6px 8px;border:1px solid #ccc;border-radius:4px;font-size:13px;"></td>
    </tr>"""

    form_editar = f"""
<details style="margin-top:20px;">
  <summary style="cursor:pointer;font-weight:600;color:#9B27AF;font-size:14px;padding:10px 0;">
    ✏️ Editar dados clínicos
  </summary>
  <form method="POST" action="/painel/dados/{job_id}?token={token_safe}" style="margin-top:12px;">
    <table style="max-width:700px;width:100%">
      <tbody>{campos_edit_html}</tbody>
    </table>
    <p style="font-size:12px;color:#888;margin-top:8px;">
      Após salvar, clique em "Reprocessar plano" para gerar um novo plano com os dados corrigidos.
    </p>
    <button type="submit"
      style="margin-top:8px;background:#F57C00;color:#fff;border:none;border-radius:8px;
             padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;">
      💾 Salvar alterações
    </button>
  </form>
</details>"""

    btn_reprocessar = f"""
    <form method="POST" action="/painel/reprocessar/{job_id}?token={token_safe}"
          onsubmit="return confirm('Reprocessar este plano? Um novo email será enviado para a paciente.');"
          style="margin-top:20px;display:inline-block;">
      <button type="submit"
        style="background:#9B27AF;color:#fff;border:none;border-radius:8px;
               padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;">
        🔄 Reprocessar plano
      </button>
    </form>
    <p style="color:#888;font-size:12px;margin-top:6px;">
      Reprocessar zera tentativas, gera novo plano e envia novo email para a paciente.
    </p>"""

    conteudo = f"""
    <h2 style="color:#9B27AF;margin-bottom:4px">📋 Detalhes do envio #{job_id}</h2>
    <p style="color:#888;font-size:13px;margin-top:0">{data_str} &nbsp;|&nbsp; {status}</p>
    <table style="max-width:700px">
      <tbody>{linhas}</tbody>
    </table>
    {'<p style="color:#c00;margin-top:16px;font-size:13px"><strong>Erro:</strong> ' + erro_s + '</p>' if erro and not aguardando else ''}
    {btn_preview}
    {btn_aprovar}
    {form_editar}
    {btn_reprocessar}
    <div style="margin-top:32px;padding-top:20px;border-top:1px solid #f0e6f6;">
      <a href="{voltar_busca}" class="btn" style="background:#6c757d;">📋 Ver histórico da paciente</a>
    </div>"""

    return _painel_html_base(token_recebido, conteudo), 200


@app.route('/painel/reprocessar/<int:job_id>', methods=['POST'])
def painel_reprocessar(job_id):
    """Reseta um plano para reprocessamento — zera tentativas e erro."""
    token_esperado = os.environ.get('PAINEL_TOKEN', '')
    token_recebido = request.args.get('token', '')
    if not token_esperado or token_recebido != token_esperado:
        return '<h2 style="font-family:sans-serif;color:#c00">Acesso negado.</h2>', 403

    conn = None
    try:
        conn = get_db()
        cur  = conn.cursor()
        # Reseta o plano principal
        cur.execute("""
            UPDATE planos_agendados
            SET processado            = FALSE,
                tentativas            = 0,
                proxima_tentativa     = NOW(),
                erro                  = NULL,
                processado_em         = NULL,
                aguardando_aprovacao  = FALSE,
                motivo_aprovacao      = NULL,
                pdf_base64            = NULL
            WHERE id = %s
        """, (job_id,))
        if cur.rowcount == 0:
            return '<h2 style="font-family:sans-serif;color:#c00">Plano não encontrado.</h2>', 404
        # Reseta imagens para que Vision re-processe (se bytes ainda existirem)
        cur.execute("""
            UPDATE exames_imagens
            SET processado = FALSE
            WHERE plano_id = %s
        """, (job_id,))
        conn.commit()
        cur.close()
        log.info(f"[REPROCESSAR] Plano {job_id} resetado via painel")
    except Exception as e:
        log.error(f"[REPROCESSAR] Erro ao resetar plano {job_id}: {e}")
        return '<h2 style="font-family:sans-serif;color:#c00">Erro interno ao reprocessar.</h2>', 500
    finally:
        if conn:
            conn.close()

    token_safe = urllib.parse.quote(token_recebido, safe='')
    return redirect(f"/painel/detalhes/{job_id}?token={token_safe}")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
