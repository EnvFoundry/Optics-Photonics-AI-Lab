import json
from pathlib import Path
import shutil
import time
from datetime import datetime, timezone

from . import catalogue, environments, jobs, programs, routes


def definitions():
    return json.loads((catalogue.resources() / 'data/modules.json').read_text(encoding='utf-8'))


def catalogue_view():
    """Task modules for installation; the full software reference stays separate."""
    source = definitions()
    entries = {item['id']: item for item in catalogue.builtin_catalogue()}
    domains = {row[0]: row for row in json.loads((catalogue.resources() / 'data/domains.en.json').read_text(encoding='utf-8'))}
    chinese = {row[0]: row for row in json.loads((catalogue.resources() / 'data/domains.zh.json').read_text(encoding='utf-8'))}
    packaged, connections = programs.builtin_catalog(), catalogue.builtin_connections()
    selected_tools = {name for name, entry in entries.items() if entry['group'] == 'open'}
    for module in source['modules']:
        if not set(module['components']) <= selected_tools:
            raise ValueError('Research modules use open-source software: ' + module['id'])
    components = []
    for name in sorted(selected_tools):
        item, recipe = entries[name], connections[name]
        if item['group'] != 'open':
            raise ValueError('Research modules use open-source software: ' + name)
        manager = next((key for key in ('conda', 'pip', 'julia') if recipe.get(key)), None)
        interfaces = [{'route': route['route'], **({'platforms': routes.platforms(route)} if routes.platforms(route) else {})}
                      for route in routes.routes(name, record=recipe)]
        components.append({'id': name, 'title': item['name'],
                           'kind': 'python' if recipe.get('pip') else 'environment' if manager else 'external',
                           'requirements': recipe.get('pip', recipe.get('conda', recipe.get('julia', []))),
                           'installation': {'method': manager or 'external', 'platforms': recipe.get('platforms', []),
                                            'python': recipe.get('python'), 'note': recipe.get('installation_note', ''),
                                            'routes': interfaces},
                           'license': item['license'], 'sources': item['sources']})
    modules = []
    for module in source['modules']:
        examples = [{'tool': tool, 'name': action, 'title': spec['title'],
                     'path': 'lib/optics_photonics/programs/' + spec['file'].relative_to(programs.FOLDER).as_posix()}
                    for tool in module['components'] for action, spec in packaged.get(tool, {}).items()
                    if set(spec.get('domains', [])) & set(module['domains'])]
        modules.append({**module, 'kind': 'calculation', 'includes': [],
                        'tags': module['domains'] + module['components'] + [row['name'] for row in examples],
                        'programs': examples,
                        'resources': ['skills/optical-computation/SKILL.md'] +
                                     ['skills/optical-research/references/' + domain + '.md' for domain in module['domains']] +
                                     [example['path'] for example in examples]})
    software = []
    for name in sorted(selected_tools, key=lambda name: entries[name]['name'].casefold()):
        item, recipe = entries[name], connections[name]
        membership = list(dict.fromkeys(item['cats'] + [domain for spec in packaged.get(name, {}).values()
                                                       for domain in spec.get('domains', [])]))
        manager = next((key for key in ('conda', 'pip', 'julia') if recipe.get(key)), None)
        software.append({'id': name, 'title': item['name'], 'summary': item['summary'], 'kind': 'software',
                         'components': [name], 'includes': [], 'tags': membership + [manager or 'external'],
                         'domains': membership,
                         'note': (('Install with ' + manager + '.') if manager else 'Connect an existing installation.') +
                                 (' Recipe platforms: ' + ', '.join(recipe['platforms']) + '.' if recipe.get('platforms') else '') +
                                 (' ' + recipe['installation_note'] if recipe.get('installation_note') else ''),
                         'resources': ['skills/optical-research/references/' + domain + '.md' for domain in membership]})
    fields = [{key: value for key, value in field.items() if key != 'domains'} | {
              'directions': [{'id': domain, 'title': domains[domain][1],
                              'titles': {'en': domains[domain][1], 'zh-CN': chinese[domain][1]},
                              'summary': domains[domain][2],
                              'modules': [row['id'] for row in modules if domain in row['domains']],
                              'software': [row['id'] for row in software if domain in row['domains']]}
                             for domain in field['domains']]}
              for field in source['fields']]
    return {'fields': fields, 'modules': modules, 'software': software, 'components': components}


def selected(selections):
    if not isinstance(selections, list) or any(not isinstance(name, str) for name in selections):
        raise ValueError('selections must contain module or software IDs')
    names = list(dict.fromkeys(selections))
    modules = {row['id'] for row in definitions()['modules']}
    for name in names:
        if name not in modules:
            environments.software(name)
    return names


def components(selections):
    modules = {row['id']: row['components'] for row in definitions()['modules']}
    return list(dict.fromkeys(tool for name in selections for tool in modules.get(name, [name])))


def command(binding):
    if binding.get('kind') == 'command':
        return binding.get('command')
    executable = binding.get('executable') if binding.get('kind') == 'executable' else binding.get('python')
    return [executable] if executable else None


def invocation(name, binding):
    launch = command(binding)
    return {'component': name, 'kind': binding.get('kind'), 'command': launch,
            'location': str(Path(launch[0]).parent) if launch else None,
            'managed': bool(binding.get('managed'))}


def available(binding):
    launch = command(binding)
    return bool(launch and (Path(launch[0]).is_file() or shutil.which(launch[0])))


def installed():
    configuration = environments.read()
    bindings = configuration['tools']
    ready = [invocation(name, value) for name, value in bindings.items() if available(value)]
    ids = {row['component'] for row in ready}
    modules = configuration.get('modules', {})
    return {'modules': sorted(set(bindings) | modules.keys()),
            'ready_modules': sorted(ids | {name for name, tools in modules.items() if set(tools) <= ids}), 'ready': ready,
            'pending': [{'component': name, 'status': 'connection_needed'} for name in sorted(bindings.keys() - ids)], 'failed': []}


def plan(selections):
    names = selected(selections)
    bindings = environments.read()['tools']
    actions = []
    for name in components(names):
        if name in bindings and available(bindings[name]):
            actions.append({'component': name, 'action': 'reuse', **invocation(name, bindings[name])})
            continue
        try:
            recipe = environments.installable(name)
            actions.append({'component': name, 'action': 'install', 'requirements': recipe['pip'] or recipe['conda'] or recipe['julia'],
                            'destination': str(environments.environment_prefix(recipe['environment']))})
        except ValueError as error:
            actions.append({'component': name, 'action': 'connect', 'instructions': str(error)})
    return {'modules': names, 'platform': environments.here(), 'actions': actions, 'storage': {'root': str(environments.home()),
            'software': str(environments.software_home()),
            'download_bytes': None if any(row['action'] == 'install' for row in actions) else 0}}


def prepare(selections, python=None, packages=None, request_id=None):
    names = selected(selections)
    request = {'selections': names, 'python': python, 'packages': packages or []}
    if request_id:
        with jobs.database() as db:
            row = db.execute('SELECT payload FROM requests WHERE id=?', (request_id,)).fetchone()
        if row:
            payload = json.loads(row[0])
            if payload.get('operation') != 'environment_prepare' or payload.get('request') != request:
                raise ValueError('This request_id already belongs to different inputs')
            return jobs.reuse(request_id, payload)
    prepared = plan(names)
    steps, overrides = [], {}
    for action in prepared['actions']:
        if action['action'] == 'install' or action['action'] == 'reuse' and packages:
            step, config = jobs.setup(action['component'], python=python, packages=packages or [])
            steps.append(step)
            overrides[step['id']] = config
    payload = {'operation': 'environment_prepare', 'request': request, 'plan': prepared}
    result = jobs.submit(steps, name='Prepare selected optical software', request_id=request_id,
                         _environments=overrides, _payload=payload, _allow_empty=True)
    definitions_by_id = {row['id']: row['components'] for row in definitions()['modules']}
    chosen = {name: definitions_by_id[name] for name in names if name in definitions_by_id}
    if chosen:
        with environments.editing():
            configuration = environments._read()
            configuration.setdefault('modules', {}).update(chosen)
            environments._save(configuration)
    return result


def remove(selections):
    if not isinstance(selections, list) or any(not isinstance(name, str) for name in selections):
        raise ValueError('selections must contain module or software IDs')
    names = list(dict.fromkeys(selections))
    configuration = environments.read()
    modules = configuration.get('modules', {})
    shared = {tool for name, tools in modules.items() if name not in names for tool in tools}
    requested = components(names)
    result = environments.uninstall([tool for tool in requested if tool not in shared])
    removed = set(result['removed'])
    retained = set(result['retained'])
    for tool in requested:
        if tool in shared:
            retained.update(map(str, environments.binding_paths(configuration['tools'].get(tool, {}))))
            result.setdefault('reasons', []).append({'component': tool, 'reason': 'Used by another selected module'})
    dropped = [name for name in names if name in modules and
               all(tool in removed or tool in shared or tool not in configuration['tools'] for tool in modules[name])]
    if dropped:
        with environments.editing():
            current = environments._read()
            for name in dropped:
                if current.get('modules', {}).get(name) == modules[name]:
                    del current['modules'][name]
            environments._save(current)
    result['removed'] = sorted((removed & set(names)) | set(dropped))
    result['retained'] = sorted(retained)
    return result


def request_plan(run_id, *, required=True):
    with jobs.database() as db:
        rows = db.execute('SELECT payload FROM requests WHERE run_id=?', (run_id,)).fetchall()
    for row in rows:
        payload = json.loads(row[0])
        if payload.get('operation') == 'environment_prepare':
            return payload['plan']
    if required:
        raise ValueError('Unknown environment preparation task')
    return None


def outcome(run_id):
    prepared = request_plan(run_id)
    state = jobs.status(run_id, limit=200)
    steps = {item['id']: item for item in state['steps']}
    ready, pending, failed = [], [], []
    for action in prepared['actions']:
        name = action['component']
        step = steps.get('prepare.' + name)
        if step and step['state'] == 'succeeded':
            ready.append(invocation(name, step['result']['result']))
        elif step and step['state'] in {'failed', 'blocked', 'interrupted'}:
            failed.append({'component': name, 'error': step['error'] or step['state']})
        elif step:
            pending.append({'component': name, 'status': step['state']})
        elif action['action'] == 'reuse':
            ready.append({key: value for key, value in action.items() if key != 'action'})
        else:
            pending.append(action)
    return {'modules': prepared['modules'], 'ready': ready, 'pending': pending, 'failed': failed}


def task(run_id, starting=False):
    value = jobs.status(run_id, limit=200, include_results=False)
    status = 'working' if starting or value['state'] not in jobs.TERMINAL else (
        'completed' if value['state'] == 'succeeded' else 'cancelled' if value['state'] == 'cancelled' else 'failed')
    stamp = lambda moment: datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    result = {'taskId': run_id, 'status': status, 'createdAt': stamp(value['created']),
              'lastUpdatedAt': stamp(value['finished'] or value['created']), 'ttl': None, 'pollInterval': 1000}
    errors = [step['error'] for step in value['steps'] if step.get('error')]
    if errors:
        result['statusMessage'] = '; '.join(errors)
    return result


def task_rpc(method, task_id, cancellation=None):
    value = task(task_id)
    if method == 'tasks/get':
        return value
    if method == 'tasks/cancel':
        if value['status'] != 'working':
            raise ValueError('The task is already terminal')
        jobs.cancel(task_id)
    while (value := task(task_id))['status'] == 'working':
        if cancellation and cancellation():
            raise InterruptedError('The task request was disconnected; work continues')
        time.sleep(.1)
    if method == 'tasks/cancel':
        return value
    result = outcome(task_id) if request_plan(task_id, required=False) is not None else jobs.status(task_id)
    return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
            'structuredContent': result, 'isError': value['status'] != 'completed',
            '_meta': {'io.modelcontextprotocol/related-task': {'taskId': task_id}}}
