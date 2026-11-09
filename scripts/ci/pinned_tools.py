"""Cache metadata and uv tool-cache seeding through the Linux installer's verifier."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('linux_tools', ROOT / 'ci/install-linux-tools.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def uv_pin(runner_os: str, runner_arch: str, version: str) -> dict[str, str]:
    target = {'Linux': 'unknown-linux-gnu', 'macOS': 'apple-darwin', 'Windows': 'pc-windows-msvc'}[runner_os]
    architecture = {'X64': 'x86_64', 'ARM64': 'aarch64'}[runner_arch]
    extension = 'zip' if runner_os == 'Windows' else 'tar.gz'
    filename = f'uv-{architecture}-{target}.{extension}'
    pins = json.loads((ROOT / 'ci/uv-artifacts.json').read_text())
    if version != pins['version']:
        raise ValueError(f'No pinned uv checksums for {version}')
    return {'url': f'https://github.com/astral-sh/uv/releases/download/{version}/{filename}',
            'sha256': pins['sha256'][filename], 'version': version, 'architecture': architecture}


def seed_uv(pin: dict, cache: Path, tool_cache: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='hermes-ci-uv-') as directory:
        scratch = Path(directory)
        archive = installer.verified_archive(pin, scratch, cache)
        unpacked = scratch / 'unpacked'
        if pin['url'].endswith('.zip'):
            unpacked.mkdir()
            with zipfile.ZipFile(archive) as source:
                source.extractall(unpacked)
        else:
            with installer.tarfile.open(archive) as source:
                installer.extract_archive(source, unpacked)
        # Match @actions/tool-cache's uv layout so setup-uv preserves its normal
        # PATH, outputs and dependency-cache behavior without another download.
        destination = tool_cache / 'uv' / pin['version'] / pin['architecture']
        destination.mkdir(parents=True, exist_ok=True)
        suffix = '.exe' if pin['url'].endswith('.zip') else ''
        for name in ('uv', 'uvx'):
            binary, = unpacked.rglob(name + suffix)
            shutil.copy2(binary, destination / binary.name)
            (destination / binary.name).chmod(0o755)
        destination.with_suffix('.complete').touch()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('info', 'fetch'))
    parser.add_argument('--source', choices=('linux', 'uv'), default='linux')
    parser.add_argument('--tool', required=True)
    parser.add_argument('--architecture', default='amd64')
    parser.add_argument('--version', default='')
    args = parser.parse_args()
    cache = Path('.ci/linux-tool-archives')
    if args.source == 'linux':
        info = installer.cache_info(args.architecture, args.tool, cache)
    else:
        pin = uv_pin(os.environ['RUNNER_OS'], os.environ['RUNNER_ARCH'], args.version)
        info = {'version': pin['version'], 'checksum': pin['sha256'], 'path': str(cache / pin['sha256'])}
    if args.operation == 'info':
        for key, value in info.items():
            print(f'{key}={value}')
    elif args.source == 'linux':
        installer.main(args.architecture, cache, tool=args.tool, download_only=True)
    else:
        seed_uv(pin, cache, Path(os.environ['RUNNER_TOOL_CACHE']))


if __name__ == '__main__':
    main()
