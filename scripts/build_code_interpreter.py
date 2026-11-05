"""Build pinned upstream components on the cluster host; publish via existing Harbor helper."""
import io, json, os, shlex, subprocess, tarfile
from pathlib import Path
from cluster_transport import remote, HOST, SOCKET

root=Path(__file__).resolve().parents[1]
source=root.parent/'code-interpreter-upstream'
revision=subprocess.check_output(['git','-c','safe.directory='+str(source),'-C',str(source),'rev-parse','HEAD'],text=True).strip()
if revision!='9a3f5dbd6fedf42462239d135c2c25ec8e737628':raise RuntimeError('Unreviewed upstream revision')
tag=os.environ.get('CODEAPI_BUILD_TAG','codeapi-20261105.1')
if not all(c in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in tag):raise ValueError('tag')
archive=root/'release/codeapi-source.tar.gz'
nsjail=root/'release/nsjail'
if not nsjail.exists():
 subprocess.run(['git','clone','--filter=blob:none','--no-checkout','https://github.com/google/nsjail.git',str(nsjail)],check=True)
 subprocess.run(['git','-C',str(nsjail),'checkout','66ad78dc361721512fbf9470488e584364c75489'],check=True)
 subprocess.run(['git','-C',str(nsjail),'submodule','update','--init','--recursive'],check=True)
for path,commit in [(nsjail,'66ad78dc361721512fbf9470488e584364c75489'),(nsjail/'kafel','76d0f41bf3eb5c4008713d64b9767b461a9129a3')]:
 if subprocess.check_output(['git','-c','safe.directory='+str(path),'-C',str(path),'rev-parse','HEAD'],text=True).strip()!=commit:raise RuntimeError('Unreviewed NsJail dependency')
with tarfile.open(archive,'w:gz') as tar:
 for path in source.rglob('*'):
  if path.is_file() and '.git' not in path.relative_to(source).parts:
   name=str(path.relative_to(source))
   if name.replace('\\','/')=='api/Dockerfile':
    content=path.read_text();start=content.index('RUN git clone');end=content.index('RUN make',start)
    payload=(content[:start]+'COPY vendor/nsjail /nsjail\n\n'+content[end:]).encode()
    info=tarfile.TarInfo('api/Dockerfile');info.size=len(payload);tar.addfile(info,io.BytesIO(payload))
   else:tar.add(path,arcname=name)
 for path in nsjail.rglob('*'):
  if path.is_file() and '.git' not in path.relative_to(nsjail).parts:tar.add(path,arcname='vendor/nsjail/'+str(path.relative_to(nsjail)))
directory='/home/edge/platform-releases/'+tag
remote(['mkdir','-p',directory+'/context'])
with archive.open('rb') as stream:subprocess.run(['ssh','-S',SOCKET,HOST,'cat > '+directory+'/source.tar.gz'],stdin=stream,check=True)
remote(['tar','-xzf',directory+'/source.tar.gz','-C',directory+'/context'])
remote(['tee',directory+'/push.py'],(root/'scripts/push_on_host.py').read_text())
commands=['cd '+directory+'/context']
for name,target in [('api','api'),('worker','worker'),('files','production'),('egress','egress-gateway')]:
 image_tag=tag+'-'+name
 commands.append('docker build --network=host --provenance=false -f service/Dockerfile --target '+target+' -t platform-control:'+image_tag+' .')
 commands.append('PLATFORM_IMAGE_TAG='+image_tag+' python3 '+directory+'/push.py > '+directory+'/'+name+'.json')
commands.append('docker build --network=host --provenance=false -f service/Dockerfile.tool-call-server --target production -t platform-control:'+tag+'-tools .')
commands.append('PLATFORM_IMAGE_TAG='+tag+'-tools python3 '+directory+'/push.py > '+directory+'/tools.json')
commands.append('docker build --network=host --provenance=false -f api/Dockerfile --target sandbox-runner-baked -t platform-control:'+tag+'-runner .')
commands.append('PLATFORM_IMAGE_TAG='+tag+'-runner python3 '+directory+'/push.py > '+directory+'/runner.json')
pid=remote(['/bin/sh','-c','nohup /bin/sh -ec '+shlex.quote(' && '.join(commands))+' > '+directory+'/build.log 2>&1 < /dev/null & echo $!']).strip()
print(json.dumps({'revision':revision,'build_pid':pid,'log':directory+'/build.log'}))
