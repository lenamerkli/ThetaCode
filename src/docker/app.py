from flask import Flask, request
from secrets import compare_digest
import os
import subprocess
from pathlib import Path


ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')


app = Flask(__name__)


@app.before_request
def check_access():
    token = request.headers.get('Authorization', '')
    if not compare_digest(token, f'Bearer {ACCESS_TOKEN}'):  # noqa
        return {'error': 'Unauthorized'}, 401


@app.route('/', methods=['GET'])
def index():
    return {'status': 'ok'}


@app.route('/execute', methods=['POST'])
def execute():
    data = request.get_json()
    command = data.get('command', '')
    timeout = data.get('timeout', 60)
    directory = data.get('directory', '/home/agent/')
    venv = data.get('venv', None)
    if venv:
        command = f"source {venv}/bin/activate && {command}"
    if not command:
        return {'error': 'Missing command'}, 400
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=directory
        )
        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        }
    except Exception as e:
        return {'error': str(e)}, 500


@app.route('/read_file', methods=['POST'])
def read_file():
    data = request.get_json()
    path_str = data.get('path', '')
    if not path_str:
        return {'error': 'Missing path'}, 400
    try:
        path = Path(path_str)
        if not path.exists():
            return {'error': f'File not found: {path_str}'}, 404
        if not path.is_file():
            return {'error': f'Not a file: {path_str}'}, 400
        content = path.read_text(encoding='utf-8', errors='replace')
        return {'content': content}
    except ValueError as e:
        return {'error': str(e)}, 400
    except Exception as e:
        return {'error': str(e)}, 500


@app.route('/write_to_file', methods=['POST'])
def write_to_file():
    data = request.get_json()
    path_str = data.get('path', '')
    content = data.get('content', '')
    if not path_str:
        return {'error': 'Missing path'}, 400
    try:
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return {'ok': True}
    except ValueError as e:
        return {'error': str(e)}, 400
    except Exception as e:
        return {'error': str(e)}, 500


@app.route('/replace_in_file', methods=['POST'])
def replace_in_file():
    data = request.get_json()
    path_str = data.get('path', '')
    search = data.get('search', '')
    replace = data.get('replace', '')
    if not path_str:
        return {'error': 'Missing path'}, 400
    if not search:
        return {'error': 'Missing search text'}, 400
    try:
        path = Path(path_str)
        if not path.exists():
            return {'error': f'File not found: {path_str}'}, 404
        if not path.is_file():
            return {'error': f'Not a file: {path_str}'}, 400
        current_content = path.read_text(encoding='utf-8')
        if search not in current_content:
            return {'error': 'Search text not found in file'}, 400
        new_content = current_content.replace(search, replace, 1)
        path.write_text(new_content, encoding='utf-8')
        return {'ok': True}
    except ValueError as e:
        return {'error': str(e)}, 400
    except Exception as e:
        return {'error': str(e)}, 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=50000)
