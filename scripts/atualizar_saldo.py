"""
Robô de saldo do Painel de Metas.

Lê o saldo das contas conectadas no Meu Pluggy (Open Finance) e grava no Firestore,
em users/{FIREBASE_UID}/finance/balances, que o painel mostra no card "Saldo nos bancos".

Roda no GitHub Actions (.github/workflows/saldo.yml). Todas as credenciais vêm de
GitHub Secrets. ATENÇÃO: o repositório é público e os logs das Actions também, por isso
este script NUNCA imprime saldos, nomes de contas ou identificadores.

Secrets necessários:
  PLUGGY_CLIENT_ID, PLUGGY_CLIENT_SECRET  -> app de desenvolvimento no dashboard.pluggy.ai
  PLUGGY_ITEM_IDS                         -> IDs das conexões (um por banco), separados por vírgula
  FIREBASE_SERVICE_ACCOUNT                -> conteúdo do JSON da conta de serviço do Firebase
  FIREBASE_UID                            -> seu ID de usuário no painel
"""
import calendar
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore

API = "https://api.pluggy.ai"


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Falta o secret {name}")
    return value


BANCOS = [("nu pagamentos", "Nubank"), ("nubank", "Nubank"), ("picpay", "PicPay"), ("santander", "Santander"),
          ("itau", "Itaú"), ("itaú", "Itaú"), ("bradesco", "Bradesco"), ("caixa", "Caixa"), ("banco do brasil", "Banco do Brasil"),
          ("inter", "Inter"), ("c6", "C6 Bank"), ("mercado pago", "Mercado Pago"), ("btg", "BTG"), ("xp ", "XP")]


def nome_banco(texto):
    """Nome curto do banco a partir do nome da conta (ex.: 'Nu Pagamentos S.A. - ...' -> 'Nubank')."""
    t = (texto or "").lower()
    for chave, nome in BANCOS:
        if chave in t:
            return nome
    return (texto or "").split(" - ")[0].replace(" S.A.", "").replace(" S/A", "").strip()


def nome_cartao(nome, bandeira, banco):
    """'PIC PAY MASTERCARD GOLD' + PicPay -> 'Mastercard Gold'; 'platinum' + MASTERCARD -> 'Mastercard Platinum'."""
    palavras = (nome or "").title().split()
    # tira o nome do banco do começo (ex.: 'Pic Pay', 'Santander')
    alvo = (banco or "").lower().replace(" ", "")
    for n in range(len(palavras), 0, -1):
        if alvo and "".join(palavras[:n]).lower() == alvo:
            palavras = palavras[n:]
            break
    band = (bandeira or "").title()
    if band and band.lower() not in [p.lower() for p in palavras]:
        palavras = [band] + palavras
    return " ".join(palavras) or "Cartão"


# ---------------------------------------------------------------- faturas
def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _data(txt):
    try:
        return date.fromisoformat(str(txt)[:10])
    except (TypeError, ValueError):
        return None


def vencimento_de(dia, fecha, vence):
    """Data de vencimento da fatura em que cai uma compra feita em `dia`."""
    base = date(dia.year, dia.month, 1)
    mes_fecha = base if dia.day <= fecha else add_months(base, 1)
    mes_vence = mes_fecha if vence > fecha else add_months(mes_fecha, 1)
    return date(mes_vence.year, mes_vence.month, min(vence, calendar.monthrange(mes_vence.year, mes_vence.month)[1]))


def listar_transacoes(acc_id, headers, desde):
    """Transações do cartão desde `desde` (API v2 com cursor; cai para a v1 se a v2 não existir)."""
    out, url, params = [], f"{API}/v2/transactions", {"accountId": acc_id, "dateFrom": desde}
    for _ in range(30):
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 404 and "/v2/" in url:
            return listar_transacoes_v1(acc_id, headers, desde)
        r.raise_for_status()
        j = r.json()
        out += j.get("results", [])
        nxt = j.get("next")
        if not nxt:
            break
        if str(nxt).startswith("http"):
            url, params = nxt, None
        else:
            params = {"accountId": acc_id, "dateFrom": desde, "after": nxt}
    return out


def listar_transacoes_v1(acc_id, headers, desde):
    out = []
    for page in range(1, 21):
        r = requests.get(f"{API}/transactions", params={"accountId": acc_id, "from": desde, "pageSize": 500, "page": page}, headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("results", [])
        if page >= (j.get("totalPages") or 1):
            break
    return out


def listar_faturas(acc_id, headers):
    try:
        r = requests.get(f"{API}/bills", params={"accountId": acc_id}, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])
    except requests.RequestException:
        return []


PAGTO = re.compile(r"pagamento|pgto|pag fat|payment", re.I)


def _chave_parcela(desc):
    d = re.sub(r"\b(parc(ela)?\.?|parcelado)\b", " ", desc or "", flags=re.I)
    d = re.sub(r"\b\d{1,2}\s*(/|de)\s*\d{1,2}\b", " ", d, flags=re.I)
    return re.sub(r"\s+", " ", d).strip().lower()


def faturas_cartao(acc, headers, hoje, meses=6):
    """Fatura fechada a vencer + próximas `meses` faturas (reais quando a Pluggy tem a fatura; senão estimadas)."""
    credit = acc.get("creditData") or {}
    due0, close0 = _data(credit.get("balanceDueDate")), _data(credit.get("balanceCloseDate"))
    vence = due0.day if due0 else 10
    fecha = close0.day if close0 else ((vence - 8) % 28) + 1

    bills = []
    for b in listar_faturas(acc["id"], headers):
        d = _data(b.get("dueDate"))
        if d:
            bills.append({"id": b.get("id"), "due": d, "total": float(b.get("totalAmount") or 0), "min": b.get("minimumPaymentAmount")})
    due_por_bill = {b["id"]: b["due"] for b in bills}
    bill_por_mes = {(b["due"].year, b["due"].month): b for b in bills}

    txs = listar_transacoes(acc["id"], headers, (hoje - timedelta(days=200)).isoformat())
    postado, compras = defaultdict(float), {}
    for t in txs:
        dia, valor = _data(t.get("date")), float(t.get("amount") or 0)
        if not dia or not valor:
            continue
        meta = t.get("creditCardMetadata") or {}
        venc = due_por_bill.get(meta.get("billId")) or vencimento_de(dia, fecha, vence)
        if valor < 0 and PAGTO.search(t.get("description") or ""):
            continue                      # pagamento de fatura anterior não abate a fatura atual
        postado[(venc.year, venc.month)] += valor
        n, tot = int(meta.get("installmentNumber") or 0), int(meta.get("totalInstallments") or 0)
        if valor > 0 and tot > 1 and 0 < n < tot:
            k = (_chave_parcela(t.get("description")), tot, round(valor, 2))
            if k not in compras or n > compras[k][0]:
                compras[k] = (n, venc, valor)
    futuro = defaultdict(float)           # parcelas que ainda vão cair (compras já feitas)
    for (_, tot, _), (n, venc, valor) in compras.items():
        for i in range(1, tot - n + 1):
            m = add_months(venc, i)
            futuro[(m.year, m.month)] += valor

    # primeira fatura a mostrar: a fechada ainda não vencida, ou a aberta
    abertas = sorted(b["due"] for b in bills if b["due"] >= hoje)
    inicio = abertas[0] if abertas else vencimento_de(hoje, fecha, vence)
    previsao = []
    for i in range(meses):
        m = add_months(date(inicio.year, inicio.month, 1), i)
        chave = (m.year, m.month)
        dia = date(m.year, m.month, min(vence, calendar.monthrange(m.year, m.month)[1]))
        if chave in bill_por_mes:
            b = bill_por_mes[chave]
            previsao.append({"due": b["due"].isoformat(), "amount": round(b["total"], 2), "real": True, "min": b["min"], "parcelas": 0})
        else:
            p, f = postado.get(chave, 0.0), futuro.get(chave, 0.0)
            previsao.append({"due": dia.isoformat(), "amount": round(max(p + f, 0), 2), "real": False, "parcelas": round(f, 2)})
    historico = [{"due": b["due"].isoformat(), "amount": round(b["total"], 2)} for b in sorted(bills, key=lambda x: x["due"]) if b["due"] < hoje][-4:]
    return previsao, historico


def main():
    # 1) Autentica na Pluggy
    r = requests.post(f"{API}/auth", json={"clientId": env("PLUGGY_CLIENT_ID"),
                                           "clientSecret": env("PLUGGY_CLIENT_SECRET")}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Falha ao autenticar na Pluggy (HTTP {r.status_code})")
    headers = {"X-API-KEY": r.json()["apiKey"]}

    # 2) Lê as contas de cada conexão (item)
    accounts, falhas = [], 0
    for item_id in [i.strip() for i in env("PLUGGY_ITEM_IDS").split(",") if i.strip()]:
        try:
            item = requests.get(f"{API}/items/{item_id}", headers=headers, timeout=30)
            item.raise_for_status()
            item = item.json()
            connector = (item.get("connector") or {}).get("name", "")
            res = requests.get(f"{API}/accounts", params={"itemId": item_id}, headers=headers, timeout=30)
            res.raise_for_status()
            contas = res.json().get("results", [])
            # Via MeuPluggy o conector se chama "MeuPluggy"; o banco real vem no nome da conta corrente
            banco_ref = next((a.get("name") for a in contas if a.get("type") == "BANK"), "")
            institution = nome_banco(banco_ref) if (banco_ref and connector.lower() == "meupluggy") else (connector or nome_banco(banco_ref))
            for acc in contas:
                credit = acc.get("creditData") or {}
                if acc.get("type") == "CREDIT":
                    nome = nome_cartao(acc.get("name"), credit.get("brand"), institution)
                else:
                    nome = "Conta corrente" if acc.get("subtype") == "CHECKING_ACCOUNT" else ("Poupança" if acc.get("subtype") == "SAVINGS_ACCOUNT" else (acc.get("name") or "Conta"))
                previsao, historico = [], []
                if acc.get("type") == "CREDIT":
                    try:
                        previsao, historico = faturas_cartao(acc, headers, date.today())
                    except Exception as e:  # faturas são extra: se falhar, o saldo continua sendo gravado
                        print(f"Aviso: faturas de um cartão não puderam ser calculadas ({type(e).__name__})")
                accounts.append({
                    "forecast": previsao,
                    "billsHist": historico,
                    "institution": institution,
                    "name": nome,
                    "type": acc.get("type", ""),          # BANK ou CREDIT
                    "subtype": acc.get("subtype", ""),
                    "balance": acc.get("balance") or 0,
                    "currency": acc.get("currencyCode", "BRL"),
                    "creditLimit": credit.get("creditLimit"),
                    "availableCreditLimit": credit.get("availableCreditLimit"),
                    "dueDate": (credit.get("balanceDueDate") or "")[:10],
                })
        except requests.RequestException as e:
            falhas += 1
            code = getattr(getattr(e, "response", None), "status_code", "?")
            print(f"Aviso: uma conexão falhou (HTTP {code})")

    if not accounts:
        sys.exit("Nenhuma conta lida; nada foi gravado")

    # 3) Grava no Firestore (Admin SDK)
    cred = credentials.Certificate(json.loads(env("FIREBASE_SERVICE_ACCOUNT")))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    now = datetime.now(timezone.utc)
    # o UID do Firebase só tem letras/números: remove espaços, pontos ou aspas colados por engano
    uid = re.sub(r"[^A-Za-z0-9]", "", env("FIREBASE_UID"))
    col = db.collection("users").document(uid).collection("finance")
    payload = {"updatedAt": now.isoformat(), "accounts": accounts}
    col.document("balances").set(payload)
    # histórico diário (um documento por dia) para gráficos futuros
    col.document("hist-" + now.strftime("%Y-%m-%d")).set(payload)

    nf = sum(len(a["forecast"]) for a in accounts)
    print(f"OK: {len(accounts)} contas atualizadas, {nf} faturas previstas, {falhas} conexões com falha")


if __name__ == "__main__":
    main()
