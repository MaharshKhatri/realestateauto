"""Static funnel build + publish gate: operator details from SITE_* variables, disclosures present, and no e-mail
or phone number in the output except the operator's own and the published help lines."""
import pytest

from engine import site_build as sb

SITE_ENV = {"SITE_COMPANY": "Garden State Home Buyers LLC", "SITE_OPERATOR": "Jordan Sample",
            "SITE_PHONE": "908-555-0100", "SITE_EMAIL": "jordan@example.org",
            "SITE_POSTAL": "PO Box 10, Edison, NJ 08817", "SITE_FORM_ENDPOINT": "https://formspree.io/f/abc123"}


@pytest.fixture()
def site_env(monkeypatch):
    for k, v in SITE_ENV.items():
        monkeypatch.setenv(k, v)


def test_unconfigured_build_fails_the_gate(tmp_path, monkeypatch):
    for k in SITE_ENV:
        monkeypatch.delenv(k, raising=False)
    cfg = sb.build(tmp_path / "out")
    problems = sb.check(tmp_path / "out", cfg)
    assert any("no form endpoint" in p for p in problems) and any("operator email" in p for p in problems)


def test_configured_build_passes_and_uses_variables(tmp_path, site_env):
    from core.config import settings
    before = dict(settings().operator)
    out = tmp_path / "out"
    cfg = sb.build(out)
    assert sb.check(out, cfg) == []
    assert cfg["company"] == "Garden State Home Buyers LLC" and cfg["phone"] == "(908) 555-0100"
    assert cfg["mode"] == "endpoint" and "Mercer" in cfg["counties"] and (out / ".nojekyll").exists()
    assert dict(settings().operator) == before                      # the build doesn't change the running settings


def test_gate_catches_foreign_contacts_and_stray_files(tmp_path, site_env):
    out = tmp_path / "out"
    cfg = sb.build(out)
    page = out / "index.html"
    page.write_text(page.read_text() + "<p>Call Quinn at (973) 555-0142 or quinn@sample.org</p>")
    (out / "leads.csv").write_text("name,phone\n")
    problems = sb.check(out, cfg)
    assert any("(973) 555-0142" in p for p in problems) and any("quinn@sample.org" in p for p in problems)
    assert any("unexpected file in the build: leads.csv" in p for p in problems)
    page.write_text(page.read_text().replace("not licensed real estate agents", "agents"))
    assert any("missing the disclosure" in p for p in sb.check(out, cfg))
