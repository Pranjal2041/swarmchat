"""Runs inside each agent's task container. run.py starts it as `python3 /guest/guest.py`.

The container has no network, as in the official ProgramBench baseline. The agent's model calls go to
127.0.0.1:8765 and its chat to 127.0.0.1:8484, and this script carries both to Unix sockets that run.py serves on
the host. The API key stays on the host.

Then it prepares /workspace and runs the agent until run.py creates /guest/stop:

- claude: Claude Code in print mode. Its PostToolUse hook adds new chat messages and the official time warning to
  the conversation, and its Stop hook sends it back to work each time it ends its turn.
- codex: Codex's app server. New chat messages are added to the running turn, and each later turn sends it back to
  work.
- command: any other agent, run as a command. It reads its prompt from $AGENT_PROMPT_FILE and its new chat
  messages from the files in /notices. When it exits early it is run again with the loop message.

The task images ship Python 3.10, so this file uses only the standard library of Python 3.10.
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

GUEST = '/guest'
SOCKETS = '/sockets'
HOME = '/agent-home'
NOTICES = '/notices'
MODEL_PORT = 8765
CHAT_PORT = 8484


def load():
    with open(os.path.join(GUEST, 'config.json')) as f:
        return json.load(f)


def save(path, value):
    with open(path+'.tmp', 'w') as f:
        json.dump(value, f)
    os.replace(path+'.tmp', path)


def stopped():
    return os.path.exists(os.path.join(GUEST, 'stop'))


def note(kind, **fields):
    with open(os.path.join(HOME, 'guest.jsonl'), 'a') as f:
        f.write(json.dumps({'at': time.time(), 'kind': kind, **fields})+'\n')


# Relays: loopback TCP in the container to a Unix socket on the host.

def pipe(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for s in (source, target):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(port, path):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(64)
    while True:
        client, _ = server.accept()
        upstream = socket.socket(socket.AF_UNIX)
        try:
            upstream.connect(path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


# Chat messages and other notices. run.py writes each new chat message as a file in /notices.

def notices(limit=8):
    """The oldest waiting notices, at most `limit`. Returns their paths and their text. The caller removes the files
    once the agent has them."""
    names = sorted(n for n in os.listdir(NOTICES) if n.endswith('.json'))[:limit]
    paths, texts = [], []
    for name in names:
        path = os.path.join(NOTICES, name)
        try:
            with open(path) as f:
                texts.append(json.load(f)['text'])
        except (OSError, ValueError):
            continue
        paths.append(path)
    return paths, '\n\n'.join(texts)


def take_notices():
    """Like notices(), but each file is claimed by renaming it first, so two hooks running at once never deliver
    the same message twice."""
    texts = []
    for name in sorted(n for n in os.listdir(NOTICES) if n.endswith('.json'))[:8]:
        path = os.path.join(NOTICES, name)
        try:
            os.rename(path, path+'.taken')
        except FileNotFoundError:
            continue
        with open(path+'.taken') as f:
            texts.append(json.load(f)['text'])
        os.remove(path+'.taken')
    return '\n\n'.join(texts)


def time_warning(config):
    remaining = config['deadline']-time.time()
    return config['time_warning'].replace('{{minutes}}', str(max(0, int(remaining/60))))


def timekeeper(config):
    """Posts the end of the first phase, and for agents without hooks the official time warning, as notices."""
    while not stopped():
        phase = config.get('phase')
        if phase and time.time() >= phase['until'] and not os.path.exists(HOME+'/phase-over'):
            open(HOME+'/phase-over', 'w').close()
            save(os.path.join(NOTICES, '0-phase-over.json'), {'text': phase['notice']})
        if config['kind'] != 'claude' and config['deadline']-time.time() < 600 and not os.path.exists(HOME+'/warned'):
            open(HOME+'/warned', 'w').close()
            save(os.path.join(NOTICES, '0-time-warning.json'), {'text': time_warning(config)})
        time.sleep(5)


def loop_message(config):
    """What sends the agent back to work: the first phase's own message while that phase lasts."""
    phase = config.get('phase')
    if config['loop'] and phase and time.time() < phase['until']:
        return phase['loop']
    return config['loop']


# Claude Code hooks. Claude Code runs them as `python3 /guest/guest.py warn` and `... loop`.

def hook_warn():
    """PostToolUse: the official time warning in the last ten minutes, and new chat messages."""
    sys.stdin.read()
    config = load()
    texts = []
    if config['deadline']-time.time() < 600:
        texts.append(time_warning(config))
    text = take_notices()
    if text:
        texts.append(text)
    if texts:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                                 'additionalContext': '\n\n'.join(texts)}}))


def hook_loop():
    """Stop: block the end of the turn and send the agent back to work, with any new chat messages."""
    sys.stdin.read()
    config = load()
    if stopped() or not config['loop']:
        return
    text = take_notices()
    print(json.dumps({'decision': 'block', 'reason': loop_message(config)+('\n\n'+text if text else '')}))


# Setup.

def git(*args):
    subprocess.run(['git', *args], cwd='/workspace', check=True, capture_output=True)


def setup(config):
    # The reference is mounted read-only at /reference/executable. The workspace copy goes, so the agent's own
    # build at ./executable replaces nothing.
    if os.path.exists('/workspace/executable'):
        os.remove('/workspace/executable')
    git('config', 'user.name', config['id'])
    git('config', 'user.email', config['id']+'@programbench.local')
    if config.get('shared'):
        git('remote', 'add', 'origin', config['shared'])
        git('fetch', '-q', 'origin')
        git('branch', '-u', 'origin/'+config['branch'])
        if config.get('incoming'):
            git('remote', 'add', 'incoming', config['incoming'])
            git('fetch', '-q', 'incoming')


def environment(config):
    env = dict(os.environ)
    env.update(HOME='/home/agent', PATH='/opt/swarmchat/bin:'+env.get('PATH', '/usr/local/bin:/usr/bin:/bin'))
    env.update(config['env'])
    if config.get('chat_token'):
        env.update(SWARMCHAT_SERVER=f'http://127.0.0.1:{CHAT_PORT}', SWARMCHAT_TOKEN=config['chat_token'])
    return env


def end(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# The agents.

def run_claude(config):
    """Claude Code processes, one after another, all in one conversation, until run.py stops the run."""
    env = environment(config)
    seconds = str(config['command_seconds']*1000)
    env.update(CLAUDE_CONFIG_DIR=HOME+'/config', ANTHROPIC_BASE_URL=f'http://127.0.0.1:{MODEL_PORT}',
               ANTHROPIC_AUTH_TOKEN='relay', DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               BASH_DEFAULT_TIMEOUT_MS=seconds, BASH_MAX_TIMEOUT_MS=seconds,
               BASH_MAX_OUTPUT_LENGTH=str(config['output_chars']), API_TIMEOUT_MS=str(3600*1000))
    env.pop('ANTHROPIC_API_KEY', None)
    projects = HOME+'/config/projects'
    restarts = 0
    while not stopped():
        # Claude Code saves the conversation under projects/ once it starts, and later processes resume it.
        first = not any(os.path.exists(os.path.join(projects, d, config['session']+'.jsonl'))
                        for d in (os.listdir(projects) if os.path.isdir(projects) else ()))
        command = [config['binary'], '-p', '--output-format', 'stream-json', '--verbose', '--model', config['model'],
                   '--dangerously-skip-permissions', '--setting-sources', 'user',
                   '--append-system-prompt-file', GUEST+'/system.txt']
        if config.get('effort'):
            command += ['--effort', config['effort']]
        if config.get('tools'):
            command += ['--tools', ','.join(config['tools'])]
        command += ['--session-id', config['session']] if first else ['--resume', config['session']]
        prompt = open(GUEST+'/prompt.txt').read() if first else loop_message(config) or config['resume']
        with open(HOME+'/events.jsonl', 'a') as out, open(HOME+'/stderr.log', 'a') as err:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env,
                                       cwd='/workspace', text=True)
            process.stdin.write(prompt)
            process.stdin.close()
            while process.poll() is None and not stopped():
                time.sleep(1)
            end(process)
        note('exit', code=process.returncode)
        if stopped() or not config['loop']:
            return
        # Claude Code ended before the time limit, on an error or a turn the Stop hook did not catch. Wait a little
        # and resume the conversation.
        restarts = restarts+1 if process.returncode else 0
        time.sleep(min(60, 2**restarts))


def run_codex(config):
    """Codex's app server, restarted if it exits. It keeps one conversation until run.py stops the run."""
    env = environment(config)
    env.update(CODEX_HOME=HOME, RELAY_API_KEY='relay')
    cycles = 0
    with open(HOME+'/events.jsonl', 'a', buffering=1) as events, open(HOME+'/stderr.log', 'a', buffering=1) as err:
        while not stopped():
            thread = open(HOME+'/thread-id').read().strip() if os.path.exists(HOME+'/thread-id') else None
            process = subprocess.Popen([config['binary'], 'app-server'], env=env, cwd='/workspace',
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1)
            incoming = queue.Queue()

            def reader():
                try:
                    for line in process.stdout:
                        incoming.put(json.loads(line))
                finally:
                    incoming.put(None)

            threading.Thread(target=reader, daemon=True).start()
            requests, serial = {}, 0
            ready, active, waiting, interrupted = False, None, False, False
            retry_after, errors = 0.0, 0

            def send(method, params, paths=()):
                nonlocal serial
                serial += 1
                requests[serial] = (method, list(paths))
                process.stdin.write(json.dumps({'id': serial, 'method': method, 'params': params})+'\n')
                process.stdin.flush()

            try:
                send('initialize', {'clientInfo': {'name': 'swarmchat-programbench', 'version': '1'}})
                while True:
                    try:
                        event = incoming.get(timeout=.2)
                    except queue.Empty:
                        event = {}
                    if event is None:
                        raise RuntimeError('app server exited')
                    method, params = event.get('method'), event.get('params', {})
                    # The whole conversation, less the streamed pieces that the completed items repeat.
                    if method and not method.lower().endswith('delta'):
                        events.write(json.dumps({'at': time.time(), **event})+'\n')
                    # Subagents' turns come on the same stream. Only the agent's own thread's turns are its turns.
                    own = params.get('threadId', thread) == thread
                    if method == 'turn/started' and own:
                        active = params['turn']['id']
                    elif method == 'turn/completed' and own:
                        active = None
                        if params['turn'].get('error'):
                            errors += 1
                            retry_after = time.monotonic()+min(60, 2**errors)
                        else:
                            errors = 0
                        if not config['loop'] and os.path.exists(HOME+'/started'):
                            # Without a loop message, the end of the agent's turn ends its work.
                            return
                    # Replies to this script's requests. A request from the app server has a method as well as an id.
                    if 'method' not in event and event.get('id') in requests:
                        request, paths = requests.pop(event['id'])
                        if request in ('turn/start', 'turn/steer'):
                            waiting = False
                        if 'error' in event:
                            note('request_error', method=request, error=event['error'])
                            if request not in ('turn/start', 'turn/steer', 'turn/interrupt'):
                                raise RuntimeError(str(event['error']))
                            retry_after = time.monotonic()+1
                        else:
                            for path in paths:
                                os.remove(path)
                            if request == 'initialize':
                                process.stdin.write(json.dumps({'method': 'initialized', 'params': {}})+'\n')
                                process.stdin.flush()
                                args = {'cwd': '/workspace', 'model': config['model'], 'approvalPolicy': 'never',
                                        'sandbox': 'danger-full-access', 'developerInstructions': config['system']}
                                if thread:
                                    send('thread/resume', {**args, 'threadId': thread})
                                else:
                                    send('thread/start', args)
                            elif request in ('thread/start', 'thread/resume'):
                                thread, ready = event['result']['thread']['id'], True
                                with open(HOME+'/thread-id', 'w') as f:
                                    f.write(thread)
                            elif request == 'turn/start':
                                open(HOME+'/started', 'w').close()
                                if event['result'].get('turn', {}).get('status') == 'inProgress':
                                    active = event['result']['turn']['id']
                    if stopped():
                        if not active:
                            return
                        if not interrupted:
                            interrupted = True
                            send('turn/interrupt', {'threadId': thread, 'turnId': active})
                        continue
                    if not ready or waiting or time.monotonic() < retry_after:
                        continue
                    paths, text = notices()
                    if active:
                        if paths:
                            waiting = True
                            send('turn/steer', {'threadId': thread, 'expectedTurnId': active,
                                                'input': [{'type': 'text', 'text': text}]}, paths)
                        continue
                    first = not os.path.exists(HOME+'/started')
                    prompt = open(GUEST+'/prompt.txt').read() if first else loop_message(config) or config['resume']
                    if text:
                        prompt += '\n\n'+text
                    waiting = True
                    params = {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}]}
                    if config.get('effort'):
                        params['effort'] = config['effort']
                    send('turn/start', params, paths)
            except Exception as exc:
                note('restart', error=f'{type(exc).__name__}: {exc}')
            finally:
                end(process)
            cycles += 1
            time.sleep(min(30, 2*cycles))


def run_command(config):
    """Any other agent, run as a command. It is run again with the loop message each time it exits early."""
    env = environment(config)
    env.update(AGENT_SYSTEM_FILE=GUEST+'/system.txt', AGENT_MODEL=config['model'], AGENT_EFFORT=config.get('effort') or '',
               AGENT_MODEL_URL=f'http://127.0.0.1:{MODEL_PORT}', AGENT_NOTICES=NOTICES,
               AGENT_DEADLINE=str(config['deadline']))
    runs = 0
    while not stopped():
        if runs == 0:
            prompt_file = GUEST+'/prompt.txt'
        else:
            paths, text = notices(limit=10**6)
            prompt_file = f'{HOME}/prompt-{runs}.txt'
            with open(prompt_file, 'w') as f:
                f.write((loop_message(config) or config['resume'])+('\n\n'+text if text else ''))
            for path in paths:
                os.remove(path)
        env['AGENT_PROMPT_FILE'] = prompt_file
        with open(HOME+'/stdout.log', 'a') as out, open(HOME+'/stderr.log', 'a') as err:
            process = subprocess.Popen(config['command'], stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                       cwd='/workspace')
            while process.poll() is None and not stopped():
                time.sleep(1)
            end(process)
        note('exit', code=process.returncode)
        runs += 1
        if stopped() or not config['loop']:
            return
        time.sleep(5)


def main():
    config = load()
    threading.Thread(target=relay, args=(MODEL_PORT, SOCKETS+'/model.sock'), daemon=True).start()
    if config.get('chat_token'):
        threading.Thread(target=relay, args=(CHAT_PORT, SOCKETS+'/chat.sock'), daemon=True).start()
    setup(config)
    threading.Thread(target=timekeeper, args=(config,), daemon=True).start()
    try:
        {'claude': run_claude, 'codex': run_codex, 'command': run_command}[config['kind']](config)
    except Exception as exc:
        save(HOME+'/exit.json', {'at': time.time(), 'error': f'{type(exc).__name__}: {exc}'})
        raise
    save(HOME+'/exit.json', {'at': time.time()})
    # The container stays up until run.py has copied the workspace and removes it.
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    {'warn': hook_warn, 'loop': hook_loop}.get(sys.argv[1] if sys.argv[1:] else '', main)()
