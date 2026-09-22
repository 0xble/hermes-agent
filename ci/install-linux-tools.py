#!/usr/bin/env python3
"""Image-build only: install verified, architecture-specific CI tool binaries."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent


def extract_archive(source: tarfile.TarFile, destination: Path) -> None:
    """Validate pinned tool archives before Python 3.11.2's standard extractor.

    Extraction filters were added after 3.11.2. Explicit path/link checks keep
    the same boundary without requiring that newer keyword argument.
    """
    destination.mkdir()
    root = destination.resolve()
    members = {}
    for member in source.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or '\\' in member.name or not path.parts:
            raise RuntimeError(f'Unsafe archive member: {member.name}')
        name = path.as_posix()
        if name in members or not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise RuntimeError(f'Duplicate or unsupported archive member: {member.name}')
        members[name] = member

    # Validate the entire archive before writing any content. No archive link can
    # become the parent of another member, regardless of archive entry ordering.
    for name, member in members.items():
        path = PurePosixPath(name)
        for parent in path.parents:
            ancestor = members.get(parent.as_posix())
            if ancestor is not None and not ancestor.isdir():
                raise RuntimeError(f'Archive member has a non-directory ancestor: {name}')
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or '\\' in member.linkname or not member.linkname:
                raise RuntimeError(f'Unsafe archive link: {name}')
            target = (root / path.parent / member.linkname) if member.issym() else (root / member.linkname)
            if not target.resolve().is_relative_to(root):
                raise RuntimeError(f'Archive link escapes extraction root: {name}')
            if member.islnk():
                linked_member = members.get(link.as_posix())
                if linked_member is None or not linked_member.isfile():
                    raise RuntimeError(f'Archive hard link must target an ordinary file: {name}')

    for member in members.values():
        member.mode = (member.mode & 0o755) | 0o600
        member.uid = member.gid = 0
        member.uname = member.gname = ''
    source.extractall(root, members=members.values(), numeric_owner=True)
    # A chain can change how '..' resolves once all symlinks exist. No writes
    # traversed links above. Reject escaping/cyclic chains before using binaries.
    for name, member in members.items():
        if member.issym() and not (root / name).resolve().is_relative_to(root):
            raise RuntimeError(f'Archive link chain escapes extraction root: {name}')


def main(architecture: str) -> None:
    artifacts = json.loads((HERE / 'linux-artifacts.json').read_text(encoding='utf-8'))[architecture]
    for artifact in artifacts:
        with tempfile.TemporaryDirectory(prefix='hermes-ci-tool-') as directory:
            scratch = Path(directory)
            archive = scratch / 'download'
            with urllib.request.urlopen(artifact['url'], timeout=120) as response, archive.open('wb') as target:
                shutil.copyfileobj(response, target)
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != artifact['sha256']:
                raise RuntimeError(f"Checksum mismatch: {artifact['url']}")
            kind = artifact['kind']
            if kind == 'hadolint':
                target = Path('/usr/local/bin/hadolint')
                shutil.copy2(archive, target)
                target.chmod(0o755)
                continue
            unpacked = scratch / 'unpacked'
            with tarfile.open(archive) as source:
                extract_archive(source, unpacked)
            if kind == 'rust':
                installers = list(unpacked.glob('*/install.sh'))
                if len(installers) != 1:
                    raise RuntimeError('Expected exactly one Rust component installer')
                subprocess.run(['sh', str(installers[0]), '--prefix=/opt/ci/rust', '--disable-ldconfig'], check=True)
            else:
                binaries = list(unpacked.glob(f'*/{kind}'))
                if len(binaries) != 1:
                    raise RuntimeError(f'Expected exactly one {kind} binary')
                shutil.copy2(binaries[0], Path('/usr/local/bin') / kind)


if __name__ == '__main__':
    main(sys.argv[1])
