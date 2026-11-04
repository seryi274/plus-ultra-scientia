import httpx

from scientia import net
from scientia.net import PUBLISHER_INTERVAL, HttpClient, site_of


def test_hosts_map_to_publisher_sites():
    assert site_of("linkinghub.elsevier.com") == site_of("www.sciencedirect.com") == site_of("www.cell.com") == "elsevier"
    assert site_of("onlinelibrary.wiley.com") == "wiley"
    assert site_of("ard.bmj.com") == "bmj"
    assert site_of("link.springer.com") == site_of("www.nature.com") == "springer-nature"
    assert site_of("arxiv.org") == "arxiv.org"
    assert site_of("notelsevier.com") == "notelsevier.com"  # suffix match on whole labels only


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def routes(request: httpx.Request) -> httpx.Response:
    hops = {
        "https://doi.org/10.1016/x": "https://linkinghub.elsevier.com/retrieve/pii/X",
        "https://linkinghub.elsevier.com/retrieve/pii/X": "https://www.sciencedirect.com/science/article/pii/X",
    }
    if str(request.url) in hops:
        return httpx.Response(302, headers={"Location": hops[str(request.url)]})
    return httpx.Response(200, content=b"%PDF-1.7")


def test_redirect_hops_are_paced_by_publisher(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(net.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(net.time, "sleep", clock.sleep)
    http = HttpClient(transport=httpx.MockTransport(routes))

    response = http.get("https://doi.org/10.1016/x")
    assert response.status_code == 200 and str(response.url).startswith("https://www.sciencedirect.com/")
    # doi.org is free; the two Elsevier hops share one budget, so the second one waits.
    assert clock.slept == [PUBLISHER_INTERVAL]

    http.get("https://www.cell.com/article.pdf")  # another Elsevier domain: same queue
    assert clock.slept == [PUBLISHER_INTERVAL, PUBLISHER_INTERVAL]
    http.get("https://arxiv.org/pdf/2001.00001")  # unrelated site: no wait
    assert len(clock.slept) == 2
