"""Execute one authenticated management request from INSIDE the container."""
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
        config = json.loads(Path('/AstrBot/data/config/astrbot_plugin_roleplay_gateway_config.json').read_text(encoding='utf-8-sig'))
        status = payload == {'op': 'status'}
        request = urllib.request.Request('http://127.0.0.1:6199/status' if status else 'http://127.0.0.1:6199/manage',
                    data=None if status else json.dumps(payload).encode(),
                    headers={'Authorization': 'Bearer ' + config['native_bridge_token'], 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=48) as response:
                value = json.loads(response.read(8 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            value = json.loads(exc.read(4096))
        print(json.dumps(value, ensure_ascii=True))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'management_unavailable'}))


if __name__ == '__main__':
    main()
