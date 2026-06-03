from flask import Flask, request
from secrets import compare_digest
import os
import subprocess


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=50000)
