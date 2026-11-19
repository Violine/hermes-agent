"""Explicit operator configuration; no model call and no credential values in output."""
import argparse
import json
from pathlib import Path
from .controller import read_json, write_json, file_lock, git, require_clean
from .contracts import safe_id, ContractError
from .install_profiles import default_role_configurations, install_profiles
from .project_context import inspect_project_context, write_project_context, validate_context_settings


def apply_profiles(config):
    return install_profiles(config['hermes_root'], config['roles'],
        role_docs_dir=Path(config['bundle_root']) / 'roles',
        common_instructions=Path(config['bundle_root']) / 'roles/COMMON.md')


def main():
    p = argparse.ArgumentParser(description='Configure Hermes engineering team')
    p.add_argument('--config', default='/home/alex/.hermes/team/config.json')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--root', default='/home/alex/.hermes')
    init.add_argument('--provider', default='kimi-coding')
    init.add_argument('--model', default='kimi-k2.6')
    sub.add_parser('install-profiles')
    sub.add_parser('show')
    pool = sub.add_parser('set-pool')
    pool.add_argument('name')
    pool.add_argument('--file', required=True, help='JSON pool definition, no secret values')
    candidates = sub.add_parser('set-candidates')
    candidates.add_argument('role')
    candidates.add_argument('--file', required=True, help='JSON array of explicitly permitted model candidates')
    role = sub.add_parser('set-role')
    role.add_argument('role')
    role.add_argument('--provider')
    role.add_argument('--model')
    role.add_argument('--effort', choices=['none','minimal','low','medium','high','xhigh','max','ultra'])
    role.add_argument('--budget', type=int)
    role.add_argument('--credential-env', action='append', help='Explicit allowed env key name, never its value')
    project = sub.add_parser('add-project')
    project.add_argument('name')
    project.add_argument('--repo', required=True)
    project.add_argument('--base', default='main')
    project.add_argument('--checks-json', required=True, help='JSON array of trusted argv arrays')
    project.add_argument('--timeout', type=int, default=180)
    project.add_argument('--context-file', default='.agents/project.json')
    project.add_argument('--context-required', action='store_true')
    context = sub.add_parser('project-context', help='Inspect committed project documentation; does not run checks')
    context.add_argument('name')
    context.add_argument('--write', metavar='DIRECTORY', help='Save an immutable context snapshot outside the checkout')
    a = p.parse_args()
    path = Path(a.config)
    with file_lock(str(path) + '.lock'):
        if a.command == 'init':
            if path.exists():
                raise ContractError('Configuration already exists; use set-role/add-project')
            root = Path(a.root).resolve()
            cfg = {'schema_version':1,'hermes_root':str(root),'board':'engineering-team',
                   'bundle_root':str(Path(__file__).resolve().parent.parent),
                   'data_root':str(root/'team/data'),'poll_seconds':5,'max_active_features':1,
                   'auto_start_after_judge_accept':False,
                   'max_plan_tasks':12,'max_rework_rounds':2,'max_plan_corrections':2,
                   'quota':{'pools':{'kimi-main':{'provider':'kimi-coding','enabled':True,
                       'auth_ready':True,'max_concurrent':2,'reserve_percent':20,
                       'max_snapshot_age':180,'unknown_policy':'wait','probe':'kimi'}}},
                   'roles':default_role_configurations(a.provider,a.model),'projects':{}}
            apply_profiles(cfg)
            write_json(path,cfg)
            print('Initialized 13 role profiles and configuration')
        else:
            cfg=read_json(path)
            if a.command == 'show':
                print(json.dumps(cfg,ensure_ascii=False,indent=2))
            elif a.command == 'install-profiles':
                print(json.dumps(apply_profiles(cfg),ensure_ascii=False,indent=2))
            elif a.command == 'project-context':
                name = safe_id(a.name)
                if name not in cfg['projects']:
                    raise ContractError('Unknown project')
                project_cfg = cfg['projects'][name]
                result = inspect_project_context(name, project_cfg)
                if not result['ready']:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    raise SystemExit(1)
                if a.write:
                    destination = Path(a.write).expanduser().resolve()
                    repo = Path(project_cfg['repo']).resolve()
                    if destination == repo or repo in destination.parents:
                        raise ContractError('Context snapshots must be stored outside the project checkout')
                    result = write_project_context(name, project_cfg, result['revision'], destination)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if not result['ready']:
                    raise SystemExit(1)
            elif a.command == 'set-role':
                name=a.role if a.role.startswith('team-') else 'team-'+a.role
                if name not in cfg['roles']: raise ContractError('Unknown team role')
                r=cfg['roles'][name]
                if r.get('candidates') and any((a.provider, a.model, a.effort)):
                    raise ContractError('Role has routing candidates; use set-candidates to change provider/model/effort')
                for arg,key in [('provider','provider'),('model','model'),('effort','reasoning_effort'),('budget','run_budget')]:
                    value=getattr(a,arg)
                    if value is not None: r[key]=value
                if a.credential_env is not None: r['credential_env_vars']=a.credential_env
                if a.provider and a.credential_env is None and a.provider != 'kimi-coding':
                    r['credential_env_vars']=[]
                install_profiles(cfg['hermes_root'],{name:r},role_docs_dir=Path(cfg['bundle_root'])/'roles',
                                 common_instructions=Path(cfg['bundle_root'])/'roles/COMMON.md')
                write_json(path,cfg)
                print(json.dumps({name:{k:r[k] for k in ['provider','model','reasoning_effort','run_budget']}},ensure_ascii=False))
            elif a.command == 'set-pool':
                from .quota import QuotaManager
                name = safe_id(a.name)
                cfg.setdefault('quota',{}).setdefault('pools',{})[name] = read_json(a.file)
                QuotaManager(cfg['quota'],Path(cfg['data_root'])/'quota.json')
                write_json(path,cfg)
                print('Configured quota pool '+name)
            elif a.command == 'set-candidates':
                from .quota import QuotaManager
                name=a.role if a.role.startswith('team-') else 'team-'+a.role
                if name not in cfg['roles']: raise ContractError('Unknown team role')
                cfg['roles'][name]['candidates']=read_json(a.file)
                manager=QuotaManager(cfg['quota'],Path(cfg['data_root'])/'quota.json')
                manager._candidate_list(cfg['roles'][name])
                write_json(path,cfg)
                print('Configured explicit candidates for '+name)
            elif a.command == 'add-project':
                name=safe_id(a.name); repo=Path(a.repo).resolve(); require_clean(repo)
                git(repo,'rev-parse',a.base+'^{commit}')
                checks=json.loads(a.checks_json)
                if not isinstance(checks,list) or not checks or any(not isinstance(c,list) or not c or any(not isinstance(v,str) or not v for v in c) for c in checks):
                    raise ContractError('Checks must be nonempty argv arrays')
                if a.timeout < 1: raise ContractError('Timeout must be positive')
                settings = validate_context_settings({'context_file': a.context_file,
                                                      'context_required': a.context_required})
                cfg['projects'][name]={'repo':str(repo),'base_ref':a.base,'checks':checks,
                                      'check_timeout':a.timeout, **settings}
                write_json(path,cfg);print('Registered project '+name)

if __name__=='__main__': main()
