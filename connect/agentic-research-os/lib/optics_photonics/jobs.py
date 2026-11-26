"""Independent background calculations, dependency graphs and persisted results."""
import base64
import heapq
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from optics_photonics import arrays, environments, execution
from optics_photonics.signals import Cancellation, Wakeup, notify

TERMINAL = {'succeeded', 'failed', 'cancelled', 'blocked', 'interrupted'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def cores():
    return len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)


@contextmanager
def database(path=None):
    root = environments.home(path)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root/'jobs.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA user_version').fetchone()[0] == 0:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, name TEXT, state TEXT,
          created REAL, finished REAL, cancel INTEGER DEFAULT 0, launched REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, payload TEXT, run_id TEXT);
        CREATE TABLE IF NOT EXISTS steps(run_id TEXT, id TEXT, specification TEXT, environment TEXT,
          state TEXT, result TEXT, error TEXT, started REAL, finished REAL, attempt INTEGER DEFAULT 1,
          PRIMARY KEY(run_id,id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER);
        CREATE INDEX IF NOT EXISTS active_steps ON steps(state);
    ''')
        db.execute("INSERT OR IGNORE INTO settings VALUES('cpu',?)", (cores(),))
        db.execute('PRAGMA user_version=1')
        db.commit()
    if db.execute('PRAGMA user_version').fetchone()[0] < 2:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS pools(name TEXT PRIMARY KEY, capacity INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS wakeups(id TEXT PRIMARY KEY, port INTEGER NOT NULL, expires REAL NOT NULL);
        PRAGMA user_version=2;
        ''')
        db.commit()
    if db.execute('PRAGMA user_version').fetchone()[0] < 3:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS request_steps(request_id TEXT PRIMARY KEY, step_ids TEXT NOT NULL);
        PRAGMA user_version=3;
        ''')
        db.commit()
    if db.execute('PRAGMA user_version').fetchone()[0] < 4:
        db.executescript('''
        CREATE INDEX IF NOT EXISTS runs_created_id ON runs(created DESC, id DESC);
        CREATE INDEX IF NOT EXISTS requests_run ON requests(run_id);
        CREATE INDEX IF NOT EXISTS steps_tool_run ON steps(json_extract(specification, '$.tool'), run_id);
        PRAGMA user_version=4;
        ''')
        db.commit()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def worker_lock(path, run_id):
    folder = environments.home(path)/'runs'/run_id
    folder.mkdir(parents=True, exist_ok=True)
    stream = (folder/'worker.lock').open('a+b')
    if stream.tell() == 0:
        stream.write(b'0'); stream.flush()
    stream.seek(0)
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1); acquired = True
            except OSError: pass
        else:
            import fcntl
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB); acquired = True
            except BlockingIOError: pass
        yield acquired
    finally:
        if acquired:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('Use letters, digits, dots, underscores or hyphens for step and run IDs')
    return value


def written(file):
    """Normalize a file or folder path relative to a step's output directory."""
    if not isinstance(file, str) or not file.strip():
        raise ValueError('A file binding names a path inside the predecessor output directory')
    text = file.replace('\\', '/')
    parts = PurePosixPath(text).parts
    if text.startswith('/') or PureWindowsPath(file).drive or '..' in parts:
        raise ValueError('A file binding names a path inside the predecessor output directory')
    return '/'.join(parts)


def packaged(step):
    """A step naming a packaged program: the program exists and declares every input the step gives it."""
    from . import native, programs
    tool, action = step['tool'], step['action']
    if not isinstance(action, str):
        raise ValueError('action names a packaged program')
    adapter = native.details(tool)
    if adapter and action in adapter['actions']:
        return
    spec = programs.catalog().get(tool, {}).get(action)
    if spec is None:
        if adapter:
            available = ', '.join(programs.catalog().get(tool, {})) or 'none'
            raise ValueError(f"{tool} session actions: {', '.join(adapter['actions'])}; programs: {available}")
        programs.source(tool, action)  # names the programs of the tool
    varied = {str(key).split('.')[0] for key in (step.get('optimize') or {}).get('variables', {})}
    given = set(step.get('inputs', {})).union(*(item['inputs'] for item in step.get('points', [])))
    unknown = sorted((given | set(step.get('bindings', {})) | varied) - set(spec['inputs']))
    if unknown:
        raise ValueError(f"{tool}/{action} has no input {', '.join(unknown)}; it takes: {', '.join(spec['inputs'])}")


def validate(steps, known=()):
    """Check new steps. known holds the IDs of steps already in the run.

    Existing steps cannot depend on new ones, so only the new steps are traversed
    and appending to a long run costs no more than the batch itself.
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError('Provide a nonempty steps array')
    graph = {}
    result = []
    for item in steps:
        if not isinstance(item, dict): raise ValueError('Each step must be an object')
        s = dict(item)
        identifier(s.get('id'))
        if s['id'] in graph or s['id'] in known: raise ValueError('Duplicate step ID: '+s['id'])
        if set(s)-{'id','tool','action','source','script','inputs','after','bindings','timeout','requirements','optimize','point','points'}:
            raise ValueError('A step accepts id, tool, action, source, script, inputs, after, bindings, requirements, timeout, optimize, point and points')
        if not isinstance(s.get('script', ''), str): raise ValueError('script is the text of a program in the software\'s own language')
        if s.get('source') and s.get('script'): raise ValueError('Give a Python source or a script in the software\'s language, not both')
        if not isinstance(s.get('tool'), str): raise ValueError('Each step needs a software tool ID')
        if not isinstance(s.get('inputs', {}), dict): raise ValueError('inputs must be an object')
        if not isinstance(s.get('after', []), list): raise ValueError('after must be a list of step IDs')
        if not isinstance(s.get('bindings', {}), dict): raise ValueError('bindings must be an object')
        if s.get('timeout') is not None and (isinstance(s['timeout'], bool) or not isinstance(s['timeout'], (int,float)) or s['timeout'] <= 0):
            raise ValueError('timeout must be positive seconds')
        quantities(s.get('requirements', {}))
        s.setdefault('after', [])
        for key, reference in s.get('bindings', {}).items():
            if not isinstance(reference, dict) or set(reference)-{'step','path','file'} or not isinstance(reference.get('step'),str):
                raise ValueError('A binding needs step and either a result path array or a file')
            if 'file' in reference:
                if 'path' in reference: raise ValueError('A binding takes a result path or a file, not both')
                written(reference['file'])
            elif not isinstance(reference.get('path', []), list): raise ValueError('Binding path must be an array of object keys or array indices')
            if reference['step'] not in s['after']: s['after'].append(reference['step'])
        if not isinstance(s.get('point', {}), dict): raise ValueError('point records the swept inputs of a step as an object')
        if 'points' in s and (not isinstance(s['points'], list) or not s['points'] or s.get('bindings') or
                              any(not isinstance(item, dict) or set(item) != {'id','point','inputs'} for item in s['points'])):
            raise ValueError('points lists {id, point, inputs} computed in turn by one process, without bindings')
        point_ids = set()
        for point in s.get('points', []):
            identifier(point['id'])
            if point['id'] in point_ids or not isinstance(point['inputs'], dict) or not isinstance(point['point'], dict):
                raise ValueError('points need unique IDs and object inputs and point values')
            point_ids.add(point['id'])
        if s.get('optimize') is not None:
            from . import optimization
            optimization.checked(s['optimize'])
        if s.get('action') and not s.get('source') and not s.get('script'):
            packaged(s)
        graph[s['id']] = s['after']
        result.append(s)
    # Kahn's traversal is linear even for a long sequential experiment.
    from collections import deque
    children = {key: [] for key in graph}
    degree = {}
    for key, deps in graph.items():
        unique = set(deps)
        if any(dep not in graph and dep not in known for dep in unique):
            raise ValueError('Workflow contains an unknown predecessor')
        inside = [dep for dep in unique if dep in graph]
        degree[key] = len(inside)
        for dep in inside:
            children[dep].append(key)
    ready = deque(key for key, count in degree.items() if not count)
    visited = 0
    while ready:
        visited += 1
        for child in children[ready.popleft()]:
            degree[child] -= 1
            if not degree[child]: ready.append(child)
    if visited != len(graph): raise ValueError('Workflow contains a cycle')
    return result


def start_worker(run_id, path=None):
    identifier(run_id)
    root = environments.home(path)
    with worker_lock(root, run_id) as available:
        if not available: return
    with database(root) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state,launched FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row or row['state'] in TERMINAL or time.time()-row['launched']<5: return
        db.execute('UPDATE runs SET launched=? WHERE id=?',(time.time(),run_id))
    directory = root/'runs'/run_id
    options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with (directory/'worker.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), str(root), run_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=root, close_fds=True, **options)
    threading.Thread(target=process.wait,daemon=True).start()


def deferred():
    """The environment of a calculation waiting for its tool's preparation step; the binding is read when it starts.

    Admission uses these settings, which match those of a newly prepared
    environment: one thread per process, and as many processes as CPU capacity allows.
    """
    return {'kind':'prepared','threads':1,'concurrency':cores(),'cwd':None,'requirements':{},'env':{}}


def setup(tool, path=None, **inputs):
    """A step that prepares a tool's environment, and the environment that runs it."""
    step = {'id':'prepare.'+tool,'tool':tool,'inputs':{'tool':tool,'home':str(environments.home(path)),**inputs},'after':[]}
    config = {'kind':'command','command':[sys.executable,'-B',str(Path(__file__).with_name('setup_worker.py'))],
              'threads':1,'concurrency':1,'cwd':None,'requirements':{},'env':{}}
    return step, config


def provisioned(steps, known, path=None):
    """Put a preparation step in front of the calculations of each tool that has no environment yet.

    A tool with an installation recipe for this platform is prepared once per
    run, by a step named prepare.TOOL; its calculations start after it. A
    tool without a recipe keeps the reason in the error.
    """
    configured = environments.read(path)['tools']
    overrides, added = {}, []
    for spec in steps:
        tool = spec['tool']
        if tool in configured:
            continue
        key = 'prepare.'+tool
        if spec['id'] == key:
            raise ValueError(key+' names the step that prepares '+tool+'; use another step ID')
        if key not in known and key not in overrides:
            try:
                environments.installable(tool, path)
            except ValueError as reason:
                raise ValueError('No environment is connected for '+tool+'. '+str(reason)) from None
            step, config = setup(tool, path, if_missing=True)
            added.append(step); overrides[key] = config
        if key not in spec['after']:
            spec['after'] = [*spec['after'], key]
        overrides[spec['id']] = deferred()
    return added+steps, overrides


def step_ids(run_id, path=None):
    """IDs of the steps of a run; an unknown run is an error."""
    identifier(run_id)
    with database(path) as db:
        if not db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone(): raise ValueError('Unknown run: '+run_id)
        return {row[0] for row in db.execute('SELECT id FROM steps WHERE run_id=?', (run_id,))}


def submitted_steps(request_id, path=None):
    """Original step IDs of a submission, including on a retry after later appends."""
    with database(path) as db:
        row = db.execute('SELECT step_ids FROM request_steps WHERE request_id=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else []


def reuse(request_id, payload, path=None):
    """Return an accepted request before discovery or expansion of its inputs.

    submit still checks inside its transaction when two new requests race.
    This path lets a retry use the saved definition after an integration changes.
    """
    if not request_id:
        return None
    with database(path) as db:
        row = db.execute('SELECT payload,run_id FROM requests WHERE id=?', (request_id,)).fetchone()
    if row is None:
        return None
    if row['payload'] != encoded(payload):
        raise ValueError('This request_id already belongs to different inputs')
    notify(path)
    start_worker(row['run_id'], path)
    return {'run_id': row['run_id'], 'request_id': request_id}


def submit(steps, name='Optical research', request_id=None, run_id=None, path=None, _environments=None, _payload=None, _allow_empty=False):
    key = request_id or str(uuid.uuid4())
    # A retried request is recognized by its payload: the steps, or the call that produced them.
    payload = encoded(_payload if _payload is not None else {'steps':steps,'name':name,'run_id':run_id})
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT * FROM requests WHERE id=?', (key,)).fetchone()
        if prior:
            if prior['payload'] != payload: raise ValueError('This request_id already belongs to different inputs')
            result_id = prior['run_id']
        else:
            result_id = identifier(run_id) if run_id else str(uuid.uuid4())
            existing = db.execute('SELECT * FROM runs WHERE id=?', (result_id,)).fetchone()
            if run_id and not existing: raise ValueError('Unknown run: '+run_id)
            if existing and existing['cancel']: raise ValueError('Resume the cancelled run before appending steps')
            known = {r[0] for r in db.execute('SELECT id FROM steps WHERE run_id=?', (result_id,))}
            from . import programs
            checked = [] if _allow_empty and steps == [] else [programs.snapshot(step) for step in validate(steps, known)]
            if _environments is None:
                checked, _environments = provisioned(checked, known, path)
            capacity = db.execute("SELECT value FROM settings WHERE key='cpu'").fetchone()[0]
            prepared = []
            for spec in checked:
                environment = _environments.get(spec['id']) or execution.binding(spec['tool'], path)
                if spec.get('points') and environment['kind'] == 'command':
                    raise ValueError('Command environments require one sweep point per step')
                if environment['threads'] > capacity:
                    raise ValueError(f"{spec['tool']} requests {environment['threads']} CPU threads; configured capacity is {capacity}")
                limits = dict(db.execute('SELECT name,capacity FROM pools'))
                for resource, count in requirements(spec, environment).items():
                    if count > limits.get(resource, 0):
                        raise ValueError('Configure sufficient capacity for resource: '+resource)
                prepared.append((result_id,spec['id'],encoded(spec),encoded(environment),'queued'))
            if not existing:
                now = time.time()
                db.execute('INSERT INTO runs(id,name,state,created,finished) VALUES(?,?,?,?,?)',
                           (result_id,name,'queued' if prepared else 'succeeded',now,None if prepared else now))
            elif prepared:
                db.execute("UPDATE runs SET state=CASE WHEN state='running' THEN state ELSE 'queued' END,finished=NULL WHERE id=?", (result_id,))
            db.executemany('INSERT INTO steps(run_id,id,specification,environment,state) VALUES(?,?,?,?,?)', prepared)
            db.execute('INSERT INTO requests VALUES(?,?,?)', (key,payload,result_id))
            db.execute('INSERT INTO request_steps VALUES(?,?)', (key,encoded([step['id'] for step in steps])))
    notify(path)
    start_worker(result_id, path)
    return {'run_id':result_id, 'request_id':key}


def prepare(tool, python=None, packages=None, request_id=None, path=None):
    """Install a tool's recipe and extra packages in the background; returns the run of one prepare step."""
    configured = environments.read(path)['tools'].get(tool)
    if not (packages and configured and configured.get('managed')):
        environments.installable(tool, path)
    step, config = setup(tool, path, python=python, packages=packages or [])
    return submit([step],name='Prepare '+tool,request_id=request_id,path=path,_environments={step['id']:config})


def reconcile(path=None):
    with database(path) as db:
        runs = [r[0] for r in db.execute("SELECT id FROM runs WHERE state='running'")]
    for run_id in runs:
        with worker_lock(path, run_id) as available:
            if not available: continue
            with database(path) as db:
                db.execute("UPDATE steps SET state='interrupted',error='Background worker stopped; completed results are retained',finished=? WHERE run_id=? AND state='running'", (time.time(),run_id))
                db.execute("UPDATE runs SET state='interrupted',finished=? WHERE id=? AND state='running'", (time.time(),run_id))


def status(run_id, path=None, offset=0, limit=50, include_results=True, wait=0, cancel=None):
    """Read a run; wait returns early on completion or when cancel (a Cancellation) is set."""
    if not 0 <= wait <= 25: raise ValueError("wait must be between 0 and 25 seconds")
    if wait:
        deadline = time.monotonic()+wait
        with Wakeup(path) as wake, (cancel.subscribe(wake.interrupt) if cancel else nullcontext()):
            while True:
                result = status(run_id, path, offset, limit, include_results)
                remaining = deadline-time.monotonic()
                if result["state"] in TERMINAL or remaining <= 0 or cancel and cancel(): return result
                wake.wait(min(remaining, 2))
    identifier(run_id)
    if offset < 0 or not 1 <= limit <= 200: raise ValueError('Use offset >= 0 and limit between 1 and 200')
    reconcile(path)
    with database(path) as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row: raise ValueError('Unknown run: '+run_id)
        result = dict(row)
        result['counts'] = dict(db.execute('SELECT state,count(*) FROM steps WHERE run_id=? GROUP BY state', (run_id,)))
        projection = 'result' if include_results else 'NULL AS result'
        items = db.execute(f'SELECT id,state,{projection},error,started,finished,attempt FROM steps WHERE run_id=? ORDER BY rowid LIMIT ? OFFSET ?', (run_id,limit+1,offset)).fetchall()
        result['steps'] = [{**dict(s), 'result':json.loads(s['result']) if s['result'] else None} for s in items[:limit]]
        if not include_results:
            for item in result['steps']:
                item.pop('result', None)
                item['output'] = item['id']+'/attempt-'+str(item['attempt'])+'/'
        result['next_offset'] = offset+limit if len(items)>limit else None
    from .progress import summary
    for item in result['steps']:
        prefix = item['id'] + '/attempt-' + str(item['attempt']) + '/'
        saved = summary(environments.home(path) / 'runs' / run_id / prefix)
        if saved:
            item['progress'] = [{**entry, 'path': prefix + entry['path']} for entry in saved]
    if result['state']=='queued': start_worker(run_id, path)
    return result


def outcome(run_id, step_id=None, wait=0, path=None, cancel=None):
    """The state of a run after waiting up to wait seconds, with the result or error of step_id once it has finished."""
    current = status(run_id, path, limit=1, include_results=False, wait=wait, cancel=cancel)
    reply = {'state': current['state'], 'counts': current['counts']}
    if step_id:
        with database(path) as db:
            row = db.execute('SELECT state,result,error,attempt FROM steps WHERE run_id=? AND id=?', (run_id, step_id)).fetchone()
        if row and row['state'] in TERMINAL:
            record = json.loads(row['result']) if row['result'] else {}
            reply.update({'output': f"{step_id}/attempt-{row['attempt']}/", **{key: record[key] for key in ('result', 'files') if key in record}})
            if row['error']: reply['error'] = row['error']
    return reply


def list_runs(state=None, offset=0, limit=20, path=None, search=None, tool=None, before=None):
    """Find saved work without loading program sources or calculation results.

    ``before`` is the last run ID of the previous page. Unlike an offset, it
    keeps the page stable when another client submits new work between reads.
    The existing offset interface remains available.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Use integer offset >= 0 and limit between 1 and 200')
    if state is not None and (not isinstance(state, str) or state not in TERMINAL | {'queued', 'running'}):
        raise ValueError('state is queued, running, succeeded, failed, cancelled, blocked or interrupted')
    for key, value in (('search', search), ('tool', tool), ('before', before)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(key + ' must be nonempty text')
    if before is not None and offset:
        raise ValueError('Use before or offset to continue a page, not both')
    reconcile(path)
    with database(path) as db:
        clauses, values = [], []
        if state is not None:
            clauses.append('r.state=?'); values.append(state)
        if tool is not None:
            clauses.append("EXISTS (SELECT 1 FROM steps s WHERE json_extract(s.specification, '$.tool')=? AND s.run_id=r.id)")
            values.append(tool)
        if search is not None:
            # A percent or underscore in a research name is literal text.
            pattern = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("""(r.name LIKE ? ESCAPE '\\' OR r.id LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM requests q WHERE q.run_id=r.id AND q.id LIKE ? ESCAPE '\\')
                OR EXISTS (SELECT 1 FROM steps s WHERE s.run_id=r.id
                    AND json_extract(s.specification, '$.tool') LIKE ? ESCAPE '\\'))""")
            values.extend([pattern] * 4)
        if before is not None:
            anchor = db.execute('SELECT created,id FROM runs WHERE id=?', (before,)).fetchone()
            if anchor is None:
                raise ValueError('Unknown page cursor run: ' + before)
            clauses.append('(r.created,r.id) < (?,?)'); values.extend(anchor)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = db.execute('SELECT r.id,r.name,r.state,r.created,r.finished FROM runs r' + where +
                          ' ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?', [*values, limit + 1, offset]).fetchall()
        more = len(rows) > limit
        result = [dict(row) for row in rows[:limit]]
        by_id = {row['id']: row for row in result}
        for row in result:
            row.update(tools=[], counts={})
        if result:
            placeholders = ','.join('?' for _ in result)
            for row in db.execute("SELECT DISTINCT run_id,json_extract(specification, '$.tool') AS tool FROM steps WHERE run_id IN (" + placeholders + ') ORDER BY tool', list(by_id)):
                by_id[row['run_id']]['tools'].append(row['tool'])
            for row in db.execute('SELECT run_id,state,count(*) AS n FROM steps WHERE run_id IN (' + placeholders + ') GROUP BY run_id,state', list(by_id)):
                by_id[row['run_id']]['counts'][row['state']] = row['n']
    return {'runs': result, 'next_offset': offset + limit if more and before is None else None,
            'next_before': result[-1]['id'] if more else None}


def cancel(run_id, path=None):
    identifier(run_id)
    with database(path) as db:
        if not db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone(): raise ValueError('Unknown run: '+run_id)
        db.execute('UPDATE runs SET cancel=1 WHERE id=?', (run_id,))
    notify(path)
    start_worker(run_id, path)
    return {'run_id':run_id,'cancel_requested':True}


def resume(run_id, path=None):
    identifier(run_id)
    with worker_lock(path, run_id) as available:
        with database(path) as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run: raise ValueError('Unknown run: '+run_id)
            if not available and run['state'] not in TERMINAL:
                raise ValueError('This run is still executing')
            # A terminal worker may still be closing its notification socket.
            # It no longer writes steps and starts queued work after unlocking.
            db.execute("UPDATE steps SET state='queued',result=NULL,error=NULL,started=NULL,finished=NULL,attempt=attempt+1 WHERE run_id=? AND state<>'succeeded'", (run_id,))
            db.execute("UPDATE runs SET state='queued',cancel=0,finished=NULL,launched=0 WHERE id=?", (run_id,))
    notify(path)
    start_worker(run_id, path)
    return {'run_id':run_id,'resumed':True}


def quantities(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k.strip() or
        k == 'cpu' or type(v) is not int or v < 1 for k, v in value.items()):
        raise ValueError('Resource pools need names and positive integer quantities; CPU uses threads')
    return value


def requirements(spec, config):
    result = dict(quantities(config.get('requirements', {})))
    for name, count in quantities(spec.get('requirements', {})).items():
        result[name] = max(count, result.get(name, 0))
    return result


def resources(cpu=None, pools=None, path=None):
    if pools is not None: quantities(pools)
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        pending = [(json.loads(r[0]), json.loads(r[1])) for r in db.execute(
            "SELECT specification,environment FROM steps WHERE state IN ('queued','running')")]
        if cpu is not None:
            if type(cpu) is not int or cpu < 1: raise ValueError('cpu must be a positive integer')
            if any(c['threads'] > cpu for s, c in pending):
                raise ValueError('A pending calculation requires more CPU threads than this capacity')
            db.execute("UPDATE settings SET value=? WHERE key='cpu'", (cpu,))
        for name, count in (pools or {}).items():
            if any(requirements(s, c).get(name, 0) > count for s, c in pending):
                raise ValueError('A pending calculation requires more capacity for: '+name)
            db.execute('INSERT INTO pools VALUES(?,?) ON CONFLICT(name) DO UPDATE SET capacity=excluded.capacity', (name,count))
        result = {'cpu':db.execute("SELECT value FROM settings WHERE key='cpu'").fetchone()[0],
                  'pools':dict(db.execute('SELECT name,capacity FROM pools'))}
    if cpu is not None or pools is not None: notify(path)
    return result


def file_path(run_id, file, path=None):
    identifier(run_id)
    root = (environments.home(path)/'runs'/run_id).resolve()
    selected = (root/file).resolve()
    if not selected.is_relative_to(root) or not selected.is_file(): raise ValueError('Unknown result file')
    return selected


def folder(directory):
    """(name, is_folder) entries of one directory in listing order; symbolic links are left out."""
    found = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False): found.append((entry.name, True))
                elif entry.is_file(follow_symlinks=False): found.append((entry.name, False))
    except OSError:
        return []  # removed by its calculation while listing
    return sorted(found, key=lambda item: (os.path.normcase(item[0]), item[0]))


def walk(directory, relative=''):
    """Relative file paths below a directory in a stable order, one folder at a time."""
    for name, is_folder in folder(directory):
        if is_folder: yield from walk(os.path.join(directory, name), relative+name+'/')
        elif name != 'worker.lock': yield relative+name


def files(run_id, path=None, offset=0, limit=100, prefix=''):
    """List result files; prefix (e.g. 'solve/attempt-1/') walks only that part of the run.

    Names are read folder by folder; sizes, and dtype and shape of NPY and NPZ
    files, are read only for the returned page.
    """
    identifier(run_id)
    if offset<0 or not 1<=limit<=500: raise ValueError('Use offset >= 0 and limit between 1 and 500')
    root = (environments.home(path)/'runs'/run_id).resolve()
    if not root.is_dir(): raise ValueError('Unknown run: '+run_id)
    prefix = str(prefix or '').replace('\\','/').lstrip('/')
    base = (root/prefix).resolve()
    if not base.is_dir(): base = base.parent
    if not base.is_relative_to(root): raise ValueError('prefix must be inside the run')
    inside = base.relative_to(root).as_posix()
    page, total = [], 0
    for name in walk(str(base), '' if inside == '.' else inside+'/'):
        if not name.startswith(prefix): continue
        if offset <= total < offset+limit:
            try: entry = {'file':name,'bytes':os.stat(root/name).st_size}
            except OSError: continue  # removed since its folder was read
            if name.endswith(('.npy','.npz')):
                try: entry['array'] = arrays.describe(root/name)
                except (OSError, ValueError): pass  # still being written
            page.append(entry)
        total += 1
    return {'run_id':run_id, 'files':page, 'total':total, 'next_offset':offset+limit if offset+limit<total else None}


def download(run_id, file, destination, path=None):
    source = file_path(run_id, file, path)
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, target.open('xb') as outgoing: shutil.copyfileobj(incoming, outgoing)
    return {'path':str(target),'bytes':target.stat().st_size}


def read_result(run_id, file, offset=0, limit=16384, path=None):
    if offset<0 or not 1<=limit<=65536: raise ValueError('Use offset >= 0 and limit between 1 and 65536 bytes')
    source = file_path(run_id,file,path)
    with source.open('rb') as stream:
        stream.seek(offset); content=stream.read(limit)
    try: data=content.decode('utf-8'); format='utf-8'
    except UnicodeDecodeError: data=base64.b64encode(content).decode('ascii'); format='base64'
    return {'file':file,'offset':offset,'encoding':format,'data':data,'next_offset':offset+len(content) if offset+len(content)<source.stat().st_size else None}


def read_array(run_id, file, member=None, index=None, max_elements=None, image=None, part=None, scale='linear', path=None):
    """Read part of an NPY array or NPZ member from a run; see arrays.read.

    With image (auto, line or map) the selection is drawn as a PNG, returned
    under '_image' with a summary (extremes and their positions) in place of
    the values; part and scale choose what is drawn (see pictures.picture).
    """
    target = file_path(run_id, file, path)
    selection = arrays.read(target, member=member, index=index, max_elements=max_elements or (arrays.LIMIT if image else 10000))
    if not image:
        return {'run_id':run_id, 'file':file, **selection}
    # Axes of length one hold no choice: a picture fixes them, as an index would.
    given = list(index or []) + [None] * (len(selection['shape']) - len(index or []))
    fixed = [0 if size == 1 and not isinstance(entry, int) else entry for entry, size in zip(given, selection['shape'])]
    if fixed != given and any(size > 1 and not isinstance(entry, int) for entry, size in zip(fixed, selection['shape'])):
        selection = arrays.read(target, member=member, index=fixed, max_elements=max_elements or arrays.LIMIT)
    from . import pictures
    png, summary = pictures.picture(selection, kind=image, part=part, scale=scale)
    kept = {key: value for key, value in selection.items() if key not in ('values', 'real', 'imag')}
    return {'run_id':run_id, 'file':file, **kept, 'summary':summary, '_image':png}


def resolved_inputs(spec, results, folders):
    """Step inputs with bound values: a result value by path, or the location of a file a predecessor wrote."""
    inputs = dict(spec.get('inputs', {}))
    for key, reference in spec.get('bindings', {}).items():
        if 'file' in reference:
            folder_ = folders[reference['step']]
            target = (folder_/written(reference['file'])).resolve()
            if not target.is_relative_to(folder_.resolve()) or not target.exists():
                raise ValueError(f"Step {reference['step']} did not write {reference['file']}")
            inputs[key] = str(target)
            continue
        value = results[reference['step']]
        for part in reference.get('path', []): value = value[part]
        inputs[key] = value
    return inputs


def perform(run_id, row, path, cancellation):
    spec, config = json.loads(row['specification']), json.loads(row['environment'])
    output = environments.home(path)/'runs'/run_id/row['id']/('attempt-'+str(row['attempt']))
    try:
        if config['kind'] == 'prepared':
            config = execution.binding(spec['tool'], path)
        with database(path) as db:
            results, folders = {}, {}
            for reference in spec.get('bindings', {}).values():
                key = reference['step']
                if 'file' in reference and key not in folders:
                    attempt = db.execute("SELECT attempt FROM steps WHERE run_id=? AND id=? AND state='succeeded'",(run_id,key)).fetchone()[0]
                    folders[key] = environments.home(path)/'runs'/run_id/key/('attempt-'+str(attempt))
                elif 'file' not in reference and key not in results:
                    row_result = db.execute("SELECT result FROM steps WHERE run_id=? AND id=? AND state='succeeded'",(run_id,key)).fetchone()
                    results[key] = json.loads(row_result[0])
        request = {'action':spec.get('action',''),'source':spec.get('source',''),'inputs':resolved_inputs(spec,results,folders),
                   **{key:spec[key] for key in ('script','optimize','points','_program') if spec.get(key)}}
        previous = [output.parent / ('attempt-' + str(number)) for number in range(row['attempt'] - 1, 0, -1)]
        result = execution.run(spec['tool'],request,output=output,configuration=config,timeout=spec.get('timeout'),
                               cancel=cancellation, previous=previous)
        record = result['record']
        result = {'output':result['output'], **record} if isinstance(record,dict) and 'result' in record else {'output':result['output'],'result':record}
        state, error = 'succeeded', None
    except execution.Cancelled as exc:
        state, result, error = 'cancelled', None, str(exc)
    except Exception as exc:
        state, result, error = 'failed', None, str(exc)
    with database(path) as db:
        db.execute('UPDATE steps SET state=?,result=?,error=?,finished=? WHERE run_id=? AND id=?', (state,encoded(result) if result is not None else None,error,time.time(),run_id,row['id']))
    return state


def admission(spec, config):
    """The part of a step that scheduling needs; stored specifications never change."""
    return {'tool':spec['tool'], 'threads':config['threads'], 'concurrency':config['concurrency'],
            'needs':requirements(spec, config)}


class Scheduler:
    """Admit one run's steps; dependency state is kept in memory by this worker.

    Only this worker changes the states of its run's steps while it holds the
    run lock, so each iteration reads new steps, the cancel flag and the steps
    running across all runs, instead of every step and its result.
    """
    def __init__(self, run_id, path, db):
        self.run_id, self.path, self.db = run_id, path, db
        self.steps, self.children = {}, {}
        self.ready, self.last_rowid, self.unsettled = [], 0, 0
        self.shared = {}  # (run_id, step_id) -> admission of a running step

    def load(self):
        """Read steps appended since the last iteration and place them in the graph."""
        rows = self.db.execute('SELECT rowid,id,specification,environment,state FROM steps '
                               'WHERE run_id=? AND rowid>? ORDER BY rowid', (self.run_id, self.last_rowid)).fetchall()
        if not rows: return False
        batch = []
        for row in rows:
            spec = json.loads(row['specification'])
            node = {'position':row['rowid'], 'after':sorted(set(spec['after'])), 'state':row['state'], 'pending':0,
                    **admission(spec, json.loads(row['environment']))}
            self.steps[row['id']] = node; self.children.setdefault(row['id'], [])
            batch.append((row['id'], node))
            self.last_rowid = row['rowid']
            if node['state'] not in TERMINAL: self.unsettled += 1
        # Predecessors may appear later in the same batch.
        for key, node in batch:
            for dep in node['after']:
                self.children[dep].append(key)
                if self.steps[dep]['state'] != 'succeeded': node['pending'] += 1
        for key, node in batch:
            if node['state'] == 'running':
                # Left by a previous worker of this run; this worker holds the lock.
                self.finish(key, 'interrupted', 'Background worker stopped; completed results are retained')
            elif node['state'] == 'queued':
                if any(self.steps[d]['state'] in TERMINAL-{'succeeded'} for d in node['after']):
                    self.finish(key, 'blocked', 'A predecessor did not complete')
                elif not node['pending']:
                    heapq.heappush(self.ready, (node['position'], key))
        return True

    def finish(self, key, state, error=None, *, recorded=False):
        """Record a terminal state and release or block the steps that follow it."""
        stack = [(key, state, error, recorded)]
        while stack:
            key, state, error, recorded = stack.pop()
            node = self.steps[key]
            if node['state'] in TERMINAL: continue
            node['state'] = state; self.unsettled -= 1
            if not recorded:
                self.db.execute('UPDATE steps SET state=?,error=?,finished=? WHERE run_id=? AND id=?',
                                (state, error, time.time(), self.run_id, key))
            for child in self.children[key]:
                follower = self.steps[child]
                if follower['state'] != 'queued': continue
                if state == 'succeeded':
                    follower['pending'] -= 1
                    if not follower['pending']: heapq.heappush(self.ready, (follower['position'], child))
                else:
                    stack.append((child, 'blocked', 'A predecessor did not complete', False))

    def cancel_queued(self):
        now = time.time()
        self.db.execute("UPDATE steps SET state='cancelled',finished=? WHERE run_id=? AND state='queued'", (now, self.run_id))
        for node in self.steps.values():
            if node['state'] == 'queued': node['state'] = 'cancelled'; self.unsettled -= 1
        self.ready.clear()

    def running(self):
        """Admissions of steps running in every run; they share CPU and named capacity."""
        keys = set(map(tuple, self.db.execute("SELECT run_id,id FROM steps WHERE state='running'")))
        for key in set(self.shared) - keys: del self.shared[key]
        for key in keys - set(self.shared):
            row = self.db.execute('SELECT specification,environment FROM steps WHERE run_id=? AND id=?', key).fetchone()
            self.shared[key] = admission(json.loads(row[0]), json.loads(row[1]))
        return list(self.shared.values())

    def admit(self):
        """Mark ready steps running in submission order while capacity remains."""
        active = self.running()
        cpu = self.db.execute("SELECT value FROM settings WHERE key='cpu'").fetchone()[0]
        limits = dict(self.db.execute('SELECT name,capacity FROM pools'))
        used = sum(a['threads'] for a in active)
        counts = Counter(a['tool'] for a in active)
        occupied = Counter()
        for a in active: occupied.update(a['needs'])
        admitted, deferred = [], []
        while self.ready and used < cpu:
            position, key = heapq.heappop(self.ready)
            node = self.steps[key]
            concurrency = min([node['concurrency']]+[a['concurrency'] for a in active if a['tool']==node['tool']])
            if used+node['threads'] > cpu or counts[node['tool']] >= concurrency or \
               any(occupied[name]+count > limits.get(name, 0) for name, count in node['needs'].items()):
                deferred.append((position, key)); continue
            occupied.update(node['needs']); used += node['threads']; counts[node['tool']] += 1
            active.append(node); node['state'] = 'running'
            self.shared[(self.run_id, key)] = node
            row = self.db.execute('SELECT specification,environment,attempt FROM steps WHERE run_id=? AND id=?',
                                  (self.run_id, key)).fetchone()
            self.db.execute("UPDATE steps SET state='running',started=? WHERE run_id=? AND id=?", (time.time(), self.run_id, key))
            admitted.append({'id':key, 'specification':row[0], 'environment':row[1], 'attempt':row[2]})
        for item in deferred: heapq.heappush(self.ready, item)
        return admitted


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def work(run_id, path):
    with worker_lock(path,run_id) as available:
        if not available: return
        reconcile(path)
        with database(path) as db:
            run = db.execute('SELECT state FROM runs WHERE id=?',(run_id,)).fetchone()
            if not run or run[0] in TERMINAL: return
            db.execute("UPDATE runs SET state='running',launched=0 WHERE id=?",(run_id,))
        with database(path) as db, Wakeup(path) as wake, \
             ThreadPoolExecutor(max_workers=resources(path=path)['cpu']) as pool:
            db.commit()
            scheduler, pending, cancellation = Scheduler(run_id, path, db), {}, Cancellation()
            while True:
                with transaction(db):
                    for key, future in list(pending.items()):
                        if future.done():
                            del pending[key]
                            scheduler.finish(key, future.result(), recorded=True)
                    changed = scheduler.load()
                    cancelled = db.execute('SELECT cancel FROM runs WHERE id=?',(run_id,)).fetchone()[0]
                    if cancelled:
                        cancellation.set(); scheduler.cancel_queued()
                    if not scheduler.unsettled and not pending:
                        states = {node['state'] for node in scheduler.steps.values()}
                        state = 'cancelled' if cancelled else ('succeeded' if states <= {'succeeded'} else 'failed')
                        db.execute('UPDATE runs SET state=?,finished=? WHERE id=?',(state,time.time(),run_id))
                        break
                    admitted = [] if cancelled else scheduler.admit()
                for row in admitted:
                    future = pool.submit(perform,run_id,row,path,cancellation)
                    pending[row['id']] = future
                    future.add_done_callback(lambda _: notify(path))
                if not changed and not any(f.done() for f in pending.values()): wake.wait()
    notify(path)
    with database(path) as db:
        restart = db.execute("SELECT state FROM runs WHERE id=?",(run_id,)).fetchone()[0]=='queued'
    if restart: start_worker(run_id,path)


if __name__=='__main__':
    work(sys.argv[2], sys.argv[1])
