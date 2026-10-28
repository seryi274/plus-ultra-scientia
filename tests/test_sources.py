from scientia.sources.arxiv import build_query, parse_feed
from scientia.sources.europepmc import parse_result
from scientia.sources.openalex import parse_work

ARXIV_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2010.08895v3</id>
    <title>Fourier Neural Operator for
      Parametric Partial Differential Equations</title>
    <summary>  We formulate a new neural operator.  </summary>
    <published>2020-10-18T19:00:00Z</published>
    <author><name>Zongyi Li</name></author>
    <author><name>Anima Anandkumar</name></author>
    <arxiv:doi>10.1000/EXAMPLE</arxiv:doi>
  </entry>
</feed>"""


def test_parse_arxiv_feed():
    [p] = parse_feed(ARXIV_FEED)
    assert p.arxiv_id == "2010.08895"
    assert p.title == "Fourier Neural Operator for Parametric Partial Differential Equations"
    assert p.abstract == "We formulate a new neural operator."
    assert p.authors == ["Zongyi Li", "Anima Anandkumar"]
    assert (p.year, p.published) == (2020, "2020-10-18")
    assert p.doi == "10.1000/example"
    assert p.pdf_urls == ["https://arxiv.org/pdf/2010.08895"]


def test_arxiv_query_building():
    assert build_query("neural operator") == "all:neural AND all:operator"
    assert build_query('"neural operator" pde') == 'all:"neural operator" AND all:pde'
    assert build_query("ti:transformer AND cat:cs.LG") == "ti:transformer AND cat:cs.LG"
    assert build_query("x", since="2023-01-01") == (
        "(all:x) AND submittedDate:[202301010000 TO 299912312359]"
    )


def test_parse_openalex_work():
    work = {
        "id": "https://openalex.org/W1",
        "doi": "https://doi.org/10.48550/arXiv.2111.13802",
        "display_name": "Factorized Fourier Neural Operators",
        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123",
                "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/456"},
        "publication_year": 2021,
        "publication_date": "2021-11-27",
        "authorships": [{"author": {"display_name": "Alasdair Tran"}}],
        "primary_location": {"is_oa": True, "landing_page_url": "http://arxiv.org/abs/2111.13802",
                             "pdf_url": "https://arxiv.org/pdf/2111.13802",
                             "source": {"display_name": "arXiv", "type": "repository"}},
        "locations": [{"is_oa": False, "pdf_url": None, "landing_page_url": "https://x.org",
                       "source": {"display_name": "ICLR", "type": "conference"}}],
        "best_oa_location": {"is_oa": True, "pdf_url": "https://arxiv.org/pdf/2111.13802",
                             "license": "cc-by"},
        "open_access": {"is_oa": True},
        "cited_by_count": 70,
        "abstract_inverted_index": {"operators": [1], "Neural": [0], "scale.": [2]},
        "has_content": {"pdf": True},
    }
    p = parse_work(work)
    assert p.openalex_id == "W1"
    assert p.doi == "10.48550/arxiv.2111.13802"
    assert p.arxiv_id == "2111.13802"
    assert (p.pmid, p.pmcid) == ("123", "PMC456")
    assert p.abstract == "Neural operators scale."
    assert p.venue == "ICLR"
    assert p.pdf_urls == ["https://arxiv.org/pdf/2111.13802"]
    assert p.license == "cc-by" and p.openalex_has_pdf


def test_parse_europepmc_result():
    r = {
        "id": "PMC999", "source": "PMC", "pmid": "111", "pmcid": "PMC999", "doi": "10.1/ABC",
        "title": "Deep learning for <i>protein</i> folding",
        "authorList": {"author": [{"fullName": "Doe J"}, {"fullName": "Roe R"}]},
        "pubYear": "2024", "firstPublicationDate": "2024-02-01",
        "journalInfo": {"journal": {"title": "Nature Methods"}},
        "abstractText": "<h4>Background</h4>Proteins <b>fold</b>.",
        "isOpenAccess": "Y", "citedByCount": 5, "license": "cc by",
        "fullTextUrlList": {"fullTextUrl": [
            {"documentStyle": "pdf", "availabilityCode": "OA", "url": "https://europepmc.org/a.pdf"},
            {"documentStyle": "pdf", "availabilityCode": "S", "url": "https://paywall.example/a.pdf"},
        ]},
    }
    p = parse_result(r)
    assert p.title == "Deep learning for protein folding"
    assert p.abstract == "Background Proteins fold ."
    assert p.authors == ["Doe J", "Roe R"]
    assert (p.year, p.venue, p.doi, p.pmcid) == (2024, "Nature Methods", "10.1/abc", "PMC999")
    assert p.is_oa and p.pdf_urls == ["https://europepmc.org/a.pdf"]


def test_core_skips_cloudflare_protected_links():
    from scientia.sources.core import Core

    work = {
        "sourceFulltextUrls": ["https://core.ac.uk/download/1.pdf", "http://repo.example.edu/1.pdf"],
        "outputs": ["https://api.core.ac.uk/v3/outputs/1"],
    }
    assert Core(http=None)._download_urls(work) == ["http://repo.example.edu/1.pdf"]
    keyed = Core(http=None, api_key="k")
    assert keyed._download_urls(work)[-1] == "https://api.core.ac.uk/v3/outputs/1/download"
    assert keyed.headers_for("https://api.core.ac.uk/v3/outputs/1/download") == {"Authorization": "Bearer k"}
    assert keyed.headers_for("http://repo.example.edu/1.pdf") == {}


def test_semanticscholar_pdf_field():
    from scientia.sources.semanticscholar import _pdf

    assert _pdf({"openAccessPdf": {"url": "https://x/a.pdf", "license": "CCBY"}}) == ("https://x/a.pdf", "CCBY")
    assert _pdf({"openAccessPdf": {"url": "", "status": "CLOSED"}}) is None
    assert _pdf({}) is None
