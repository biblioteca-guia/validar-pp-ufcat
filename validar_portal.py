#!/usr/bin/env python3
"""
Validacao da migracao do Portal de Periodicos da UFCAT (OJS)

O que faz:
  1. Lista as revistas e coleta todos os itens publicados, via OAI-PMH,
     no portal antigo e no novo.
  2. Compara os dois: itens faltando, sobrando ou com titulo diferente.
  3. Testa se cada arquivo (PDF) abre no portal novo.
  4. Para cada arquivo com problema, confere se o problema ja existia
     no portal antigo (ou seja, se nao foi causado pela migracao).

Como usar (Windows, Mac ou Linux, com Python 3 instalado):
  pip install requests
  python validar_portal.py

Opcoes:
  --apenas-novo   testa so o portal novo (use depois da troca do DNS)
  --revista ra    testa so uma revista (pelo codigo: ra, cadis, Opsis...)
  --pausa 1.5     segundos entre um acesso e outro (padrao: 1.0)
  --recoletar     ignora a lista de itens ja salva e coleta de novo
  --limite 20     testa so os primeiros N arquivos (para um teste rapido)

Se for interrompido, rode de novo: ele continua de onde parou.
Os relatorios ficam na pasta "validacao_portal".

IMPORTANTE: a comparacao com o portal antigo so faz sentido ANTES da troca
do DNS. Depois da troca, o endereco antigo passa a apontar para o servidor
novo. Nesse caso, use --apenas-novo.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca requests. Instale com: pip install requests")

ANTIGO = "https://periodicos.ufcat.edu.br"
NOVO = "https://ufcat.ojsbr.com"
PASTA = "validacao_portal"
UA = "Mozilla/5.0 (validacao migracao Portal de Periodicos UFCAT)"
NS = {
    "o": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

SESSAO = requests.Session()
SESSAO.headers["User-Agent"] = UA
PAUSA = 1.0


def acessar(url, params=None, headers=None, stream=False):
    """GET com novas tentativas em caso de erro de conexao ou excesso de acessos (429)."""
    falhas = 0
    while True:
        try:
            r = SESSAO.get(url, params=params, headers=headers, timeout=120, stream=stream)
        except requests.RequestException as e:
            falhas += 1
            if falhas >= 5:
                raise
            print(f"    erro de conexao ({e.__class__.__name__}), nova tentativa em 20s...")
            time.sleep(20)
            continue
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "")
            espera = int(ra) if ra.isdigit() else 60
            r.close()
            print(f"    o servidor pediu uma pausa, aguardando {espera}s...")
            time.sleep(espera)
            continue
        return r


def oai(base, params):
    r = acessar(f"{base}/index.php/index/oai", params=params)
    r.raise_for_status()
    texto = r.content.decode("utf-8", "replace")
    texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto)  # caracteres invalidos no XML
    return ET.fromstring(texto.encode("utf-8"))


def token(root):
    t = root.find(".//o:resumptionToken", NS)
    return t.text.strip() if t is not None and (t.text or "").strip() else None


def listar_revistas(base):
    revistas, params = [], {"verb": "ListSets"}
    while True:
        root = oai(base, params)
        for s in root.iter(f"{{{NS['o']}}}set"):
            spec = s.find("o:setSpec", NS).text
            nome = s.find("o:setName", NS).text
            if ":" not in spec and spec != "driver":
                revistas.append((spec, nome))
        t = token(root)
        if not t:
            return revistas
        params = {"verb": "ListSets", "resumptionToken": t}
        time.sleep(PAUSA)


def coletar_itens(base, spec):
    itens, params = {}, {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": spec}
    while True:
        root = oai(base, params)
        for rec in root.iter(f"{{{NS['o']}}}record"):
            h = rec.find("o:header", NS)
            if h.get("status") == "deleted":
                continue
            md = rec.find("o:metadata/oai_dc:dc", NS)
            if md is None:
                continue
            t = md.find("dc:title", NS)
            urls = [e.text for e in md.findall("dc:identifier", NS) if e.text and e.text.startswith("http")]
            itens[h.find("o:identifier", NS).text] = {
                "titulo": (t.text or "").strip() if t is not None else "",
                "url": urls[0] if urls else "",
                "arquivos": [e.text for e in md.findall("dc:relation", NS) if e.text and "/article/view/" in e.text],
            }
        t = token(root)
        if not t:
            return itens
        params = {"verb": "ListRecords", "resumptionToken": t}
        time.sleep(PAUSA)


def carregar_ou_coletar(rotulo, base, revistas, recoletar):
    arq = os.path.join(PASTA, f"itens_{rotulo}.json")
    dados = {}
    if os.path.exists(arq) and not recoletar:
        dados = json.load(open(arq, encoding="utf-8"))
    for spec, nome in revistas:
        if spec in dados:
            continue
        print(f"  {rotulo}: coletando {nome}...")
        dados[spec] = coletar_itens(base, spec)
        json.dump(dados, open(arq, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"    {len(dados[spec])} itens")
    return dados


def checar_arquivo(url_view):
    """Baixa so o inicio do arquivo e classifica."""
    url = url_view.replace("/article/view/", "/article/download/")
    r = acessar(url, headers={"Range": "bytes=0-1023"}, stream=True)
    try:
        inicio = r.raw.read(1024, decode_content=True) or b""
    finally:
        r.close()
    tipo = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if r.status_code == 404:
        return "NAO ENCONTRADO", tipo
    if r.status_code not in (200, 206):
        return f"ERRO HTTP {r.status_code}", tipo
    if b"%PDF-" in inicio:
        return "OK", tipo
    if "pdf" in tipo or tipo in ("application/octet-stream", "application/force-download", "application/save"):
        return "VAZIO OU CORROMPIDO", tipo
    return "OUTRO FORMATO (conferir)", tipo


def salvar_csv(nome, cabecalho, linhas):
    caminho = os.path.join(PASTA, nome)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        w.writerows(linhas)
    return caminho


def main():
    global PAUSA
    ap = argparse.ArgumentParser(description="Validacao da migracao do Portal de Periodicos da UFCAT")
    ap.add_argument("--apenas-novo", action="store_true")
    ap.add_argument("--revista")
    ap.add_argument("--pausa", type=float, default=1.0)
    ap.add_argument("--recoletar", action="store_true")
    ap.add_argument("--limite", type=int)
    a = ap.parse_args()
    PAUSA = a.pausa
    os.makedirs(PASTA, exist_ok=True)

    print("1/4 Listando revistas...")
    revistas = listar_revistas(NOVO)
    if a.revista:
        revistas = [r for r in revistas if r[0].lower() == a.revista.lower()]
        if not revistas:
            sys.exit(f"Revista '{a.revista}' nao encontrada.")
    nomes = dict(revistas)
    print("   " + ", ".join(n for _, n in revistas))

    print("2/4 Coletando itens publicados...")
    novo = carregar_ou_coletar("novo", NOVO, revistas, a.recoletar)
    antigo = {} if a.apenas_novo else carregar_ou_coletar("antigo", ANTIGO, revistas, a.recoletar)

    linhas_itens, resumo = [], []
    for spec, nome in revistas:
        n = novo.get(spec, {})
        if a.apenas_novo:
            resumo.append(f"   {nome}: {len(n)} itens no novo")
            continue
        o = antigo.get(spec, {})
        faltando = set(o) - set(n)
        sobrando = set(n) - set(o)
        titulo = [k for k in set(o) & set(n) if o[k]["titulo"] != n[k]["titulo"]]
        for k in sorted(faltando):
            linhas_itens.append([nome, "FALTANDO NO NOVO", k, o[k]["titulo"], o[k]["url"]])
        for k in sorted(sobrando):
            linhas_itens.append([nome, "SOBRANDO NO NOVO", k, n[k]["titulo"], n[k]["url"]])
        for k in sorted(titulo):
            linhas_itens.append([nome, "TITULO DIFERENTE", k, n[k]["titulo"], n[k]["url"]])
        resumo.append(f"   {nome}: antigo={len(o)} novo={len(n)} faltando={len(faltando)} "
                      f"sobrando={len(sobrando)} titulo_diferente={len(titulo)}")
    print("\n".join(resumo))
    if not a.apenas_novo:
        c = salvar_csv("relatorio_itens.csv", ["Revista", "Situacao", "Identificador OAI", "Titulo", "URL"], linhas_itens)
        print(f"   Diferencas de itens: {len(linhas_itens)} (detalhes em {c})")

    print("3/4 Testando os arquivos no portal novo...")
    fila = []
    for spec, _ in revistas:
        for item in novo.get(spec, {}).values():
            for arq in item["arquivos"]:
                fila.append((spec, item["titulo"], item["url"], arq))
    if a.limite:
        fila = fila[: a.limite]
    prog = os.path.join(PASTA, "progresso_arquivos.jsonl")
    feitos = {}
    if os.path.exists(prog):
        for l in open(prog, encoding="utf-8"):
            x = json.loads(l)
            feitos[x["arquivo"]] = x
    pendentes = [f for f in fila if f[3] not in feitos]
    print(f"   {len(fila)} arquivos, {len(pendentes)} a testar "
          f"(estimativa: {len(pendentes) * (PAUSA + 0.3) / 60:.0f} min)")
    inicio_t = time.time()
    with open(prog, "a", encoding="utf-8") as out:
        for i, (spec, titulo, url, arq) in enumerate(pendentes, 1):
            sit, tipo = checar_arquivo(arq)
            x = {"revista": spec, "titulo": titulo, "url": url, "arquivo": arq, "novo": sit, "tipo": tipo}
            out.write(json.dumps(x, ensure_ascii=False) + "\n")
            out.flush()
            feitos[arq] = x
            if i % 25 == 0 or i == len(pendentes):
                resto = (time.time() - inicio_t) / i * (len(pendentes) - i) / 60
                print(f"   {i}/{len(pendentes)} testados, faltam cerca de {resto:.0f} min")
            time.sleep(PAUSA)

    resultados = [feitos[f[3]] for f in fila if f[3] in feitos]
    problemas = [x for x in resultados if x["novo"] != "OK"]

    if problemas and not a.apenas_novo:
        print(f"4/4 Conferindo {len(problemas)} arquivos com problema no portal antigo...")
        for x in problemas:
            if "antigo" not in x:
                sit, _ = checar_arquivo(x["arquivo"].replace(NOVO, ANTIGO))
                x["antigo"] = sit
                time.sleep(PAUSA)
    else:
        print("4/4 Comparacao com o portal antigo: nao se aplica.")

    linhas = []
    for x in problemas:
        ant = x.get("antigo", "")
        if not ant:
            origem = ""
        elif ant == x["novo"]:
            origem = "Problema antigo (ja existia)"
        elif ant == "OK":
            origem = "ATENCAO: funcionava no antigo"
        else:
            origem = f"Diferente no antigo: {ant}"
        linhas.append([nomes.get(x["revista"], x["revista"]), x["titulo"], x["url"], x["arquivo"], x["novo"], x["tipo"], origem])
    c = salvar_csv("relatorio_arquivos.csv",
                   ["Revista", "Titulo", "Pagina do artigo", "Arquivo", "Situacao no novo", "Tipo informado", "Origem do problema"],
                   linhas)

    ok = sum(1 for x in resultados if x["novo"] == "OK")
    causados = sum(1 for l in linhas if l[6].startswith("ATENCAO"))
    print("\nRESUMO")
    print(f"   Arquivos testados: {len(resultados)}")
    print(f"   Abriram normalmente: {ok}")
    print(f"   Com problema: {len(problemas)}")
    if not a.apenas_novo:
        print(f"   Problemas causados pela migracao: {causados}")
    print(f"   Detalhes: {c}")


if __name__ == "__main__":
    main()
