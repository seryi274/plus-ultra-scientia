from datetime import date

from scientia.ranking import query_terms, score, term_sets

TODAY = date(2026, 1, 1)


def test_query_terms_drop_stopwords_fields_and_plurals():
    assert query_terms("neural operators for PDEs") == ["neural", "operator", "pde"]
    assert query_terms("ti:transformer AND cat:cs.LG") == ["transformer", "cs", "lg"]


def test_relevance_dominates():
    terms = term_sets(["fourier neural operator"])
    on_topic = score(terms, "Fourier neural operators", None, 2024, 5, TODAY)
    off_topic = score(terms, "Graph coloring heuristics", None, 2024, 500, TODAY)
    assert on_topic > off_topic


def test_scores_are_bounded():
    assert 0 <= score(term_sets(["x"]), "x", "x", 2026, 10**6, TODAY) <= 1


def test_being_cited_by_the_topic_outweighs_citing_it():
    foundational = score([], "x", None, 2020, 0, TODAY, cited_in=10, cites_out=0, max_links=10)
    review = score([], "x", None, 2020, 0, TODAY, cited_in=0, cites_out=10, max_links=10)
    assert foundational > review


def test_or_alternatives_and_multiple_queries_each_match_fully():
    sets = term_sets(['"ankylosing spondylitis" OR "axial spondyloarthritis"', "secukinumab"])
    assert sets == [[[["ankylosing", "spondyliti"]]], [[["axial", "spondyloarthriti"]]], [[["secukinumab"]]]]
    old_name = score(sets, "Ankylosing spondylitis outcomes", None, 2020, 0, TODAY)
    new_name = score(sets, "Axial spondyloarthritis outcomes", None, 2020, 0, TODAY)
    drug = score(sets, "Secukinumab in practice", None, 2020, 0, TODAY)
    unrelated = score(sets, "Psoriasis outcomes", None, 2020, 0, TODAY)
    assert old_name == new_name == drug > unrelated



def test_grouped_queries_need_one_alternative_per_group():
    [q] = term_sets(['("ankylosing spondylitis" OR "axial spondyloarthritis") AND (secukinumab OR ixekizumab) NOT psoriasis'])
    assert q == [[["ankylosing", "spondyliti"], ["axial", "spondyloarthriti"]], [["secukinumab"], ["ixekizumab"]]]
    full = score([q], "Ixekizumab in axial spondyloarthritis", None, 2020, 0, TODAY)
    half = score([q], "Ixekizumab in psoriasis", None, 2020, 0, TODAY)
    none = score([q], "Statins in heart failure", None, 2020, 0, TODAY)
    assert full > half > none
