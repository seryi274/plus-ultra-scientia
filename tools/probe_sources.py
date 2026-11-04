"""Probe how many missing papers OpenAIRE and Crossref could add. Read-only: downloads are
checked and discarded, nothing is written to the library.
Usage: python probe_sources.py <inventory.csv> <research.db> <out.json>"""
import csv
import json
import random
import sqlite3
import sys
from collections import Counter
from urllib.parse import urlsplit

from scientia.acquire import _meta_pdf_url
from scientia.net import HttpClient

inventory, db, out = sys.argv[1:4]
rows = list(csv.DictReader(open(inventory, encoding="utf-8-sig")))
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tried = {}
for pid, attempts in conn.execute("SELECT paper_id, attempts FROM fetch_log"):
    tried[pid] = {a["url"] for a in json.loads(attempts) if a["url"]}

def doi_of(r):
    link = r["doi link"]
    return link.split("doi.org/", 1)[1].lower() if "doi.org/" in link else None

blocked = [r for r in rows if r["access"] == "free copy, download failed" and doi_of(r)]
licence = [r for r in rows if r["access"] == "licence needed" and doi_of(r)]
random.seed(7)
sample = [("blocked", r) for r in blocked] + [("licence", r) for r in random.sample(licence, 100)]

SKIP_HOSTS = ("doi.org", "dx.doi.org", "pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "europepmc.org")
http = HttpClient(retries=1, timeout=20)


def is_pdf(url, depth=0):
    try:
        r = http.get(url)
    except Exception as exc:
        return f"network:{type(exc).__name__}"
    if r.status_code != 200:
        return f"http_{r.status_code}"
    if r.content.lstrip()[:5].startswith(b"%PDF"):
        return "pdf"
    if depth == 0 and (target := _meta_pdf_url(r.content, str(r.url))) and target != url:
        return is_pdf(target, 1)
    return "not_pdf"


def openaire(doi):
    r = http.get("https://api.openaire.eu/graph/v1/researchProducts", params={"pid": doi}, retries=1)
    if r.status_code != 200:
        return None, []
    res = r.json().get("results") or []
    if not res:
        return "not_found", []
    urls = []
    for inst in res[0].get("instances", []):
        for u in inst.get("urls", []):
            if not urlsplit(u).hostname or urlsplit(u).hostname in SKIP_HOSTS:
                continue
            urls.append(u)
    return (res[0].get("bestAccessRight") or {}).get("label"), list(dict.fromkeys(urls))


def crossref(doi):
    r = http.get(f"https://api.crossref.org/works/{doi}", retries=1)
    if r.status_code != 200:
        return None, [], []
    m = r.json()["message"]
    links = [l["URL"] for l in m.get("link", []) if l.get("content-type") in ("application/pdf", "unspecified")]
    licences = [l.get("URL") for l in m.get("license", [])]
    return m.get("publisher"), links, licences


def probe(item):
    group, r = item
    pid, doi = int(r["paper_id"]), doi_of(r)
    known = tried.get(pid, set())
    access_right, oa_urls = openaire(doi)
    oa_new = [u for u in oa_urls if u not in known]
    publisher, cr_links, cr_lic = crossref(doi)
    cr_new = [u for u in cr_links if u not in known]
    oa_res = {u: is_pdf(u) for u in oa_new[:4]}
    cr_res = {u: is_pdf(u) for u in cr_new[:2]}
    return {"group": group, "paper_id": pid, "doi": doi, "publisher": publisher,
            "openaire_access": access_right, "openaire": oa_res, "crossref": cr_res,
            "cc_licence": any("creativecommons" in (l or "") for l in cr_lic)}


def safe_probe(item):
    try:
        return probe(item)
    except Exception as exc:
        return {"group": item[0], "paper_id": int(item[1]["paper_id"]), "doi": doi_of(item[1]), "error": repr(exc),
                "publisher": None, "openaire": {}, "crossref": {}, "cc_licence": False}


from concurrent.futures import ThreadPoolExecutor
results = []
with ThreadPoolExecutor(6) as pool:
    for i, res in enumerate(pool.map(safe_probe, sample), 1):
        results.append(res)
        if i % 50 == 0:
            print(f"{i}/{len(sample)}", flush=True)
print("errors:", sum(1 for x in results if "error" in x))

json.dump(results, open(out, "w", encoding="utf-8"), indent=1)
for group in ("blocked", "licence"):
    g = [x for x in results if x["group"] == group]
    oa_any = sum(1 for x in g if x["openaire"])
    oa_pdf = sum(1 for x in g if "pdf" in x["openaire"].values())
    cr_any = sum(1 for x in g if x["crossref"])
    cr_pdf = sum(1 for x in g if "pdf" in x["crossref"].values())
    either = sum(1 for x in g if "pdf" in x["openaire"].values() or "pdf" in x["crossref"].values())
    print(f"\n{group}: {len(g)} papers")
    print(f"  OpenAIRE: new links for {oa_any}, downloadable PDF for {oa_pdf}")
    print(f"  Crossref: new links for {cr_any}, downloadable PDF for {cr_pdf}")
    print(f"  either source: {either}")
    print("  OpenAIRE results:", Counter(v for x in g for v in x["openaire"].values()).most_common(5))
    print("  Crossref results:", Counter(v for x in g for v in x["crossref"].values()).most_common(5))
    print("  PDF hosts:", Counter(urlsplit(u).hostname for x in g for src in ("openaire", "crossref")
                                  for u, v in x[src].items() if v == "pdf").most_common(8))
