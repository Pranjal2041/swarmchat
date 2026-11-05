#!/usr/bin/env python3
"""Run coding agents on ProgramBench tasks, alone or as a team that talks over swarmchat.

    python3 run.py configs/flat.toml                 # every task in the config
    python3 run.py configs/flat.toml --dry-run       # show the prompts and the team, start nothing
    python3 run.py configs/flat.toml --hours 0.2 --task sitkevij__hex.61ae69b --name flat-try

Each agent works in its own container from the task's cleanroom image, with no network, as in the official
baseline. Its model calls and its chat leave the container over Unix sockets that this script serves, so the API key
stays on the host. A team shares one git repository, and its submission is the repository's branch when the time
runs out. A single agent's submission is its /workspace. Both are written where `programbench eval` reads them:

    runs/<name>/submissions/<task>/submission.tar.gz
    runs/<name>/snapshots/<minutes>/<task>/submission.tar.gz      every snapshot_minutes
    runs/<name>/work/<task>/                                       chat, logs and each agent's workspace

Needs Python 3.11 or later, Docker on a Linux x86_64 machine, and the agent's own Linux x86_64 command-line tool.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:
    sys.exit('run.py needs Python 3.11 or later, for tomllib.')

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from swarmchat.client import Client  # noqa: E402  the swarmchat in this repository

PROMPTS = HERE/'prompts'
SHARED = '/shared/repo.git'
INCOMING = '/incoming/repo.git'
# The official mini-swe-agent settings for ProgramBench: container limits, command timeout and output length.
OFFICIAL_ENV = {'PAGER': 'cat', 'MANPAGER': 'cat', 'LESS': '-R', 'PIP_PROGRESS_BAR': 'off', 'TQDM_DISABLE': '1'}
COMMAND_SECONDS = 180
OUTPUT_CHARS = 10000
# The largest file and the most processes an agent's container may have. An agent's program that writes into what
# it reads can grow a file without end, and a script that starts itself can fill the process table.
FILE_CAP = 10*2**30
PIDS_CAP = 32768
AGENTS = {
    'claude': {'api_base': 'https://api.anthropic.com', 'api_key_env': 'ANTHROPIC_API_KEY', 'api_auth': 'x-api-key',
               'tools': ['Bash', 'Read', 'Edit', 'Write']},
    'codex': {'api_base': 'https://api.openai.com', 'api_key_env': 'OPENAI_API_KEY', 'api_auth': 'bearer'},
    'command': {'api_auth': 'bearer'},
}
BASE_PROMPTS = {'system': 'system.md', 'task': 'task.md', 'single': 'single.md', 'team': 'team.md', 'chat': 'chat.md',
                'loop': 'loop.md', 'team_loop': 'team-loop.md', 'resume': 'resume.md',
                'time_warning': 'time-warning.md'}
PLACEHOLDER = re.compile(r'\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}')

# Set by Ctrl-C or SIGTERM: every task stops its agents, saves its submission and ends.
interrupted = threading.Event()


def log(task, text):
    print(f'[{time.strftime("%H:%M:%S")}] {task}: {text}', flush=True)


# The config.

def load_config(path, args):
    path = Path(path).resolve()
    with open(path, 'rb') as f:
        c = tomllib.load(f)
    base = path.parent

    def need(table, key, kind, where):
        if key not in table:
            raise SystemExit(f'{path}: {where} needs `{key}`.')
        if not isinstance(table[key], kind):
            raise SystemExit(f'{path}: {where}.{key} has the wrong type.')
        return table[key]

    def text_file(name, where):
        file = (base/name).expanduser()
        if not file.is_file():
            raise SystemExit(f'{path}: {where} names {file}, which does not exist.')
        return file.read_text()

    config = {'path': path, 'name': args.name or need(c, 'name', str, 'the config'),
              'tasks': args.task or need(c, 'tasks', list, 'the config'),
              'hours': args.hours if args.hours is not None else float(c.get('hours', 6)),
              'snapshot_minutes': float(c.get('snapshot_minutes', 0)), 'parallel': int(c.get('parallel', 1)),
              'loop': bool(c.get('loop', True)), 'hide': c.get('hide', {})}
    agent = dict(need(c, 'agent', dict, 'the config'))
    kind = need(agent, 'kind', str, '[agent]')
    if kind not in AGENTS:
        raise SystemExit(f'{path}: [agent] kind is one of {", ".join(AGENTS)}.')
    agent = {**AGENTS[kind], **agent}
    agent['binary'] = (base/Path(need(agent, 'binary', str, '[agent]')).expanduser()).resolve()
    # Helper files the tool needs beside it. Codex runs its tools through codex-code-mode-host, from its own folder.
    helper = agent['binary'].parent/'codex-code-mode-host'
    default = [str(helper)] if kind == 'codex' and helper.exists() else []
    agent['files'] = [(base/Path(f).expanduser()).resolve() for f in agent.get('files', default)]
    if kind == 'command':
        need(agent, 'command', list, '[agent]')
    agent.setdefault('model', '')
    if agent['api_auth'] not in ('x-api-key', 'bearer'):
        raise SystemExit(f'{path}: [agent] api_auth is x-api-key or bearer.')
    config['agent'] = agent
    docker = c.get('docker', {})
    config['docker'] = {'command': docker.get('command', 'docker'),
                        'image_tag': docker.get('image_tag', 'task_cleanroom_v6'),
                        'cpus': str(docker.get('cpus', 20)), 'memory': str(docker.get('memory', '60g'))}
    prompts = {**{k: str(PROMPTS/v) for k, v in BASE_PROMPTS.items()}, **c.get('prompts', {})}
    config['prompts'] = {k: text_file(v, f'[prompts] {k}') for k, v in prompts.items()}

    team = c.get('team')
    if team is None:
        config['team'] = None
        return config
    members = need(team, 'members', int, '[team]')
    roles = need(team, 'roles', list, '[team]')
    for i, role in enumerate(roles):
        need(role, 'name', str, f'[[team.roles]] {i+1}')
        role['prompt_text'] = text_file(need(role, 'prompt', str, f'[[team.roles]] {role["name"]}'),
                                        f'[[team.roles]] {role["name"]} prompt')
        role['loop_text'] = text_file(role['loop'], f'[[team.roles]] {role["name"]} loop') if 'loop' in role else None
        role.setdefault('push', True)
    config['team'] = {'members': members, 'roles': roles, 'chat': bool(team.get('chat', True)),
                      'channels': team.get('channels', {'general': 'all'}), 'phase': None}
    if 'phase' in team:
        phase = team['phase']
        config['team']['phase'] = {'minutes': float(need(phase, 'minutes', (int, float), '[team.phase]')),
                                   'loop': text_file(need(phase, 'loop', str, '[team.phase]'), '[team.phase] loop'),
                                   'notice': text_file(need(phase, 'notice', str, '[team.phase]'),
                                                       '[team.phase] notice')}
    config['agents'] = assign(config['team'], path)
    return config


def assign(team, path):
    """Each agent's id and role. A role takes `count` agents, or `share` of the team, and the roles with neither split
    the rest evenly. Agents are numbered a1, a2, ... in the order of the roles."""
    members, roles = team['members'], team['roles']
    sizes = [r['count'] if 'count' in r else max(1, round(r['share']*members)) if 'share' in r else None for r in roles]
    rest = members-sum(s for s in sizes if s is not None)
    open_roles = [i for i, s in enumerate(sizes) if s is None]
    for k, i in enumerate(open_roles):
        sizes[i] = rest//len(open_roles)+(k < rest % len(open_roles))
    if sum(sizes) != members or min(sizes) < 1:
        raise SystemExit(f'{path}: the roles do not divide {members} members so each has at least one: '
                         + ', '.join(f'{r["name"]} {s}' for r, s in zip(roles, sizes)))
    agents, n = [], 0
    for role, size in zip(roles, sizes):
        for _ in range(size):
            n += 1
            agents.append({'id': f'a{n}', 'role': role})
    return agents


def render(template, values, where):
    def fill(match):
        if match.group(1) not in values:
            raise SystemExit(f'{where}: unknown placeholder {{{{{match.group(1)}}}}}. '
                             f'Known here: {", ".join(sorted(values))}.')
        return str(values[match.group(1)])
    return PLACEHOLDER.sub(fill, template)


def tidy(text):
    return re.sub(r'\n{3,}', '\n\n', text).strip()+'\n'


def names(ids):
    return ', '.join(ids[:-1])+' and '+ids[-1] if len(ids) > 1 else ids[0]


def prompts_for(config, agent=None, branch=None):
    """An agent's task prompt and loop message. With no agent, a single agent's."""
    p, team = config['prompts'], config['team']
    if team is None:
        prompt = render(p['task'], {'submission': p['single']}, 'task prompt')
        return tidy(prompt), p['loop'].strip() if config['loop'] else ''
    ids = [a['id'] for a in config['agents']]
    values = {'id': agent['id'], 'n': len(ids), 'others': ', '.join(i for i in ids if i != agent['id']),
              'team': names(ids), 'role': agent['role']['name'], 'shared': SHARED, 'branch': branch or '<branch>',
              'incoming': INCOMING}
    for role in team['roles']:
        values[role['name']] = names([a['id'] for a in config['agents'] if a['role'] is role])
    if team['phase']:
        values['phase_minutes'] = f'{team["phase"]["minutes"]:g}'
    where = f'role {agent["role"]["name"]}'
    role_prompt = render(agent['role']['prompt_text'], values, where+' prompt')
    chat = render(p['chat'], values, 'chat prompt') if team['chat'] else ''
    section = render(p['team'], {**values, 'chat': chat.strip(), 'role_prompt': role_prompt.strip()}, 'team prompt')
    prompt = render(p['task'], {'submission': tidy(section)}, 'task prompt')
    loop = render(agent['role']['loop_text'] or p['team_loop'], values, where+' loop') if config['loop'] else ''
    return tidy(prompt), loop.strip()


def channels_for(config):
    team, ids = config['team'], [a['id'] for a in config['agents']]
    out = {}
    for name, members in team['channels'].items():
        if members == 'all':
            out[name] = ids
            continue
        unknown = set(members)-{r['name'] for r in team['roles']}
        if unknown:
            raise SystemExit(f'{config["path"]}: channel {name} names unknown roles {", ".join(sorted(unknown))}.')
        out[name] = [a['id'] for a in config['agents'] if a['role']['name'] in members]
    return out


# Docker.

class Docker:
    def __init__(self, command):
        self.base = shlex.split(command)

    def __call__(self, *args, check=True, timeout=900):
        result = subprocess.run([*self.base, *map(str, args)], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'docker {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
        return result


def image_name(instance, tag):
    return f'programbench/{instance.replace("__", "_1776_")}:{tag}'


# The host side of the relays.

class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, handler):
        super().__init__(str(path), handler)
        os.chmod(path, 0o666)


# Headers the container sends that do not go on: its placeholder key, and those that describe its own connection.
DROP = {'host', 'content-length', 'connection', 'keep-alive', 'transfer-encoding', 'authorization', 'x-api-key',
        'cookie', 'accept-encoding', 'proxy-connection', 'upgrade'}
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length'}


class ModelHandler(BaseHTTPRequestHandler):
    """One model request from an agent's container, sent on to the real API with the real key. The answer streams
    back as it arrives, and the connection closes after it."""
    protocol_version = 'HTTP/1.1'

    def address_string(self):
        return 'agent'

    def log_message(self, *args):
        pass

    def body(self):
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            data = b''
            while True:
                size = int(self.rfile.readline().split(b';')[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get('Content-Length') or 0))

    def relay(self):
        server, started = self.server, time.time()
        self.close_connection = True
        if self.headers.get('Upgrade'):
            self.send_error(426, 'Use HTTP')  # Codex falls back from WebSockets to HTTP
            return
        body = self.body()
        url = urlsplit(server.api_base)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
        if server.api_key and server.api_auth == 'x-api-key':
            headers['x-api-key'] = server.api_key
        elif server.api_key:
            headers['Authorization'] = 'Bearer '+server.api_key
        connection = (http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection)(
            url.hostname, url.port, timeout=3600)
        status, sent = None, 0
        try:
            connection.request(self.command, url.path.rstrip('/')+self.path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            self.send_response_only(response.status, response.reason)
            for k, v in response.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            length = response.getheader('Content-Length')
            if length and 'chunked' not in (response.getheader('Transfer-Encoding') or '').lower():
                self.send_header('Content-Length', length)
            self.send_header('Connection', 'close')
            self.end_headers()
            while True:
                data = response.read1(65536)
                if not data:
                    break
                self.wfile.write(data)
                self.wfile.flush()
                sent += len(data)
        except (OSError, http.client.HTTPException) as exc:
            if status is None:
                self.send_error(502, f'model API unreachable: {exc}')
        finally:
            connection.close()
            server.record({'at': started, 'method': self.command, 'path': self.path, 'status': status,
                           'seconds': round(time.time()-started, 3), 'bytes': sent})

    do_GET = do_POST = do_PUT = do_DELETE = relay


class ModelRelay(UnixServer):
    def __init__(self, path, agent, key, log_file):
        self.api_base, self.api_auth, self.api_key = agent['api_base'], agent['api_auth'], key
        self.log_file, self.lock, self.requests, self.failed = open(log_file, 'a', buffering=1), threading.Lock(), 0, 0
        super().__init__(path, ModelHandler)

    def record(self, entry):
        with self.lock:
            self.requests += 1
            self.failed += entry['status'] is None or entry['status'] >= 400
            self.log_file.write(json.dumps(entry)+'\n')


class ChatHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection(self.server.chat_address)

        def pipe(source, target):
            try:
                while data := source.recv(65536):
                    target.sendall(data)
            except OSError:
                pass
            finally:
                for s in (source, target):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        other = threading.Thread(target=pipe, args=(upstream, self.request), daemon=True)
        other.start()
        pipe(self.request, upstream)
        other.join()
        upstream.close()


class ChatRelay(UnixServer):
    def __init__(self, path, chat_address):
        self.chat_address = chat_address
        super().__init__(path, ChatHandler)


def start_chat(work, ids, channels):
    """The team's swarmchat server, one user per agent and the channels. Returns the server process, its URL and
    each agent's token."""
    data = work/'chat'
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = {**os.environ, 'PYTHONPATH': str(REPO)+os.pathsep+os.environ.get('PYTHONPATH', '')}
    process = subprocess.Popen([sys.executable, '-m', 'swarmchat', 'serve', '--data', str(data), '--port', str(port)],
                               stdout=open(work/'chat-server.log', 'a'), stderr=subprocess.STDOUT, env=env)
    for _ in range(200):
        if (data/'admin.token').exists() and (data/'address').exists():
            break
        if process.poll() is not None:
            raise RuntimeError(f'swarmchat exited at start, see {work/"chat-server.log"}')
        time.sleep(.1)
    url = (data/'address').read_text().strip()
    admin = Client(url, (data/'admin.token').read_text().strip())
    tokens = {i: admin.user_add(id=i, name=i)['token'] for i in ids}
    for name, members in channels.items():
        Client(url, tokens[members[0]]).channel_create(name=name, members=','.join(members))
    return process, url, tokens


def notify(client, notices, stop):
    """Each new chat message for the agent becomes a file in its notices folder, which the guest hands to the agent.
    Peeking marks nothing read, so `swarmchat read` still shows it."""
    after = {}
    while not stop.is_set():
        try:
            reply = client.call('wait', {'timeout': 20, 'after': after, 'peek': True}, http_timeout=60)
        except Exception:
            stop.wait(5)
            continue
        after.update(reply.get('positions') or {})
        for m in reply.get('messages', []):
            title = (reply.get('chats') or {}).get(m['chat'], {}).get('title') or m['chat']
            text = m.get('text') or ''
            if len(text) > 2000:
                text = text[:2000]+' [cut here: `swarmchat read` shows all of it]'
            files = f' [{len(m["attachments"])} attachment(s)]' if m.get('attachments') else ''
            name = f'{time.time_ns()}-{m["id"].replace(":", "-")}.json'
            temp = notices/(name+'.tmp')
            temp.write_text(json.dumps({'text': f'[swarmchat] {title}, from {m["from"]} ({m["id"]}):\n{text}{files}'}))
            os.chmod(temp, 0o666)
            temp.replace(notices/name)


# Submissions.

def git(*args, timeout=600):
    return subprocess.run(['git', '-c', 'safe.directory=*', *args], capture_output=True, text=True, check=True,
                          timeout=timeout)


def export_branch(repo, branch, destination):
    """A team's submission: a fresh checkout of its branch, packed like the official copy of /workspace."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    checkout = Path(tempfile.mkdtemp(prefix='pb-export-'))
    try:
        git('clone', '-q', '--branch', branch, str(repo), str(checkout/'w'))
        temp = destination.with_name(destination.name+'.tmp')
        subprocess.run(['tar', '-czf', str(temp), '-C', str(checkout/'w'), '.'], check=True, timeout=600)
        temp.replace(destination)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def copy_workspace(docker, container, destination):
    """The official submission of a single agent: a gzipped tar of its /workspace, made in the container."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    docker('exec', container, 'tar', '-czf', '/tmp/_submission.tar.gz', '-C', '/workspace', '.', check=False)
    temp = destination.with_name(destination.name+'.tmp')
    docker('cp', f'{container}:/tmp/_submission.tar.gz', temp)
    temp.replace(destination)


# One task.

def writable(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)  # the container's own user, whose id may differ from yours, writes here
    return path


def readable(path, text):
    path.write_text(text)
    os.chmod(path, 0o644)


def codex_config(agent):
    effort = f'model_reasoning_effort = "{agent["effort"]}"\n' if agent.get('effort') else ''
    return f'''model = "{agent['model']}"
{effort}model_provider = "relay"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"

[model_providers.relay]
name = "relay"
base_url = "http://127.0.0.1:8765/v1"
env_key = "RELAY_API_KEY"
wire_api = "responses"

[shell_environment_policy]
inherit = "all"

[analytics]
enabled = false

# As in the blog post's runs.
[features]
multi_agent = false
multi_agent_v2 = false
apps = false
plugins = false
skill_search = false
browser_use = false
computer_use = false
enable_request_compression = false
shell_snapshot = false
'''


CLAUDE_HOOKS = {'PostToolUse': [{'matcher': '*', 'hooks': [
    {'type': 'command', 'command': 'python3 /guest/guest.py warn', 'timeout': 30}]}],
    'Stop': [{'hooks': [{'type': 'command', 'command': 'python3 /guest/guest.py loop', 'timeout': 30}]}]}


def run_task(config, instance, out, docker):
    work = out/'work'/instance
    final = out/'submissions'/instance/'submission.tar.gz'
    if (work/'result.json').exists():
        log(instance, 'already done, skipped')
        return
    if work.exists():
        raise RuntimeError(f'{work} is from a run that did not finish. Delete it to run this task again.')
    team, agent_cfg = config['team'], config['agent']
    key = os.environ.get(agent_cfg['api_key_env'], '') if agent_cfg.get('api_key_env') else ''
    image = image_name(instance, config['docker']['image_tag'])
    if docker('image', 'inspect', image, check=False).returncode:
        log(instance, f'pulling {image}')
        docker('pull', image, timeout=3600)
    work.mkdir(parents=True)
    sockets = Path(tempfile.mkdtemp(prefix='pb-'))  # short paths: a Unix socket path has at most 107 bytes
    os.chmod(sockets, 0o755)
    stop, agents, servers, containers, chat_process = threading.Event(), [], [], [], None
    started, status = time.time(), 'Interrupted'
    try:
        # The reference executable, copied out of the image and mounted read-only at /reference/executable.
        (work/'reference').mkdir()
        holder = docker('create', image).stdout.strip()
        try:
            docker('cp', f'{holder}:/workspace/executable', work/'reference'/'executable')
        finally:
            docker('rm', '-f', holder, check=False)
        mounts = ['-v', f'{work/"reference"}:/reference:ro']
        hidden = config['hide'].get(instance, [])
        if hidden:
            # Files in the image that would hand the agent the original program read as empty.
            empty = work/'hidden'
            empty.write_bytes(b'')
            empty.chmod(0o444)
            mounts += [arg for p in hidden for arg in ('-v', f'{empty}:{p}:ro')]
        ids = [a['id'] for a in config['agents']] if team else ['a1']
        tokens, branch, incoming = {}, None, False
        if team:
            # The shared repository starts as a bare clone of the image's /workspace repository.
            writable(work/'shared')
            docker('run', '--rm', '--network', 'none', '--user', 'agent', '-v', f'{work/"shared"}:/shared', image,
                   'git', 'clone', '-q', '--bare', '/workspace', SHARED)
            branch = git('-C', str(work/'shared'/'repo.git'), 'symbolic-ref', '--short', 'HEAD').stdout.strip()
            incoming = not all(a['role']['push'] for a in config['agents'])
            if incoming:
                # Agents that may not push to the shared branch push their branches here.
                writable(work/'incoming')
                docker('run', '--rm', '--network', 'none', '--user', 'agent', '-v',
                       f'{work/"incoming"}:/incoming', image, 'git', 'clone', '-q', '--bare', '/workspace', INCOMING)
            if team['chat']:
                chat_process, chat_url, tokens = start_chat(work, ids, channels_for(config))
                (work/'chat-tokens.json').write_text(json.dumps(tokens))
                os.chmod(work/'chat-tokens.json', 0o600)
                bin_dir = work/'swarmchat-bin'
                bin_dir.mkdir()
                (bin_dir/'swarmchat').write_text(
                    '#!/bin/sh\nPYTHONPATH=/opt/swarmchat/lib exec python3 -m swarmchat "$@"\n')
                os.chmod(bin_dir/'swarmchat', 0o755)
                mounts += ['-v', f'{REPO/"swarmchat"}:/opt/swarmchat/lib/swarmchat:ro',
                           '-v', f'{bin_dir}:/opt/swarmchat/bin:ro']
                log(instance, f'watch the chat: PYTHONPATH={REPO} {sys.executable} -m swarmchat view '
                              f'--data {work/"chat"}')
        started = time.time()
        deadline = started+config['hours']*3600
        phase = team['phase'] if team else None
        binary = f'/opt/agent/{agent_cfg["binary"].name}'
        for aid in ids:
            agent = next(a for a in config['agents'] if a['id'] == aid) if team else None
            folder = work/'agents'/aid
            guest, home, notices = folder/'guest', writable(folder/'home'), writable(folder/'notices')
            guest.mkdir()
            os.chmod(guest, 0o755)
            prompt, loop = prompts_for(config, agent, branch)
            readable(guest/'prompt.txt', prompt)
            readable(guest/'system.txt', config['prompts']['system'])
            readable(guest/'guest.py', (HERE/'guest.py').read_text())
            readable(guest/'config.json', json.dumps({
                'id': aid, 'kind': agent_cfg['kind'], 'binary': binary, 'command': agent_cfg.get('command'),
                'model': agent_cfg['model'], 'effort': agent_cfg.get('effort'), 'tools': agent_cfg.get('tools'),
                'system': config['prompts']['system'], 'loop': loop, 'resume': config['prompts']['resume'].strip(),
                'time_warning': config['prompts']['time_warning'].strip(), 'deadline': deadline,
                'phase': phase and {'until': started+phase['minutes']*60, 'loop': phase['loop'].strip(),
                                    'notice': phase['notice'].strip()},
                'shared': SHARED if team else None, 'branch': branch, 'incoming': INCOMING if incoming else None,
                'chat_token': tokens.get(aid), 'env': OFFICIAL_ENV, 'command_seconds': COMMAND_SECONDS,
                'output_chars': OUTPUT_CHARS, 'session': str(uuid.uuid4())}, indent=1))
            if agent_cfg['kind'] == 'claude':
                readable(writable(home/'config')/'settings.json', json.dumps({'hooks': CLAUDE_HOOKS}))
            elif agent_cfg['kind'] == 'codex':
                readable(home/'config.toml', codex_config(agent_cfg))
            socket_dir = sockets/aid
            socket_dir.mkdir()
            os.chmod(socket_dir, 0o755)
            relay = None
            if agent_cfg.get('api_base'):
                relay = ModelRelay(socket_dir/'model.sock', agent_cfg, key, folder/'model-requests.jsonl')
                servers.append(relay)
            if tokens:
                address = urlsplit(chat_url)
                servers.append(ChatRelay(socket_dir/'chat.sock', (address.hostname, address.port)))
                threading.Thread(target=notify, args=(Client(chat_url, tokens[aid]), notices, stop),
                                 daemon=True).start()
            agent_mounts = ['-v', f'{agent_cfg["binary"]}:{binary}:ro', '-v', f'{guest}:/guest:ro',
                            *[a for f in agent_cfg['files'] for a in ('-v', f'{f}:/opt/agent/{f.name}:ro')],
                            '-v', f'{socket_dir}:/sockets', '-v', f'{home}:/agent-home', '-v', f'{notices}:/notices']
            if team:
                # An agent that may not push to the shared branch can still fetch it.
                agent_mounts += ['-v', f'{work/"shared"}:/shared'+('' if agent['role']['push'] else ':ro')]
                if incoming:
                    agent_mounts += ['-v', f'{work/"incoming"}:/incoming']
            name = re.sub(r'[^a-zA-Z0-9_.-]+', '-', f'pb-{config["name"]}-{instance}-{aid}')
            agents.append({'id': aid, 'folder': folder, 'guest': guest, 'home': home, 'relay': relay,
                           'container': name, 'mounts': agent_mounts})
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        for a in agents:
            docker('run', '-d', '--name', a['container'], '--network', 'none', '--user', 'agent',
                   '--cap-drop', 'SYS_PTRACE', '--cpus', config['docker']['cpus'],
                   '--memory', config['docker']['memory'], '--memory-swap', config['docker']['memory'],
                   '--ulimit', f'fsize={FILE_CAP}:{FILE_CAP}', '--pids-limit', PIDS_CAP, '-w', '/workspace',
                   *[f'--env={k}={v}' for k, v in OFFICIAL_ENV.items()], *mounts, *a['mounts'],
                   image, 'python3', '/guest/guest.py')
            containers.append(a['container'])
        log(instance, f'{len(agents)} agent(s) started for {config["hours"]:g} hour(s)')

        def submit(destination):
            if team:
                export_branch(work/'shared'/'repo.git', branch, destination)
            else:
                copy_workspace(docker, agents[0]['container'], destination)

        snapshots, checked, lost = 0, time.time(), set()
        status = 'TimeLimit'
        while time.time() < deadline:
            if interrupted.is_set():
                status = 'Interrupted'
                break
            if all((a['home']/'exit.json').exists() for a in agents):
                status = 'Finished'
                for a in agents:
                    error = json.loads((a['home']/'exit.json').read_text()).get('error')
                    if error:
                        status = 'AgentsFailed'
                        log(instance, f'{a["id"]} failed: {error}. See {a["folder"]/"container.log"}')
                break
            if time.time()-checked > 60:
                checked = time.time()
                for a in agents:
                    running = docker('inspect', '-f', '{{.State.Running}}', a['container'], check=False).stdout.strip()
                    if running != 'true' and a['id'] not in lost:
                        lost.add(a['id'])
                        log(instance, f'{a["id"]}\'s container stopped early, see `docker logs {a["container"]}`')
                if len(lost) == len(agents):
                    status = 'ContainersStopped'
                    break
            minutes = config['snapshot_minutes']
            if minutes and time.time()-started >= (snapshots+1)*minutes*60:
                snapshots += 1
                try:
                    submit(out/'snapshots'/f'{round(snapshots*minutes):05d}'/instance/'submission.tar.gz')
                except (subprocess.SubprocessError, RuntimeError) as exc:
                    log(instance, f'snapshot failed: {exc}')
            time.sleep(5)
        # Stop the agents, and give them two minutes to end their turns.
        for a in agents:
            readable(a['guest']/'stop', '')
        wait_until = time.time()+120
        while time.time() < wait_until and not all((a['home']/'exit.json').exists() for a in agents):
            time.sleep(1)
        submit(final)
        log(instance, f'{status}: submission saved')
    finally:
        stop.set()
        if team:
            for a in agents:
                if a['container'] in containers:
                    try:
                        copy_workspace(docker, a['container'], a['folder']/'workspace.tar.gz')
                    except (subprocess.SubprocessError, RuntimeError) as exc:
                        log(instance, f'{a["id"]}: workspace not copied: {exc}')
        for a in agents:
            if a['container'] in containers:
                logs = docker('logs', a['container'], check=False)
                (a['folder']/'container.log').write_text(logs.stdout+logs.stderr)
                docker('rm', '-f', a['container'], check=False)
        for server in servers:
            server.shutdown()
            server.server_close()
        if chat_process:
            chat_process.terminate()
            chat_process.wait()
        shutil.rmtree(sockets, ignore_errors=True)
        # Files the container's user wrote, made deletable by you.
        docker('run', '--rm', '--network', 'none', '--user', 'agent', '-v', f'{work}:/work', image, 'sh', '-c',
               'chmod -R a+rwX /work 2>/dev/null; true', check=False)
        # A task with a submission is done, and a later run of the same config skips it.
        if final.exists():
            (work/'result.json').write_text(json.dumps({
                'task': instance, 'status': status, 'started': started, 'seconds': round(time.time()-started),
                'agents': {a['id']: {'model_requests': a['relay'] and a['relay'].requests,
                                     'failed_model_requests': a['relay'] and a['relay'].failed} for a in agents}},
                indent=1))


# Checks before anything starts.

def preflight(config, docker):
    agent = config['agent']
    for file in [agent['binary'], *agent['files']]:
        if not file.exists():
            raise SystemExit(f'{file} does not exist. The agent\'s files must be Linux x86_64 builds.')
    if agent['kind'] == 'codex' and not agent['files']:
        print('Note: no codex-code-mode-host beside the codex binary. Codex 0.159 and later need it to run tools.')
    if agent.get('api_key_env') and not os.environ.get(agent['api_key_env']):
        raise SystemExit(f'Set {agent["api_key_env"]} to your API key. It stays on this machine.')
    if shutil.which(docker.base[0]) is None:
        raise SystemExit(f'{docker.base[0]} is not installed.')
    info = docker('info', '--format', '{{.Architecture}}', check=False)
    if info.returncode:
        raise SystemExit(f'docker does not answer: {info.stderr.strip()}')
    if info.stdout.strip() not in ('x86_64', 'amd64'):
        raise SystemExit(f'Docker runs on {info.stdout.strip()}. The ProgramBench images are for Linux x86_64 only.')


def dry_run(config):
    team = config['team']
    if team is None:
        prompt, loop = prompts_for(config)
        print(f'One agent, {config["hours"]:g} hour(s).\n\n=== prompt\n{prompt}\n=== loop message\n{loop or "(none)"}\n')
        return
    print(f'A team of {team["members"]}, {config["hours"]:g} hour(s), chat {"on" if team["chat"] else "off"}.')
    for a in config['agents']:
        print(f'  {a["id"]}: {a["role"]["name"]}{"" if a["role"]["push"] else " (cannot push to the shared branch)"}')
    if team['chat']:
        for name, members in channels_for(config).items():
            print(f'  #{name}: {", ".join(members)}')
    for role in team['roles']:
        agent = next(a for a in config['agents'] if a['role'] is role)
        prompt, loop = prompts_for(config, agent)
        print(f'\n=== prompt of {agent["id"]} ({role["name"]})\n{prompt}\n=== loop message\n{loop or "(none)"}')
    if team['phase']:
        print(f'\n=== for the first {team["phase"]["minutes"]:g} minutes the loop message is\n'
              f'{team["phase"]["loop"].strip()}\n=== and then every agent is told\n{team["phase"]["notice"].strip()}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('config', help='a TOML file, such as configs/flat.toml')
    parser.add_argument('--task', action='append', help='run only this task (repeat for more)')
    parser.add_argument('--hours', type=float, help='agent time per task, in place of the config\'s')
    parser.add_argument('--name', help='the run\'s name, in place of the config\'s')
    parser.add_argument('--out', default=str(HERE/'runs'), help='where runs go (default: runs/ next to run.py)')
    parser.add_argument('--dry-run', action='store_true', help='show the team and its prompts, and start nothing')
    args = parser.parse_args()
    config = load_config(args.config, args)
    if args.dry_run:
        dry_run(config)
        return
    docker = Docker(config['docker']['command'])
    preflight(config, docker)
    out = Path(args.out).expanduser().resolve()/config['name']
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(config['path'], out/'config.toml')
    signal.signal(signal.SIGTERM, lambda *_: interrupted.set())
    failures = []

    def one(instance):
        try:
            run_task(config, instance, out, docker)
        except Exception as exc:
            failures.append(instance)
            log(instance, f'failed: {exc}')

    with ThreadPoolExecutor(max_workers=config['parallel']) as pool:
        futures = [pool.submit(one, t) for t in config['tasks']]
        try:
            while not all(f.done() for f in futures):
                time.sleep(1)
        except KeyboardInterrupt:
            print('Stopping: saving submissions and removing containers. This takes a minute or two.', flush=True)
            interrupted.set()
            for f in futures:
                f.cancel()
    print(f'\nSubmissions: {out/"submissions"}\nScore them: programbench eval {out/"submissions"}')
    if config['snapshot_minutes']:
        print(f'Snapshots: {out/"snapshots"}, each folder scored the same way.')
    if failures:
        sys.exit(f'{len(failures)} task(s) failed: {", ".join(failures)}')


if __name__ == '__main__':
    main()
