#!/usr/bin/env python3
"""User-owned remote dashboard services. No sudo or system-network changes."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

from labcore.runner import identity

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'runtime/dashboard-access'
TAIL_STATE = ROOT / 'runtime/tailscale'
TAIL = ROOT / 'runtime/tools/tailscale/tailscale'
TAILD = TAIL.with_name('tailscaled')
CADDY = ROOT / 'runtime/tools/caddy/caddy'
SOCKET = TAIL_STATE / 'tailscaled.sock'
PORT = 8877


def tail(*args, **kwargs):
    return subprocess.run([str(TAIL), f'--socket={SOCKET}', *args], text=True, **kwargs)


def alive(path):
    try:
        state = json.loads(path.read_text())
        return bool(state.get('identity')) and identity(state['pid']) == state['identity']
    except (OSError, ValueError, KeyError):
        return False


def launch(args, folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    env=dict(os.environ,XDG_CONFIG_HOME=str(folder/'config'),XDG_DATA_HOME=str(folder/'data'))
    with (folder / 'service.log').open('ab') as log:
        proc = subprocess.Popen(list(map(str, args)), cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077, env=env)
    time.sleep(.5)
    if proc.poll() is not None:
        raise RuntimeError(f'Service did not start; inspect {folder}/service.log')
    (folder / 'process.json').write_text(json.dumps(dict(pid=proc.pid, identity=identity(proc.pid))))
    return proc.pid


def stop_proxy():
    path = STATE / 'process.json'
    if not alive(path):
        return
    state = json.loads(path.read_text())
    if identity(state['pid']) != state['identity']:
        raise RuntimeError('Proxy identity changed; refusing to stop an unverified process')
    os.kill(state['pid'], signal.SIGTERM)
    for _ in range(100):
        if identity(state['pid']) != state['identity']:
            return
        time.sleep(.1)
    raise RuntimeError('Password proxy did not stop cleanly')


def ensure_tailscale():
    TAIL_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not alive(TAIL_STATE / 'process.json'):
        folder = TAIL_STATE / 'state'
        folder.mkdir(mode=0o700, exist_ok=True)
        launch([TAILD, '--tun=userspace-networking', '--port=0', f'--socket={SOCKET}',
                f'--statedir={folder}', f'--state={folder}/tailscaled.state'], TAIL_STATE)
    data = json.loads(tail('status', '--json', capture_output=True, check=True, timeout=15).stdout)
    if data.get('BackendState') != 'Running':
        raise RuntimeError('Tailscale needs login. Run runtime/tools/tailscale/tailscale '
                           f'--socket={SOCKET} up --accept-dns=false --accept-routes=false --hostname=research-observatory')
    name = data['Self']['DNSName'].rstrip('.')
    if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in name):
        raise RuntimeError('Invalid Tailscale DNS name')
    return name


def configure(name):
    path = STATE / 'credentials.json'
    if not path.exists():
        path.write_text(json.dumps(dict(username='researcher', password=secrets.token_urlsafe(24))) + '\n')
    path.chmod(0o600)
    credentials = json.loads(path.read_text())
    hashed = subprocess.run([str(CADDY), 'hash-password', '--algorithm', 'bcrypt'], input=credentials['password'], text=True,
                            capture_output=True, check=True, timeout=30).stdout.strip()
    config = '''{
    admin off
    auto_https off
    servers {
        timeouts {
            read_header 10s
            read_body 30s
            write 120s
            idle 60s
        }
        max_header_size 16KB
    }
}
:PORT {
    bind 127.0.0.1
    route {
        @bad_host not host HOSTNAME
        respond @bad_host "Unrecognized dashboard address" 403
        basic_auth bcrypt "Research dashboard" {
            USERNAME HASH
        }
        @foreign_origin {
            header Origin *
            not header Origin https://HOSTNAME
        }
        respond @foreign_origin "Cross-origin request denied" 403
        @cross_site {
            method POST PUT PATCH DELETE
            header Sec-Fetch-Site cross-site
        }
        respond @cross_site "Cross-site request denied" 403
        header Strict-Transport-Security "max-age=31536000"
        reverse_proxy 127.0.0.1:8765 {
            header_up Host 127.0.0.1:8765
            header_up Origin http://127.0.0.1:8765
            header_up -Cookie
        }
    }
}
'''.replace('PORT', str(PORT)).replace('HOSTNAME', name).replace('USERNAME', credentials['username']).replace('HASH', hashed)
    target = STATE / 'Caddyfile'
    target.write_text(config)
    target.chmod(0o600)
    subprocess.run([str(CADDY), 'validate', '--config', str(target), '--adapter', 'caddyfile'],
                   capture_output=True, check=True, timeout=30)
    (STATE / 'settings.json').write_text(json.dumps(dict(url=f'https://{name}', proxy_port=PORT)) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'credentials', 'restart-proxy'])
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    if args.action == 'credentials':
        credentials = json.loads((STATE / 'credentials.json').read_text())
        print('Username:', credentials['username'])
        print('Password:', credentials['password'])
        return
    if args.action == 'status':
        print('Tailscale running:', alive(TAIL_STATE / 'process.json'))
        print('Password proxy running:', alive(STATE / 'process.json'))
        if (STATE / 'settings.json').exists():
            print('Configured URL:', json.loads((STATE / 'settings.json').read_text())['url'])
        tail('funnel', 'status', check=True)
        return
    name = ensure_tailscale()
    if args.action=='restart-proxy':stop_proxy()
    settings = STATE / 'settings.json'
    current_url = json.loads(settings.read_text()).get('url') if settings.exists() else None
    if current_url != f'https://{name}':
        stop_proxy()
    if not alive(STATE / 'process.json'):
        configure(name)
        launch([CADDY, 'run', '--config', STATE / 'Caddyfile', '--adapter', 'caddyfile'], STATE)
    print('Password-protected proxy running on loopback port', PORT)
    print('HTTPS address:', f'https://{name}')
    print('Funnel is a separate step; this command does not publish the dashboard.')


if __name__ == '__main__':
    main()
