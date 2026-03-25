from src.cvbankas import _build_page_url, collect_listing_links, parse_detail_html


LISTING_PAGE_1 = """
<html><body>
<a href="/junior-it-specialist-vilniuje/1-11111111">Junior IT Specialist</a>
<a href="/m365-administrator-vilniuje/1-22222222">M365 Administrator</a>
<a href="/?page=2">2</a>
</body></html>
"""

LISTING_PAGE_2 = """
<html><body>
<a href="/m365-administrator-vilniuje/1-22222222">M365 Administrator</a>
<a href="/cloud-support-engineer-kaune/1-33333333">Cloud Support Engineer</a>
</body></html>
"""

DETAIL_HTML = """
<html>
  <head>
    <title>Lead Microsoft 365 Administrator Vilniuje, UAB Example | CVbankas.lt</title>
  </head>
  <body>
    <h1>Lead Microsoft 365 Administrator</h1>
    <div>2500-3500 €/mėn. neatskaičius mokesčių</div>
    <div>Visa darbo diena</div>
    <div>Vilnius - UAB Example</div>

    <h2>Darbo pobūdis</h2>
    <ul>
      <li>Administruoti Microsoft 365 aplinką</li>
      <li>Automatizuoti pasikartojančius procesus per PowerShell ir API</li>
    </ul>

    <h2>Reikalavimai</h2>
    <ul>
      <li>Patirtis su Intune ir Entra ID</li>
      <li>Geros anglų kalbos žinios</li>
    </ul>

    <h2>Verta kandidatuoti, nes</h2>
    <ul>
      <li>Hibridinis darbo modelis</li>
      <li>Mokymų biudžetas</li>
    </ul>
  </body>
</html>
"""


class DummySession:
    def __init__(self):
        self.responses = {
            "https://www.cvbankas.lt/": LISTING_PAGE_1,
            "https://www.cvbankas.lt/?page=2": LISTING_PAGE_2,
        }

    def get(self, url, timeout=None, headers=None):
        class Response:
            def __init__(self, text):
                self.text = text
                self.status_code = 200

        return Response(self.responses[url])


def test_build_page_url():
    assert _build_page_url("https://www.cvbankas.lt/", 1) == "https://www.cvbankas.lt/"
    assert _build_page_url("https://www.cvbankas.lt/", 2) == "https://www.cvbankas.lt/?page=2"
    assert _build_page_url("https://www.cvbankas.lt/?foo=bar", 3) == "https://www.cvbankas.lt/?foo=bar&page=3"


def test_collect_listing_links_across_pages():
    links = collect_listing_links(session=DummySession(), max_pages=2)
    assert links == [
        "https://www.cvbankas.lt/junior-it-specialist-vilniuje/1-11111111",
        "https://www.cvbankas.lt/m365-administrator-vilniuje/1-22222222",
        "https://www.cvbankas.lt/cloud-support-engineer-kaune/1-33333333",
    ]


def test_parse_detail_html():
    job = parse_detail_html(
        DETAIL_HTML,
        url="https://www.cvbankas.lt/lead-microsoft-365-administrator-vilniuje/1-99999999",
    )

    assert job["source"] == "cvbankas"
    assert job["external_id"] == "1-99999999"
    assert job["role_title"] == "Lead Microsoft 365 Administrator"
    assert job["company"] == "UAB Example"
    assert job["location"] == "Vilnius"
    assert job["salary"]["min"] == 2500
    assert job["salary"]["max"] == 3500
    assert job["salary"]["gross_or_net"] == "gross"
    assert job["employment_type"] == "full-time"
    assert len(job["responsibilities"]) == 2
    assert "intune" in [x.lower() for x in job["required_skills"]]
    assert "entra id" in [x.lower() for x in job["required_skills"]]
    assert job["benefits"] == ["Hibridinis darbo modelis", "Mokymų biudžetas"]
