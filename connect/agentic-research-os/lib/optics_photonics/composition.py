import json
from pathlib import Path
import shutil
import time
from datetime import datetime, timezone

from . import catalogue, environments, jobs


def catalogue_view():
    descriptions = {item['id']: item for item in catalogue.builtin_catalogue('zh')}
    domains = json.loads((catalogue.resources() / 'data/domains.en.json').read_text(encoding='utf-8'))
    chinese = {row[0]: row for row in json.loads((catalogue.resources() / 'data/domains.zh.json').read_text(encoding='utf-8'))}
    entries = catalogue.builtin_catalogue()
    directions = [{'id': key, 'title': title, 'titles': {'en': title, 'zh-CN': chinese[key][1]},
                   'summary': summary, 'modules': [row['id'] for row in entries if key in row['cats']]}
                  for key, title, summary in domains]
    components = []
    for item in entries:
        recipe = catalogue.builtin_connections()[item['id']]
        components.append({'id': item['id'], 'title': item['name'],
                           'kind': 'python' if recipe.get('pip') else 'environment' if recipe.get('conda') or recipe.get('julia') else 'external',
                           'requirements': recipe.get('pip', recipe.get('conda', recipe.get('julia', []))),
                           'license': item['license'], 'sources': item['sources']})
    return {'fields': [{'id': 'optics', 'title': 'Optics & photonics',
                        'titles': {'en': 'Optics & photonics', 'zh-CN': '光学与光子学'}, 'directions': directions}],
            'modules': [{'id': item['id'], 'title': item['name'], 'kind': 'software',
                         'titles': {'en': item['name'], 'zh-CN': descriptions[item['id']]['name']},
                         'summary': item['summary'],
                         'summaries': {'en': item['summary'], 'zh-CN': descriptions[item['id']]['summary']},
                         'components': [item['id']], 'includes': []} for item in entries],
            'components': components}


def selected(selections):
    if not isinstance(selections, list) or any(not isinstance(name, str) for name in selections):
        raise ValueError('selections must contain software IDs')
    names = list(dict.fromkeys(selections))
    for name in names:
        environments.software(name)
    return names


def invocation(name, binding):
    executable = binding.get('python') or binding.get('executable')
    command = [executable] if executable else binding.get('command')
    return {'component': name, 'kind': binding.get('kind'), 'command': command,
            'location': str(Path(executable).parent) if executable else None,
            'managed': bool(binding.get('managed'))}


def available(binding):
    executable = binding.get('python') or binding.get('executable')
    command = [executable] if executable else binding.get('command')
    return bool(command and (Path(command[0]).is_file() or shutil.which(command[0])))


def installed():
    bindings = environments.read()['tools']
    ready = [invocation(name, value) for name, value in bindings.items() if available(value)]
    ids = {row['component'] for row in ready}
    return {'modules': sorted(bindings), 'ready_modules': sorted(ids), 'ready': ready,
            'pending': [{'component': name, 'status': 'connection_needed'} for name in sorted(bindings.keys() - ids)], 'failed': []}


def plan(selections):
    names = selected(selections)
    bindings = environments.read()['tools']
    actions = []
    for name in names:
        if name in bindings and available(bindings[name]):
            actions.append({'component': name, 'action': 'reuse', **invocation(name, bindings[name])})
            continue
        try:
            recipe = environments.installable(name)
            actions.append({'component': name, 'action': 'install', 'requirements': recipe['pip'] or recipe['conda'] or recipe['julia'],
                            'destination': str(environments.home() / 'environments' / recipe['environment'])})
        except ValueError as error:
            actions.append({'component': name, 'action': 'connect', 'instructions': str(error)})
    return {'modules': names, 'actions': actions, 'storage': {'root': str(environments.home()),
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
    return jobs.submit(steps, name='Prepare selected optical software', request_id=request_id,
                       _environments=overrides, _payload=payload, _allow_empty=True)


def remove(selections):
    return environments.uninstall(selections)


def request_plan(run_id):
    with jobs.database() as db:
        rows = db.execute('SELECT payload FROM requests WHERE run_id=?', (run_id,)).fetchall()
    for row in rows:
        payload = json.loads(row[0])
        if payload.get('operation') == 'environment_prepare':
            return payload['plan']
    raise ValueError('Unknown environment preparation task')


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
    request_plan(run_id)
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
            raise ValueError('The preparation task is already terminal')
        jobs.cancel(task_id)
    while (value := task(task_id))['status'] == 'working':
        if cancellation and cancellation():
            raise InterruptedError('The task request was disconnected; preparation continues')
        time.sleep(.1)
    if method == 'tasks/cancel':
        return value
    result = outcome(task_id)
    return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
            'structuredContent': result, 'isError': value['status'] != 'completed',
            '_meta': {'io.modelcontextprotocol/related-task': {'taskId': task_id}}}
