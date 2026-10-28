"""Desktop launchers: repo-root scripts are path-independent, the Desktop copy names the repository, and the
shell scripts parse."""
import os
import shutil
import subprocess

import pytest

from scripts import install_desktop_shortcut as sc


@pytest.mark.parametrize("system,names", [
    ("Darwin", ["Launch NJ Wholesale Dashboard.command"]),
    ("Linux", ["Launch NJ Wholesale Dashboard.sh", "nj-wholesale-dashboard.desktop"]),
    ("Windows", ["Launch NJ Wholesale Dashboard.bat"]),
])
def test_install_writes_repo_launchers_and_desktop_copy(tmp_path, system, names):
    repo, desktop = tmp_path / "my repo", tmp_path / "Desktop"
    repo.mkdir()
    desktop.mkdir()
    out = sc.install(desktop=desktop, system=system, repo=repo)
    assert sorted(os.path.basename(p) for p in out["repo"]) == ["Launch_Dashboard.bat", "Launch_Dashboard.command",
                                                                "launch_dashboard.sh"]
    assert sorted(os.path.basename(p) for p in out["desktop"]) == sorted(names)
    sh = (repo / "launch_dashboard.sh").read_text()
    assert 'REPO="$(cd "$(dirname "$0")" && pwd)"' in sh and str(repo) not in sh      # path-independent
    assert "/api/health" in sh and "nohup" in sh and "logs/server.log" in sh and "NJW_NO_BROWSER" in sh
    assert os.access(repo / "launch_dashboard.sh", os.X_OK) and os.access(repo / "Launch_Dashboard.command", os.X_OK)
    bat = (repo / "Launch_Dashboard.bat").read_bytes()
    assert b"\r\n" in bat and b'cd /d "%~dp0"' in bat and b"manage.py server --port 8000" in bat
    copy = (desktop / names[0]).read_text()
    assert str(repo) in copy                                                            # works from the Desktop


def test_no_desktop_folder_still_writes_repo_launchers(tmp_path):
    out = sc.install(desktop=tmp_path / "missing", system="Linux", repo=tmp_path)
    assert out["desktop"] == [] and len(out["repo"]) == 3


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_shell_launchers_parse(tmp_path):
    for text in (sc.unix_script(), sc.unix_script("/opt/it's here")):
        f = tmp_path / "l.sh"
        f.write_text(text)
        assert subprocess.run(["bash", "-n", str(f)], capture_output=True).returncode == 0
