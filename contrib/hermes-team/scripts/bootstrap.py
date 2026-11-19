"""Bootstrap only inside the protected Hermes container after copying the bundle."""
import datetime,json,os,shutil,subprocess,sys
from pathlib import Path
import yaml
root=Path('/home/alex/.hermes'); bundle=root/'team/runtime'
sys.path.insert(0,str(bundle))
from team_engine.configure import main
from team_engine.install_profiles import default_role_configurations
from team_engine.controller import read_json,write_json
ctl=bundle/'scripts/teamctl'
config=root/'team/config.json'
if not config.exists():subprocess.run([str(ctl),'config','init'],check=True)
else:
 cfg=read_json(config)
 defaults=default_role_configurations('kimi-coding','kimi-k2.6')
 for name,settings in defaults.items():cfg['roles'].setdefault(name,settings)
 cfg.setdefault('max_plan_corrections',2)
 cfg.setdefault('quota',{'pools':{'kimi-main':{'provider':'kimi-coding','enabled':True,
     'auth_ready':True,'max_concurrent':2,'reserve_percent':20,
     'max_snapshot_age':180,'unknown_policy':'wait'}}})
 write_json(config,cfg)
 subprocess.run([str(ctl),'config','install-profiles'],check=True,stdout=subprocess.DEVNULL)
repo=Path('/home/alex/workspaces/team-demo')
if not repo.exists():
 shutil.copytree(bundle/'examples/demo',repo)
 subprocess.run(['git','init','-b','main',str(repo)],check=True,stdout=subprocess.DEVNULL)
 for key,value in [('user.name','Hermes Engineering Team'),('user.email','hermes-team@localhost')]:
  subprocess.run(['git','-C',str(repo),'config',key,value],check=True)
 subprocess.run(['git','-C',str(repo),'add','.'],check=True)
 subprocess.run(['git','-C',str(repo),'commit','-m','Initialize isolated Go team demo'],check=True,stdout=subprocess.DEVNULL)
checks=[['go','test','./...'],['go','test','-race','./...'],['go','vet','./...']]
subprocess.run([str(ctl),'config','add-project','demo','--repo',str(repo),'--checks-json',json.dumps(checks),'--timeout','240'],check=True)
skill=root/'skills/engineering-team'
if skill.exists() and not (skill/'.hermes-team-managed').exists():
 raise RuntimeError('Unmanaged engineering-team skill exists')
skill.mkdir(parents=True,exist_ok=True)
shutil.copy2(bundle/'skills/engineering-team/SKILL.md',skill/'SKILL.md')
(skill/'.hermes-team-managed').write_text('Installed by hermes-team bootstrap\n')
cfgpath=root/'config.yaml';cfg=yaml.safe_load(cfgpath.read_text())
kanban=cfg.setdefault('kanban',{})
settings={'max_in_progress':3,'max_in_progress_per_profile':1,'dispatch_interval_seconds':5}
if any(kanban.get(k)!=v for k,v in settings.items()):
 backup=root/'team/private-backups'/('config-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.yaml')
 backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(cfgpath,backup);backup.chmod(0o600)
 kanban.update(settings)
 tmp=cfgpath.with_name('.config.team.tmp');tmp.write_text(yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True));tmp.chmod(0o600);os.replace(tmp,cfgpath)
print('Bootstrap complete: profiles, skill, demo, native dispatch limits')
