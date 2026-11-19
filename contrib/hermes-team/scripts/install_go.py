"""Run inside the VPN-isolated Hermes container; verify official Go checksum."""
import hashlib,json,os,platform,tarfile,tempfile,urllib.request
from pathlib import Path
root=Path('/home/alex/.local'); dest=root/'go'; bindir=root/'bin'
if (dest/'bin/go').exists():
 print('Go already installed');raise SystemExit(0)
arch={'x86_64':'amd64','aarch64':'arm64'}[platform.machine()]
releases=json.load(urllib.request.urlopen('https://go.dev/dl/?mode=json',timeout=30))
release=next(r for r in releases if r.get('stable'))
f=next(f for f in release['files'] if f['os']=='linux' and f['arch']==arch and f['kind']=='archive')
root.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(dir=root) as temp:
 archive=Path(temp)/f['filename']
 with urllib.request.urlopen('https://go.dev/dl/'+f['filename'],timeout=120) as source,archive.open('wb') as out:
  while chunk:=source.read(1024*1024):out.write(chunk)
 if hashlib.sha256(archive.read_bytes()).hexdigest()!=f['sha256']:raise RuntimeError('Go checksum mismatch')
 with tarfile.open(archive) as tar:tar.extractall(root,filter='data')
bindir.mkdir(exist_ok=True)
for name in ('go','gofmt'):
 p=bindir/name
 if not p.exists():p.symlink_to(dest/'bin'/name)
print('Installed',release['version'],'linux/'+arch,'official SHA256 verified')
