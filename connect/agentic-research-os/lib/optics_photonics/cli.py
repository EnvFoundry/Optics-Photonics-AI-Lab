"""One entry point for people, shell agents and native MCP hosts."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
from . import __version__, catalogue, environments, execution, jobs


def object_argument(value):
    return json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)


def main(argv=None):
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog='optics-photonics', description='Optical software and research connections')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    search = commands.add_parser('search'); search.add_argument('query', nargs='?', default=''); search.add_argument('--domain'); search.add_argument('--license-group', choices=('open','paid','limited')); search.add_argument('--limit',type=int,default=20); search.add_argument('--offset',type=int,default=0); search.add_argument('--language',choices=('zh','en'),default='en')
    info = commands.add_parser('info'); info.add_argument('tool'); info.add_argument('--language',choices=('zh','en'),default='en'); info.add_argument('--program', help='print the source of this packaged program')
    register = commands.add_parser('register-software'); register.add_argument('definition', type=object_argument)
    commands.add_parser('registered-software')
    integrations = commands.add_parser('integrations', help='installed software packages and their guides')
    integrations.add_argument('integration', nargs='?'); integrations.add_argument('topic', nargs='?')
    integrations.add_argument('--language', choices=('en','zh'), default='en'); integrations.add_argument('--refresh', action='store_true')
    env = commands.add_parser('environments')
    discovery = commands.add_parser('discover', help='find installed software and how to bind it'); discovery.add_argument('--root', dest='roots', action='append'); discovery.add_argument('--tool')
    native_bind = commands.add_parser('bind-native', help='connect installed software by a route'); native_bind.add_argument('tool'); native_bind.add_argument('--route', choices=('session','executable','automation','python')); native_bind.add_argument('--executable'); native_bind.add_argument('--progid'); native_bind.add_argument('--project'); native_bind.add_argument('--language'); native_bind.add_argument('--python'); native_bind.add_argument('--sdk-path'); native_bind.add_argument('--options', type=object_argument, default={}); native_bind.add_argument('--threads', type=int, default=1); native_bind.add_argument('--concurrency', type=int, default=1); native_bind.add_argument('--requirements', type=object_argument); native_bind.add_argument('--env', type=object_argument)
    bind = commands.add_parser('bind'); bind.add_argument('tool', nargs='?', help='omit to bind every tool the interpreter provides'); choice=bind.add_mutually_exclusive_group(required=True); choice.add_argument('--python'); choice.add_argument('--command-json',type=object_argument); bind.add_argument('--threads',type=int,default=1); bind.add_argument('--concurrency',type=int,default=1); bind.add_argument('--cwd'); bind.add_argument('--requirements',type=object_argument); bind.add_argument('--env',type=object_argument)
    prep = commands.add_parser('prepare'); prep.add_argument('tool'); prep.add_argument('--python'); prep.add_argument('--package', dest='packages', action='append', help='an extra requirement; repeat for more')
    for name in ('run','submit'):
        run = commands.add_parser(name); run.add_argument('tool'); run.add_argument('action',nargs='?',default='',help='a packaged program listed by info'); run.add_argument('--inputs',type=object_argument,default={}); run.add_argument('--program',type=Path,help='a Python program'); run.add_argument('--script',type=Path,help='a program in the language of software bound to its executable')
        if name == 'run': run.add_argument('--output'); run.add_argument('--timeout',type=float)
        else:
            run.add_argument('--name',default='Optical research'); run.add_argument('--request-id'); run.add_argument('--timeout',type=float)
            run.add_argument('--sweep',type=object_argument,help='JSON: input -> values or {start, stop, points}'); run.add_argument('--pairing',choices=('product','zip'),default='product'); run.add_argument('--chunk',type=int,help='points per process'); run.add_argument('--run-id'); run.add_argument('--wait',type=float,default=0,help='seconds to wait for the result, up to 25')
        run.add_argument('--optimize',type=object_argument,help='JSON: {variables, minimize|maximize, method, evaluations}')
    sample = commands.add_parser('sample'); sample.add_argument('tool'); sample.add_argument('program', type=Path)
    sample.add_argument('--task', type=object_argument, required=True); sample.add_argument('--policy', type=object_argument, required=True)
    sample.add_argument('--episodes', type=int, required=True); sample.add_argument('--workers', type=int, default=1)
    sample.add_argument('--max-steps', type=int, default=1000); sample.add_argument('--seed', type=int, default=0)
    sample.add_argument('--inputs', type=object_argument, default={}); sample.add_argument('--options', type=object_argument, default={})
    sample.add_argument('--continue-from', nargs='*', default=[], metavar='RUN_ID'); sample.add_argument('--on-error', choices=['stop','continue'], default='stop')
    sample.add_argument('--request-id'); sample.add_argument('--timeout', type=float)
    resources=commands.add_parser('resources'); resources.add_argument('--cpu',type=int); resources.add_argument('--pools',type=object_argument)
    workflow=commands.add_parser('submit-workflow'); workflow.add_argument('file',type=Path,help='JSON file containing a top-level array of steps; each step needs id and tool'); workflow.add_argument('--name',default='Optics research'); workflow.add_argument('--request-id'); workflow.add_argument('--run-id')
    for name in ('status','files','cancel','resume'):
        item=commands.add_parser(name); item.add_argument('run_id')
        if name in ('status','files'):
            item.add_argument('--offset',type=int,default=0); item.add_argument('--limit',type=int,default=50 if name=='status' else 100)
        if name == 'files': item.add_argument('--prefix',default='')
        if name == 'status':
            item.add_argument('--wait',type=float,default=0); item.add_argument('--summary',dest='include_results',action='store_false')
    download=commands.add_parser('download'); download.add_argument('run_id'); download.add_argument('file'); download.add_argument('destination')
    array=commands.add_parser('array',help='Read part of an NPY array or NPZ member'); array.add_argument('run_id'); array.add_argument('file'); array.add_argument('--member'); array.add_argument('--index',type=json.loads,help='JSON list, e.g. [10, [0, 100, 2]]'); array.add_argument('--max-elements',type=int); array.add_argument('--image',metavar='PNG',type=Path,help='draw the selection to this PNG file'); array.add_argument('--kind',choices=('auto','line','map'),default='auto'); array.add_argument('--part',choices=('abs','real','imag','phase','power')); array.add_argument('--scale',choices=('linear','log'),default='linear')
    read=commands.add_parser('read'); read.add_argument('run_id'); read.add_argument('file'); read.add_argument('--offset',type=int,default=0); read.add_argument('--limit',type=int,default=16384)
    material=commands.add_parser('materials', help='n and k of a material from the refractiveindex.info database'); material.add_argument('material', nargs='?'); material.add_argument('--query'); material.add_argument('--wavelength', type=float, nargs='+'); material.add_argument('--unit', default='nm'); material.add_argument('--limit', type=int, default=20); material.add_argument('--dispersion', action='store_true', help='also the group index, GVD and D')
    sweep=commands.add_parser('sweep',help='Tabulate the points of a sweep'); sweep.add_argument('run_id'); sweep.add_argument('--value',dest='values',action='append',help='dotted RESULT path; repeat for more'); best=sweep.add_mutually_exclusive_group(); best.add_argument('--minimize'); best.add_argument('--maximize'); sweep.add_argument('--save',action='store_true'); sweep.add_argument('--offset',type=int,default=0); sweep.add_argument('--limit',type=int,default=100)
    runs=commands.add_parser('runs'); runs.add_argument('--state'); runs.add_argument('--offset',type=int,default=0); runs.add_argument('--limit',type=int,default=20)
    runs.add_argument('--search'); runs.add_argument('--tool'); runs.add_argument('--before')
    commands.add_parser('mcp')
    commands.add_parser('installed')
    for name in ('plan', 'prepare-modules', 'uninstall'):
        item = commands.add_parser(name)
        item.add_argument('selections', nargs='*')
        if name == 'prepare-modules':
            item.add_argument('--python')
            item.add_argument('--package', dest='packages', action='append')
            item.add_argument('--request-id')
    commands.add_parser('tools')
    call=commands.add_parser('call'); call.add_argument('tool'); call.add_argument('arguments',type=object_argument)
    from .packages import HOSTS
    connect=commands.add_parser('connect'); connect.add_argument('host',choices=HOSTS); connect.add_argument('--output',required=True); connect.add_argument('--python'); connect.add_argument('--managed-python',dest='managed',action='store_true',help='Use uv to select Python on the destination machine (Codex, Claude, portable)')
    a=vars(parser.parse_args(argv)); command=a.pop('command')
    try:
        if command in ('installed', 'plan', 'prepare-modules', 'uninstall'):
            from . import composition
            result = getattr(composition, {'prepare-modules': 'prepare', 'uninstall': 'remove'}.get(command, command))(**a)
        elif command == 'search': result=catalogue.search(**a)
        elif command == 'materials':
            from . import materials
            if a['wavelength'] and len(a['wavelength']) == 1: a['wavelength'] = a['wavelength'][0]
            result=materials.lookup(**a)
        elif command == 'info':
            result=environments.details(**a, source=bool(a['program']))
            if a['program']: print(result['source'], end=''); return
        elif command == 'register-software': result=environments.register(**a['definition'])
        elif command == 'registered-software': result={'software':environments.registered()}
        elif command == 'integrations':
            from .mcp import invoke
            result=invoke('software_integrations', a)
        elif command == 'environments': result={'environments':environments.available()}
        elif command in ('discover', 'bind-native'):
            from . import native
            result = native.discover(**a) if command == 'discover' else native.bind(**a)
        elif command == 'bind': a['command']=a.pop('command_json'); result=environments.bind(**a)
        elif command == 'prepare': result=environments.prepare(**a)
        elif command in ('run','submit'):
            source=a.pop('program'); source=source.read_text(encoding='utf-8') if source else ''
            text=a.pop('script'); text=text.read_text(encoding='utf-8') if text else ''
            request={k:a.pop(k) for k in ('inputs','action')}; request['source']=source
            if text: request['script']=text
            tool=a.pop('tool')
            if command=='run':
                optimize=a.pop('optimize')
                if optimize:
                    from . import optimization
                    optimization.checked(optimize); request['optimize']=optimize
                if request['action'] and not source: jobs.packaged({'tool':tool,**request})
                result=execution.run(tool,request,**a)
            else:
                from . import studies
                result=studies.submit(tool,**request,**a)
        elif command == 'sample':
            from . import sampling
            a['source'] = a.pop('program').read_text(encoding='utf-8'); result = sampling.submit(**a)
        elif command == 'sweep':
            from . import studies
            result=studies.table(**a)
        elif command == 'resources': result=jobs.resources(**a)
        elif command == 'runs': result=jobs.list_runs(**a)
        elif command == 'submit-workflow':
            file=a.pop('file'); steps=json.loads(file.read_text(encoding='utf-8'))
            for step in steps if isinstance(steps,list) else []:
                # A step may name its program file beside the workflow file instead of carrying the source text.
                if isinstance(step,dict) and 'program' in step: step['source']=(file.parent/step.pop('program')).read_text(encoding='utf-8')
                if isinstance(step,dict) and isinstance(step.get('script'),str) and '\n' not in step['script']:
                    script_file=file.parent/step['script']
                    try: is_file=script_file.is_file()
                    except (OSError, ValueError): is_file=False  # inline scripts need not be valid filesystem paths
                    if is_file: step['script']=script_file.read_text(encoding='utf-8')
            result=jobs.submit(steps,**a)
        elif command == 'array':
            target, kind = a.pop('image'), a.pop('kind')
            result=jobs.read_array(**a, image=kind if target else None)
            if target:
                target.write_bytes(result.pop('_image')); result['image']=str(target)
        elif command in ('status','files','cancel','resume','download','read'):
            result=getattr(jobs, {'read':'read_result'}.get(command,command))(**a)
        elif command == 'connect':
            from .packages import package
            result=package(a.pop('output'),**a)
        elif command in ('mcp','tools','call'):
            from .mcp import serve, invoke, listed
            if command=='mcp': serve(); return
            if command=='tools': result={'tools':listed()}
            else: result=invoke(a['tool'],a['arguments'])
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1) from None
