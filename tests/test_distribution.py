"""Check a clean extracted distribution, without the host repository installed."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_standalone_paths_and_safe_defaults(tmp_path):
    source = Path(__file__).resolve().parents[1]
    extracted = tmp_path / 'arbitrary folder' / 'kick-chat-translator'
    shutil.copytree(source / 'kicktrans', extracted / 'kicktrans', ignore=shutil.ignore_patterns('__pycache__'))
    env = {k:v for k,v in os.environ.items() if k not in ('SCRIPTS_ROOT','SCRIPTS_STATE','PYTHONPATH')}
    code = ('from kicktrans.paths import ROOT_DIR,STATE_DIR; from kicktrans.config import Config; '
            'from pathlib import Path; assert ROOT_DIR == Path.cwd(); '
            'assert STATE_DIR == Path.cwd()/"state"; '
            'c=Config.load(); assert not c.live_posting and not c.receive_events; '
            'assert c.client_id == "" and c.channel_slug == "your-channel"; '
            'assert not STATE_DIR.exists(); c.save(); assert STATE_DIR.exists()')
    subprocess.run([sys.executable,'-c',code],cwd=extracted,env=env,check=True,timeout=15)


def test_distribution_ignores_runtime_and_has_onboarding():
    root = Path(__file__).resolve().parents[1]
    ignored=(root/'.gitignore').read_text(encoding='utf-8-sig')
    assert all(value in ignored for value in ('state/','.venv/','*.dpapi','*.bak'))
    readme=(root/'README.md').read_text(encoding='utf-8-sig')
    assert 'login_windows.bat' in readme and 'Client ID' in readme
    assert (root/'LICENSE').exists()