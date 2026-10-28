from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from scientia.config import Config
from scientia.importer import Importer, read_evidence
from scientia.models import Paper
from scientia.store import Store


def make_pdf(path: Path, text: str = "", metadata: dict | None = None) -> Path:
    """A one-page PDF with `text` drawn on it, so pypdf can extract it again."""
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(content)
    if metadata:
        writer.add_metadata(metadata)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture
def env(tmp_path):
    config = Config(home=tmp_path / "lib", openalex_api_key=None, email=None)
    store = Store(config.db_path)
    yield config, store, Importer(config, store)
    store.close()


def test_evidence_from_metadata_text_and_filename(tmp_path):
    pdf = make_pdf(tmp_path / "2101.00001v2.pdf", "See arXiv:2101.00001v2 and https://doi.org/10.1000/Text.1.",
                   {"/doi": "10.1000/META"})
    ev = read_evidence(pdf)
    assert ev.dois == [("10.1000/meta", "metadata"), ("10.1000/text.1", "page 1")]
    assert ev.arxiv_ids == ["2101.00001"]


def test_doi_match_files_pdf_and_marks_downloaded(env, tmp_path):
    config, store, importer = env
    topic = store.upsert_topic("t", "q")
    pid, _ = store.add_paper(Paper(title="Some paper", source="openalex", doi="10.1000/abc"))
    store.link(topic, pid)
    store.set_status(topic, pid, "unavailable")

    pdf = make_pdf(tmp_path / "download.pdf", "doi:10.1000/ABC")
    result = importer.import_pdf(pdf)
    assert (result.status, result.paper_id) == ("imported", pid)
    stored = store.files(pid)["pdf"]["path"]
    assert stored.startswith("papers/")  # relative to the library root
    filed = store.resolve(stored)
    assert filed.read_bytes() == pdf.read_bytes() and pdf.exists()  # copied, not moved
    assert (filed.parent / "metadata.json").exists()
    assert store.topic_papers(topic)[0].status == "downloaded"

    assert importer.import_pdf(pdf).status == "duplicate"


def test_title_on_first_page_matches_paper_without_pdf(env, tmp_path):
    config, store, importer = env
    pid, _ = store.add_paper(Paper(title="Spectral methods for stiff operator learning", source="arxiv"))
    store.add_paper(Paper(title="Operator learning", source="arxiv"))  # too short to title-match
    pdf = make_pdf(tmp_path / "x.pdf", "Spectral Methods for Stiff Operator Learning - J. Doe")
    result = importer.import_pdf(pdf, move=True)
    assert (result.status, result.paper_id) == ("imported", pid)
    assert not pdf.exists()


def test_unmatched_and_not_pdf(env, tmp_path):
    _, _, importer = env
    assert importer.import_pdf(make_pdf(tmp_path / "a.pdf", "nothing here")).status == "unmatched"
    html = tmp_path / "b.pdf"
    html.write_text("<!DOCTYPE html><title>Just a moment...</title>")
    assert importer.import_pdf(html).status == "not-pdf"
