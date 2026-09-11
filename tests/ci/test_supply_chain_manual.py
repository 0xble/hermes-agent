"""Execute the actual workflow scan steps against disposable commit histories."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('job,path,content', [
    ('critical-patterns', 'startup.pth', 'import dangerous\n'),
    ('dependency-bounds', 'pyproject.toml', 'dependencies = ["unsafe>=1.0"]\n'),
])
def test_manual_scan_rejects_added_risk_and_invalid_revision(tmp_path, job, path, content):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    git('commit', '--allow-empty', '-qm', 'base')
    (tmp_path / path).write_text(content)
    git('add', path)
    git('commit', '-qm', 'risk')
    workflow = yaml.load((ROOT / '.github/workflows/supply-chain-audit.yml').read_text(), Loader=yaml.BaseLoader)
    script = next(step['run'] for step in workflow['jobs'][job]['steps'] if 'run' in step)
    for head in [git('rev-parse', 'HEAD'), 'missing-ref']:
        result = subprocess.run(['bash', '-c', script], cwd=tmp_path,
                                env={**os.environ, 'BASE': '', 'HEAD': head},
                                capture_output=True, text=True)
        assert result.returncode != 0, result.stdout + result.stderr
