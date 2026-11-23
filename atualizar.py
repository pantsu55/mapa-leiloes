#!/usr/bin/env python3
"""Baixa os CSVs de imóveis da Caixa (SP, PR, SC, RS), regenera os dados
embutidos no index.html e dá push no repositório.

Uso:
    python atualizar.py              # baixa, atualiza, commit e push
    python atualizar.py --no-push    # atualiza o index.html mas não faz commit/push
    python atualizar.py --dry-run    # só baixa e valida, não altera nada

Segurança: se algum download falhar ou o total de imóveis cair muito em relação
ao arquivo atual, o script aborta sem alterar o index.html.
"""
import argparse
import csv
import io
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
INDEX = AQUI / "index.html"
LOG = AQUI / "atualizar.log"
UFS = ("SP", "PR", "SC", "RS")
URL = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{uf}.csv"
MIN_FRACAO = 0.5  # aborta se o novo total for < 50% do total atual


def log(msg):
    linha = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(linha, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def baixar(uf, tentativas=3):
    req = urllib.request.Request(URL.format(uf=uf), headers={"User-Agent": "Mozilla/5.0"})
    for t in range(1, tentativas + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                dados = r.read()
            if len(dados) < 5000 or b"<html" in dados[:500].lower():
                raise ValueError(f"resposta inesperada ({len(dados)} bytes)")
            return dados.decode("latin-1")
        except Exception as e:  # noqa: BLE001
            log(f"{uf}: tentativa {t}/{tentativas} falhou: {e}")
            if t < tentativas:
                time.sleep(5 * t)
    raise RuntimeError(f"não consegui baixar {uf}")


def num_br(s):
    """'136.584,86' -> 136584.86"""
    return float(s.strip().replace(".", "").replace(",", "."))


def achar(pat, desc, conv=float, padrao=0):
    m = re.search(pat, desc)
    return conv(m.group(1)) if m else padrao


def parse_csv(texto):
    """Retorna (data_geracao 'dd/mm/aaaa' ou None, lista de linhas no formato RAW)."""
    data = None
    m = re.search(r"Data de gera[^;]*;\s*(\d{2}/\d{2}/\d{4})", texto)
    if m:
        data = m.group(1)
    saida = []
    for r in csv.reader(io.StringIO(texto), delimiter=";"):
        if len(r) < 12:
            continue
        r = [c.strip() for c in r]
        if not r[0].isdigit():  # pula título, cabeçalho e linhas em branco
            continue
        desc = r[9]
        saida.append([
            r[0],                                   # id
            r[1],                                   # uf
            r[2],                                   # cidade
            r[3],                                   # bairro
            re.sub(r"\s+", " ", r[4]).rstrip(" ,"),  # endereço (sem espaços duplos nem vírgula final)
            num_br(r[5]),                           # preço
            num_br(r[6]),                           # avaliação
            float(r[7].replace(",", ".")),          # desconto %
            1 if r[8].lower() == "sim" else 0,      # financiamento
            desc.split(",")[0].strip(),             # tipo
            achar(r"([\d.]+) de .rea privativa", desc),
            achar(r"([\d.]+) de .rea total", desc),
            achar(r"([\d.]+) de .rea do terreno", desc),
            achar(r"(\d+) qto\(s\)", desc, int),
            achar(r"(\d+) vaga\(s\)", desc, int),
            r[10],                                  # modalidade
            r[11],                                  # link
        ])
    return data, saida


MUN_URL = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
COD_UF = {"35": "SP", "41": "PR", "42": "SC", "43": "RS"}
NOMINATIM = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=br&q="
UFN = {"SP": "São Paulo", "PR": "Paraná", "SC": "Santa Catarina", "RS": "Rio Grande do Sul"}
CIDADES = AQUI / "cidades.json"


def norm(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", s.upper()).strip()


def coords_municipios():
    """{(uf, nome normalizado): [lat, lon]} a partir da base de municípios (IBGE)."""
    req = urllib.request.Request(MUN_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        texto = r.read().decode("utf-8")
    saida = {}
    for r in csv.DictReader(io.StringIO(texto)):
        uf = COD_UF.get(r["codigo_uf"])
        if uf:
            saida[(uf, norm(r["nome"]))] = [round(float(r["latitude"]), 4), round(float(r["longitude"]), 4)]
    return saida


def nominatim(uf, cidade):
    q = f"{cidade.title()}, {UFN[uf]}, Brasil"
    req = urllib.request.Request(NOMINATIM + urllib.parse.quote(q), headers={"User-Agent": "mapa-leiloes-atualizador"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
        return [round(float(j[0]["lat"]), 4), round(float(j[0]["lon"]), 4)] if j else None
    except Exception as e:  # noqa: BLE001
        log(f"Nominatim falhou para {cidade}/{uf}: {e}")
        return None


def resolver_cidades(linhas):
    """Garante coordenadas de todas as cidades. Cache em cidades.json (só busca as novas)."""
    cache = json.loads(CIDADES.read_text(encoding="utf-8")) if CIDADES.exists() else {}
    chaves = {f"{l[1]}|{l[2]}": (l[1], l[2]) for l in linhas}
    faltam = [k for k in chaves if k not in cache]
    if faltam:
        log(f"{len(faltam)} cidade(s) sem coordenadas, buscando")
        try:
            mun = coords_municipios()
        except Exception as e:  # noqa: BLE001
            log(f"aviso: base de municípios indisponível: {e}")
            mun = {}
        for k in faltam:
            uf, cid = chaves[k]
            pos = mun.get((uf, norm(cid)))
            if not pos:
                time.sleep(1.1)
                pos = nominatim(uf, cid)
            if pos:
                cache[k] = pos
            else:
                log(f"aviso: sem coordenadas para {cid}/{uf} (o mapa tenta localizar no navegador)")
        CIDADES.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=0) + "\n", encoding="utf-8")
    return {k: cache[k] for k in sorted(chaves) if k in cache}


def total_atual(html):
    m = re.search(r"const RAW = (\[.*\]);", html)
    return len(json.loads(m.group(1))) if m else 0


def trocar_linha(html, prefixo, novo, depois_de=None):
    """Troca a linha que começa com `prefixo`; se não existir, insere após a linha `depois_de`."""
    ini = html.find(prefixo)
    if ini < 0:
        a = html.index(depois_de)
        fim = html.index("\n", a)
        quebra = "\r\n" if html[fim - 1] == "\r" else "\n"
        if quebra == "\r\n":
            fim -= 1
        return html[:fim] + quebra + novo + html[fim:]
    fim = html.index("\n", ini)
    if html[fim - 1] == "\r":
        fim -= 1
    return html[:ini] + novo + html[fim:]


def gerar_html(html, linhas, data, cidades):
    def js(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    html = trocar_linha(html, "const RAW = ", "const RAW = " + js(linhas) + ";")
    html = trocar_linha(html, "const CITY_SEED = ", "const CITY_SEED = " + js(cidades) + ";", depois_de="const RAW = ")
    cab, n = re.subn(
        r"Dados da Caixa de [^·<]*· [\d.]+ imóveis",
        f"Dados da Caixa de {data} · {len(linhas):,} imóveis".replace(",", "."),
        html,
        count=1,
    )
    if n != 1:
        raise RuntimeError("não achei o texto 'Dados da Caixa de ... imóveis' no cabeçalho")
    return cab


def achar_git():
    g = shutil.which("git")
    if g:
        return g
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if Path(p).exists():
            return p
    raise RuntimeError("git não encontrado")


def git(*args):
    r = subprocess.run([achar_git(), *args], cwd=AQUI, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="atualiza o arquivo mas não faz commit/push")
    ap.add_argument("--dry-run", action="store_true", help="só baixa e valida")
    a = ap.parse_args()

    todas, datas, vistos = [], set(), set()
    for uf in UFS:
        data, linhas = parse_csv(baixar(uf))
        if not linhas:
            raise RuntimeError(f"{uf}: nenhum imóvel no CSV")
        if data:
            datas.add(data)
        for l in linhas:
            if l[0] not in vistos:  # id é a chave dos marcadores no mapa
                vistos.add(l[0])
                todas.append(l)
        log(f"{uf}: {len(linhas)} imóveis")
    todas.sort(key=lambda l: (l[1], l[2]))  # ordem estável por UF e cidade

    data = max(datas, key=lambda d: datetime.strptime(d, "%d/%m/%Y")) if datas else f"{datetime.now():%d/%m/%Y}"
    html = INDEX.read_text(encoding="utf-8", newline="")
    antes = total_atual(html)
    if antes and len(todas) < antes * MIN_FRACAO:
        raise RuntimeError(f"total caiu de {antes} para {len(todas)}; abortando por segurança")
    log(f"total: {len(todas)} imóveis (antes: {antes}), data da Caixa: {data}")

    if a.dry_run:
        log("dry-run: nada alterado")
        return

    novo = gerar_html(html, todas, data, resolver_cidades(todas))
    if novo == html:
        log("sem mudanças no index.html")
        return
    INDEX.write_text(novo, encoding="utf-8", newline="")
    log("index.html atualizado")

    if a.no_push:
        return
    git("add", "index.html", "cidades.json")
    git("commit", "-m", f"Atualiza dados da Caixa ({data}, {len(todas)} imóveis)")
    try:
        git("pull", "--rebase", "--autostash")
    except RuntimeError as e:
        log(f"aviso no pull: {e}")
    git("push")
    log("push feito")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ERRO: {e}")
        sys.exit(1)
