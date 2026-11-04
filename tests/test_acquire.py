import json
from types import SimpleNamespace as NS

import pytest

from scientia.acquire import Acquirer
from scientia.config import Config
from scientia.inventory import classify
from scientia.models import Paper
from scientia.store import Store


class FakeHttp:
    """Serves a PDF for arXiv ids in `pdfs`, a 403 for other arXiv ids, and empty CORE results."""

    def __init__(self, pdfs):
        self.pdfs = pdfs
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "api.core.ac.uk" in url:
            return NS(status_code=200, json=lambda: {"results": []})
        arxiv_id = url.rsplit("/", 1)[-1]
        if arxiv_id in self.pdfs:
            return NS(status_code=200, content=b"%PDF-1.7 " + arxiv_id.encode())
        return NS(status_code=403, content=b"<html>Forbidden</html>")


@pytest.fixture
def env(tmp_path):
    config = Config(home=tmp_path / "lib", openalex_api_key=None, email=None)
    store = Store(config.db_path)
    yield config, store
    store.close()


def test_parallel_fetch_stores_files_and_attempt_logs(env):
    config, store = env
    ids = []
    for n in range(8):
        pid, _ = store.add_paper(Paper(title=f"Paper {n}", source="arxiv", arxiv_id=f"2001.0000{n}"))
        ids.append(pid)
    papers = [store.get_paper(pid) for pid in ids]
    http = FakeHttp(pdfs={"2001.00000", "2001.00002", "2001.00004"})
    acquirer = Acquirer(config, store, http)
    acquirer._s2_known = {pid: None for pid in ids}  # as if prefetch() found nothing

    seen = []
    outcomes = acquirer.fetch_many(papers, workers=4, on_done=lambda p, o: seen.append(p.id))
    assert sorted(seen) == sorted(ids)
    assert [o.ok for o in outcomes] == [True, False, True, False, True, False, False, False]
    assert store.files(ids[0])["pdf"]["via"] == "arxiv"

    logs = store.fetch_logs()
    assert set(logs) == set(ids)
    failed = json.loads(logs[ids[1]]["attempts"])
    assert {"via": "arxiv", "url": "https://arxiv.org/pdf/2001.00001", "result": "http_403", "detail": ""} in failed
    assert any(a["via"] == "core" and a["result"] == "no_link" for a in failed)


def classify_with(oa_status, attempts=None, files=None):
    paper = NS(oa_status=oa_status)
    log = {"attempts": json.dumps(attempts)} if attempts is not None else None
    return classify(paper, files or {}, log)[0]


def test_access_classification():
    no_links = [{"via": "semanticscholar", "url": None, "result": "no_link", "detail": ""},
                {"via": "core", "url": None, "result": "no_link", "detail": ""}]
    blocked = [{"via": "unpaywall", "url": "https://publisher/x.pdf", "result": "not_pdf", "detail": ""}]
    assert classify_with("gold", files={"pdf": {"via": "oa-link"}}) == "downloaded (PDF)"
    assert classify_with("green", files={"xml": {"via": "europepmc"}}) == "downloaded (XML only)"
    assert classify_with("closed") == "not attempted yet"
    assert classify_with("closed", no_links) == "licence needed"
    assert classify_with("closed", no_links + blocked) == "free copy, download failed"
    assert classify_with("bronze", no_links) == "free copy, download failed"
    paid = [{"via": "openalex-content", "url": None, "result": "skipped_paid", "detail": ""}]
    assert classify_with("closed", no_links + paid) == "free copy, OpenAlex cache"


def test_fetch_log_keeps_what_earlier_attempts_learned(env):
    _, store = env
    pid, _ = store.add_paper(Paper(title="P", source="openalex", doi="10.1/p"))
    first = [{"via": "semanticscholar", "url": "https://pub/x.pdf", "result": "http_403", "detail": ""},
             {"via": "core", "url": None, "result": "no_link", "detail": ""}]
    store.save_fetch_log(pid, False, first)
    # A later run with both lookups switched off must not erase the known free copy.
    store.save_fetch_log(pid, False, [
        {"via": "semanticscholar", "url": None, "result": "skipped_disabled", "detail": ""},
        {"via": "core", "url": None, "result": "skipped_disabled", "detail": ""},
        {"via": "openalex-content", "url": "https://content/x.pdf", "result": "http_429", "detail": ""},
    ])
    merged = json.loads(store.fetch_logs()[pid]["attempts"])
    assert {"via": "semanticscholar", "url": "https://pub/x.pdf", "result": "http_403", "detail": ""} in merged
    assert {"via": "core", "url": None, "result": "no_link", "detail": ""} in merged
    assert not any(a["via"] == "core" and a["result"] == "skipped_disabled" for a in merged)
    assert classify(NS(oa_status="closed"), {}, store.fetch_logs()[pid])[0] == "free copy, download failed"


class FakePMCHttp:
    """PMC cloud bucket with versions 1 and 2 of PMC123; a publisher landing page that
    declares its PDF in a citation_pdf_url meta tag."""

    def __init__(self):
        self.urls = []

    def get(self, url, params=None, **kwargs):
        self.urls.append(url)
        if url.endswith("amazonaws.com/") and params:
            body = "".join(f"<CommonPrefixes><Prefix>{params['prefix']}{v}/</Prefix></CommonPrefixes>" for v in (1, 2))
            return NS(status_code=200, text=f"<ListBucketResult>{body}</ListBucketResult>")
        if url.endswith("PMC123.2.json"):
            return NS(status_code=200, json=lambda: {
                "license_code": "CC BY", "pdf_url": "s3://pmc-oa-opendata/PMC123.2/PMC123.2.pdf?md5=x",
                "xml_url": "s3://pmc-oa-opendata/PMC123.2/PMC123.2.xml?md5=y"})
        if url.endswith("PMC123.2.pdf") or url == "https://publisher.example/real.pdf":
            return NS(status_code=200, content=b"%PDF-1.7 ok", url=url)
        if url.endswith("PMC123.2.xml"):
            return NS(status_code=200, content=b"<article/>", url=url)
        if url == "https://publisher.example/landing":
            page = b'<html><head><meta name="citation_pdf_url" content="/real.pdf"></head></html>'
            return NS(status_code=200, content=page, url=url)
        return NS(status_code=404, content=b"", url=url, json=lambda: {})


def test_pmc_cloud_supplies_pdf_xml_and_licence(env):
    config, store = env
    pid, _ = store.add_paper(Paper(title="PMC paper", source="europepmc", pmcid="PMC123", is_oa=True))
    acquirer = Acquirer(config, store, FakePMCHttp())
    acquirer._s2_known = {pid: None}
    outcome = acquirer.fetch(store.get_paper(pid))
    files = store.files(pid)
    assert outcome.pdf and outcome.xml
    assert files["pdf"]["via"] == files["xml"]["via"] == "pmc"
    assert files["pdf"]["license"] == "CC BY"
    assert files["pdf"]["url"].endswith("/PMC123.2/PMC123.2.pdf")  # latest version


def test_landing_page_citation_pdf_url_is_followed(env):
    config, store = env
    pid, _ = store.add_paper(Paper(title="Landing", source="openalex", doi="10.1/l",
                                   pdf_urls=["https://publisher.example/landing"]))
    acquirer = Acquirer(config, store, FakePMCHttp())
    acquirer._s2_known = {pid: None}
    outcome = acquirer.fetch(store.get_paper(pid))
    assert outcome.pdf and store.files(pid)["pdf"]["url"] == "https://publisher.example/real.pdf"
    assert [t.result for t in outcome.tries if t.via == "oa-link"] == ["not_pdf", "ok"]
