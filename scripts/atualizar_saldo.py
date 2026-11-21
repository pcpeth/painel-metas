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
import json
import os
import re
import sys
from datetime import datetime, timezone

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
                    nome = " ".join(x for x in [credit.get("brand", "").title(), (acc.get("name") or "").title()] if x)
                else:
                    nome = "Conta corrente" if acc.get("subtype") == "CHECKING_ACCOUNT" else ("Poupança" if acc.get("subtype") == "SAVINGS_ACCOUNT" else (acc.get("name") or "Conta"))
                accounts.append({
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

    print(f"OK: {len(accounts)} contas atualizadas, {falhas} conexões com falha")


if __name__ == "__main__":
    main()
