from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.parser import VacancyParser


class VacancyParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = (
            Path(__file__).resolve().parents[1] / "sample_data" / "vacancy_python_backend.html"
        ).read_text(encoding="utf-8")
        self.live_fixture = """
        <html>
          <body>
            <header id="jobad_header">
              <h1 class="heading1" id="jobad_heading1" itemprop="title">IT inžinierius (-ė)</h1>
              <div class="group_component ad_info_group">
                <div class="salary_component">
                  <span class="data_tag_component_salary_amount">1800-2800</span> €/mėn. neatskaičius mokesčių
                </div>
              </div>
              <div id="jobad_location" class="txt_2">
                <span itemprop="address"><a href="#"><span itemprop="addressLocality">Kaunas</span></a></span>
                - Venipak įmonių grupė
              </div>
            </header>
            <section itemprop="description">
              <section>
                <div class="jobad_txt">Ieškome IT specialisto, kuris administruotų tinklus.</div>
              </section>
              <section>
                <h2 class="heading2 jobad_subheading">Pagrindinės atsakomybės:</h2>
                <div class="jobad_txt"><ul><li>Programinės įrangos diegimas.</li><li>Tinklo priežiūra.</li></ul></div>
              </section>
              <section>
                <h2 class="heading2 jobad_subheading">Reikalavimai kandidatui (-ei):</h2>
                <div class="jobad_txt"><ul><li>Linux patirtis</li><li>IT išsilavinimas</li></ul></div>
              </section>
              <section>
                <h2 class="heading2 jobad_subheading">Atlyginimas</h2>
                <div class="jobad_txt">1800-2800 €/mėn. neatskaičius mokesčių</div>
              </section>
            </section>
            <section id="jobad_company_c">
              <h2 id="jobad_company_title">Venipak įmonių grupė</h2>
            </section>
          </body>
        </html>
        """

    def test_parse_extracts_structured_fields(self) -> None:
        vacancy = VacancyParser().parse(
            self.fixture,
            "https://www.cvbankas.lt/python-backend-developer-vilniuje/1-1234567",
        )

        self.assertEqual(vacancy.title, "Python Backend Developer")
        self.assertEqual(vacancy.company, "Baltic Systems")
        self.assertIn("Python", vacancy.requirements)
        self.assertIn("Write tests", vacancy.responsibilities)
        self.assertIn("Python Backend Developer", vacancy.raw_text)

    def test_parse_extracts_live_cvbankas_layout(self) -> None:
        vacancy = VacancyParser().parse(
            self.live_fixture,
            "https://www.cvbankas.lt/it-inzinierius-e-kaune/1-13867124",
        )

        self.assertEqual(vacancy.source_id, "1-13867124")
        self.assertEqual(vacancy.source_name, "cvbankas")
        self.assertEqual(vacancy.title, "IT inžinierius (-ė)")
        self.assertEqual(vacancy.company, "Venipak įmonių grupė")
        self.assertEqual(vacancy.location, "Kaunas")
        self.assertIn("Linux patirtis", vacancy.requirements)
        self.assertIn("Programinės įrangos diegimas.", vacancy.responsibilities)


if __name__ == "__main__":
    unittest.main()
