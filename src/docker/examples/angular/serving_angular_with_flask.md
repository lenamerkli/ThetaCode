```python
@app.errorhandler(404)
def error_handler_404(*_, **__):
    if DEVELOPMENT:
        res = requests_send(
            method=request.method,
            url='http://' + request.url.replace(request.host_url, 'localhost:4200/'),
            headers={k: v for k, v in request.headers if k.lower() != 'host'},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=True,
        )
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [
            (k, v) for k, v in res.raw.headers.items()
            if k.lower() not in excluded_headers
        ]
        response = Response(res.content, res.status_code, headers)  # noqa
        return response
    else:
        path = request.path
        if path and path.startswith('/'):
            path = path[1:]
        if path != '' and exists(join(app.root_path, 'web', path)):
            return send_from_directory(join(app.root_path, 'web'), path), 200
        else:
            return send_from_directory(join(app.root_path, 'web'), 'index.html'), 200
```
