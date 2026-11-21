"""
Resumo diário do Painel de Metas por e-mail (chamado pelo robô de saldo).

Envia pelo Gmail (SMTP) usando uma senha de app. Secrets opcionais:
  GMAIL_USER          -> seu endereço Gmail (remetente e destinatário)
  GMAIL_APP_PASSWORD  -> senha de app de 16 letras (Conta Google > Segurança > Senhas de app)
  EMAIL_TO            -> (opcional) outro destinatário
Sem esses secrets o resumo é simplesmente pulado. Nada de valores é impresso no log.
"""
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BRT = timezone(timedelta(hours=-3))
APP_URL = "https://pcpeth.github.io/painel-metas/"


# ------------------------------------------------------------ feriados (igual ao app)
def _pascoa(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, mes, dia)


def feriados(y):
    p = _pascoa(y)
    fixos = [(1, 1), (4, 21), (5, 1), (7, 9), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)]
    return {date(y, m, d) for m, d in fixos} | {p - timedelta(days=2)}


def folga(d):
    return d.weekday() >= 5 or d in feriados(d.year)


def _ultimo_dia(y, m):
    return (date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)).day


def data_entrada(it, y, m):
    ult = _ultimo_dia(y, m)
    if it.get("rule") == "bizday":
        n = 0
        for d in range(1, ult + 1):
            x = date(y, m, d)
            if not folga(x):
                n += 1
                if n == int(it.get("day") or 1):
                    return x
        return date(y, m, ult)
    x = date(y, m, min(int(it.get("day") or 1), ult))
    for _ in range(7):
        if not folga(x):
            break
        x -= timedelta(days=1)
    return x


def entradas(itens, ini, fim):
    out, y, m = [], ini.year, ini.month
    while (y, m) <= (fim.year, fim.month):
        for it in itens:
            if float(it.get("amount") or 0) > 0:
                d = data_entrada(it, y, m)
                if ini <= d <= fim:
                    out.append((d, it.get("name") or "Entrada", float(it["amount"])))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out)


def brl(v):
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-R$ " if v < 0 else "R$ ") + s


def fdata(d):
    return d.strftime("%d/%m")


# ------------------------------------------------------------ cálculo (mesmo do app)
def livre(accounts, itens, goals, hoje):
    ligadas = {(g.get("auto") or "")[4:] for g in goals if g.get("type") == "numeric" and (g.get("auto") or "").startswith("acc:")}
    saldo = sum(float(a.get("balance") or 0) for a in accounts
                if a.get("type") != "CREDIT" and f"{a.get('institution')}|{a.get('name')}" not in ligadas)
    fim = hoje + timedelta(days=45)
    ev = [(d, 0, v, "💰 " + n) for d, n, v in entradas(itens, hoje + timedelta(days=1), fim)]
    for a in accounts:
        if a.get("type") != "CREDIT":
            continue
        for f in a.get("forecast") or []:
            d = date.fromisoformat(f["due"])
            if hoje <= d <= fim:
                ev.append((d, 1, -float(f.get("amount") or 0), "💳 " + (a.get("institution") or "")))
    ev.sort(key=lambda e: (e[0], e[1]))
    bal, minimo, quando = saldo, saldo, None
    for d, k, v, tx in ev:
        bal += v
        if bal < minimo:
            minimo, quando = bal, (d, tx)
    prox = next((e for e in entradas(itens, hoje + timedelta(days=1), hoje + timedelta(days=62))), None)
    dias = max(1, (prox[0] - hoje).days) if prox else 30
    reserva = 0.0
    for g in goals:
        n = g.get("numeric") or {}
        reserva_meta = (g.get("auto") or "").startswith("acc:") or "reserva" in (g.get("title") or "").lower()
        if g.get("area") == "dinheiro" and g.get("type") == "numeric" and reserva_meta and g.get("deadline"):
            falta = float(n.get("target") or 0) - float(n.get("current") or 0)
            meses = max(1.0, (date.fromisoformat(g["deadline"]) - hoje).days / 30.4)
            reserva += max(0.0, falta) / meses * dias / 30.4
    liv = minimo - reserva
    return {"saldo": saldo, "minimo": minimo, "quando": quando, "prox": prox, "dias": dias,
            "reserva": reserva, "livre": liv, "porDia": max(0.0, liv) / dias}


def _streak(checks, hoje):
    s, d = 0, hoje if hoje.isoformat() in checks else hoje - timedelta(days=1)
    while d.isoformat() in checks:
        s += 1
        d -= timedelta(days=1)
    return s


def montar(accounts, gastos, goals, actions, itens, hoje):
    L = livre(accounts, itens, goals, hoje)
    linhas = []
    cor = "#2fbf71" if L["livre"] >= 0 else "#ef5b5b"
    topo = (f"<div style='font-size:13px;color:#8a94a8'>Pode gastar hoje</div>"
            f"<div style='font-size:30px;font-weight:700;color:{cor}'>{brl(L['porDia'])}<span style='font-size:14px;color:#8a94a8'>/dia</span></div>"
            f"<div style='font-size:13px;color:#8a94a8'>{brl(L['livre'])} livres"
            + (f" até {L['prox'][1]} em {fdata(L['prox'][0])} ({L['dias']} dias)" if L["prox"] else "")
            + (f" · já separando {brl(L['reserva'])} para a reserva" if L["reserva"] > 0 else "") + "</div>")
    if L["minimo"] < 0 and L["quando"]:
        linhas.append(f"⚠️ Aperto em <b>{fdata(L['quando'][0])}</b> ({L['quando'][1]}): saldo projetado <b style='color:#ef5b5b'>{brl(L['minimo'])}</b>.")
    for a in accounts:
        if a.get("type") != "CREDIT":
            continue
        nb = next((f for f in a.get("forecast") or [] if date.fromisoformat(f["due"]) >= hoje), None)
        if nb:
            dd = (date.fromisoformat(nb["due"]) - hoje).days
            if dd <= 7:
                linhas.append(f"💳 Fatura {a.get('institution')} {a.get('name')} vence {'hoje' if dd == 0 else f'em {dd} dia(s)'}: <b>{brl(float(nb['amount']))}</b>{'' if nb.get('real') else ' (estimada)'}.")
    t = hoje.isoformat()
    hoje_acts = [x for x in actions if x.get("date") == t and not x.get("done")]
    atrasadas = [x for x in actions if (x.get("date") or "") < t and not x.get("done")]
    if hoje_acts:
        linhas.append("📅 Hoje: " + ", ".join(((x.get("time") + " ") if x.get("time") else "") + (x.get("title") or "") for x in hoje_acts[:5]))
    if atrasadas:
        linhas.append(f"⌛ {len(atrasadas)} ação(ões) atrasada(s): " + ", ".join(x.get("title") or "" for x in atrasadas[:3]))
    habs = []
    for g in goals:
        if g.get("type") != "habit":
            continue
        h = g.get("habit") or {}
        ch = set(h.get("checks") or [])
        ontem = (hoje - timedelta(days=1)).isoformat() in ch
        if h.get("freq") == "weekly":
            ini = hoje - timedelta(days=hoje.weekday())
            n = sum(1 for i in range(7) if (ini + timedelta(days=i)).isoformat() in ch)
            habs.append(f"{g.get('title')}: {n}/{h.get('perWeek')} na semana")
        else:
            habs.append(f"{g.get('title')}: {'✓ ontem' if ontem else '✗ ontem'} · 🔥 {_streak(ch, hoje)}")
    for g in goals:
        if g.get("deadline") and t <= g["deadline"] <= (hoje + timedelta(days=7)).isoformat():
            linhas.append(f"🚩 Prazo em {fdata(date.fromisoformat(g['deadline']))}: {g.get('title')}")
    # gastos do mês x média
    meses = gastos.get("months") or []
    gasto_txt = ""
    if len(meses) >= 2:
        atual = meses[-1]["total"]
        ant = [m["total"] for m in meses[:-1] if m["total"] > 0]
        if ant:
            media = sum(ant) / len(ant)
            ritmo = atual / max(1, hoje.day) * _ultimo_dia(hoje.year, hoje.month)
            top = list((meses[-1].get("byCat") or {}).items())[:3]
            gasto_txt = (f"Gastos no mês: <b>{brl(atual)}</b> · no ritmo atual fecha em <b>{brl(ritmo)}</b> (média {brl(media)})."
                         + (("<br>Maiores: " + ", ".join(f"{c} {brl(v)}" for c, v in top)) if top else ""))
    rec = [r for r in gastos.get("recurring") or [] if r.get("proxima") and t <= r["proxima"] <= (hoje + timedelta(days=3)).isoformat()]
    if rec:
        linhas.append("🔁 Cobranças recorrentes nos próximos dias: " + ", ".join(f"{r['desc']} ~{brl(r['media'])}" for r in rec[:4]))

    sec = lambda titulo, corpo: (f"<h3 style='font-size:14px;margin:18px 0 6px;color:#c7cede'>{titulo}</h3>"
                                 f"<div style='font-size:14px;line-height:1.6'>{corpo}</div>") if corpo else ""
    html = (f"<div style='font-family:Inter,Arial,sans-serif;background:#0b1020;color:#e6e9f2;padding:22px;border-radius:14px;max-width:560px'>"
            f"<div style='font-size:13px;color:#8a94a8'>{hoje.strftime('%d/%m/%Y')} · Bonjour, Henan !</div>{topo}"
            + sec("Atenção", "<br>".join(linhas)) + sec("Dinheiro do mês", gasto_txt) + sec("Hábitos", "<br>".join(habs))
            + f"<div style='margin-top:20px'><a href='{APP_URL}' style='background:#6d8bff;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;font-weight:600'>Abrir o painel</a></div>"
            "<div style='font-size:11px;color:#8a94a8;margin-top:14px'>Valores estimados a partir do Open Finance; faturas futuras incluem só o que já foi lançado.</div></div>")
    assunto = f"🎯 Hoje: {brl(L['porDia'])}/dia livre" + (" · ⚠️ aperto à vista" if L["minimo"] < 0 else "")
    return assunto, html


def talvez_enviar(db, uid, accounts, gastos, agora):
    user, senha = os.environ.get("GMAIL_USER", "").strip(), os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not user or not senha:
        return "sem secrets (pulado)"
    forcar = os.environ.get("RESUMO_FORCAR", "") in ("1", "true")
    local = agora.astimezone(BRT)
    if not forcar and local.hour >= 12:
        return "fora do horário (só de manhã)"
    hoje = local.date()
    base = db.collection("users").document(uid)
    log = base.collection("finance").document("emailLog")
    if not forcar:
        snap = log.get()
        if snap.exists and (snap.to_dict() or {}).get("lastSent") == hoje.isoformat():
            return "já enviado hoje"
    goals = [d.to_dict() for d in base.collection("goals").stream()]
    actions = [d.to_dict() for d in base.collection("actions").stream()]
    inc = base.collection("finance").document("incomes").get()
    itens = (inc.to_dict() or {}).get("items", []) if inc.exists else []
    assunto, html = montar(accounts, gastos, goals, actions, itens, hoje)
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = assunto, f"Painel de Metas <{user}>", os.environ.get("EMAIL_TO", "").strip() or user
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, senha)
        s.send_message(msg)
    log.set({"lastSent": hoje.isoformat(), "at": agora.isoformat()})
    return "enviado"
