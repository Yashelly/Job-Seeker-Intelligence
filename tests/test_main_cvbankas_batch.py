from argparse import Namespace

from src.main import build_cvbankas_job_for_scoring, merge_cvbankas_job, run_cvbankas_batch


BASE_CV_JOB = {
    "source": "cvbankas",
    "source_url": "https://www.cvbankas.lt/example/1-11111111",
    "external_id": "1-11111111",
    "company": "UAB Example",
    "role_title": "AI Automation Specialist",
    "location": "Vilnius",
    "work_mode": "hybrid",
    "salary": {"min": 2200, "max": 3200, "currency": "EUR", "gross_or_net": "gross", "period": "month"},
    "employment_type": "full-time",
    "responsibilities": ["Automate processes"],
    "required_skills": ["python"],
    "preferred_skills": [],
    "tools_and_platforms": ["api"],
    "language_requirements": ["english"],
    "experience_requirements": "",
    "education_requirements": "",
    "domain": "automation",
    "seniority_hints": [],
    "red_flags": [],
    "notes": "parsed_from=cvbankas",
    "raw_text": "AI Automation Specialist vacancy text",
    "raw_text_excerpt": "AI Automation Specialist vacancy text",
    "benefits": ["Hybrid work"],
}


def test_merge_cvbankas_job_prefers_cvbankas_identity_fields():
    extracted = {
        "company": "Wrong Company",
        "role_title": "Wrong Title",
        "location": "Kaunas",
        "work_mode": "remote",
        "salary": {"min": 1000, "max": 1500, "currency": "EUR", "gross_or_net": "net"},
        "employment_type": "part-time",
        "responsibilities": ["Use APIs"],
        "required_skills": ["python", "make"],
        "preferred_skills": ["sql"],
        "tools_and_platforms": ["make"],
        "language_requirements": ["english", "lithuanian"],
        "experience_requirements": "1 year",
        "education_requirements": "",
        "domain": "ai_process_automation",
        "seniority_hints": ["ownership"],
        "red_flags": [],
        "notes": "from_openai",
        "raw_text_excerpt": "excerpt",
    }

    merged = merge_cvbankas_job(BASE_CV_JOB, extracted)

    assert merged["company"] == "UAB Example"
    assert merged["role_title"] == "AI Automation Specialist"
    assert merged["location"] == "Vilnius"
    assert merged["salary"]["min"] == 2200
    assert merged["work_mode"] == "remote"
    assert merged["required_skills"] == ["python", "make"]
    assert merged["notes"] == "from_openai | parsed_from=cvbankas"
    assert merged["raw_text"] == "AI Automation Specialist vacancy text"


def test_build_cvbankas_job_for_scoring_uses_extraction_strategy(monkeypatch):
    def fake_extract(text, strategy, openai_model=None):
        assert text == "AI Automation Specialist vacancy text"
        assert strategy == "openai"
        assert openai_model == "gpt-4o-mini"
        return {
            "company": "Other",
            "role_title": "Other",
            "location": "Other",
            "work_mode": "remote",
            "salary": {"min": 1, "max": 2, "currency": "EUR", "gross_or_net": "gross"},
            "employment_type": "full-time",
            "responsibilities": ["Use APIs"],
            "required_skills": ["python", "api"],
            "preferred_skills": [],
            "tools_and_platforms": ["api"],
            "language_requirements": ["english"],
            "experience_requirements": "",
            "education_requirements": "",
            "domain": "ai_process_automation",
            "seniority_hints": [],
            "red_flags": [],
            "notes": "from_openai",
            "raw_text_excerpt": "x",
        }

    monkeypatch.setattr("src.main.extract_job_with_strategy", fake_extract)
    job = build_cvbankas_job_for_scoring(
        cv_job=BASE_CV_JOB,
        extractor="openai",
        openai_model="gpt-4o-mini",
    )

    assert job["company"] == "UAB Example"
    assert job["required_skills"] == ["python", "api"]
    assert job["work_mode"] == "remote"


def test_run_cvbankas_batch_processes_multiple_links(monkeypatch, tmp_path):
    saved = []

    def fake_collect_listing_links(**kwargs):
        return [
            "https://www.cvbankas.lt/job-one/1-11111111",
            "https://www.cvbankas.lt/job-two/1-22222222",
        ]

    def fake_fetch_and_parse_job(url):
        job = dict(BASE_CV_JOB)
        job["source_url"] = url
        job["external_id"] = url.rsplit("/", 1)[-1]
        job["role_title"] = "Role One" if "job-one" in url else "Role Two"
        return job

    def fake_process_single_job(*, job, profile, db_path, source_type, source_ref):
        saved.append((job["role_title"], source_type, source_ref, db_path))
        return len(saved), {"score": 88, "decision": "apply"}

    monkeypatch.setattr("src.main.collect_listing_links", fake_collect_listing_links)
    monkeypatch.setattr("src.main.fetch_and_parse_job", fake_fetch_and_parse_job)
    monkeypatch.setattr("src.main.process_single_job", fake_process_single_job)
    monkeypatch.setattr("src.main.load_profile", lambda _: {"skills": {}, "target_tracks": [], "constraints": {}})
    monkeypatch.setattr("src.main.init_db", lambda _: None)

    args = Namespace(
        profile="profile/user_profile.yaml",
        db=str(tmp_path / "app.db"),
        extractor="heuristic",
        openai_model=None,
        cvbankas_start_url="https://www.cvbankas.lt/",
        cvbankas_max_pages=2,
        cvbankas_limit=10,
        cvbankas_delay_seconds=0.0,
    )

    rc = run_cvbankas_batch(args)

    assert rc == 0
    assert saved == [
        ("Role One", "cvbankas", "https://www.cvbankas.lt/job-one/1-11111111", str(tmp_path / "app.db")),
        ("Role Two", "cvbankas", "https://www.cvbankas.lt/job-two/1-22222222", str(tmp_path / "app.db")),
    ]
