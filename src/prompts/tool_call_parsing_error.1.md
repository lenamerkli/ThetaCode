The tool call you tried to make is unable to be parsed. You must use one and only one tool call at a time and then end your turn of the conversation. You must include the <tool_call> XML tags. Tool calling is explained in the system message at the start of the conversation. If you want to interact with the user, use the `ask_user` tool. Review this parser to understand the exact format required:
```python
def _parse_tool_param(options: str, param_name: str, default_value: str = '') -> str:
    open_tag = f'<{param_name}>'
    close_tag = f'</{param_name}>'
    if open_tag in options and close_tag in options:
        return options.split(open_tag, 1)[-1].rsplit(close_tag, 1)[0].strip()
    return default_value

options = content.split('<tool_call>', 1)[-1].rsplit('</tool_call>', 1)[0].strip()
tool_name = _parse_tool_param(options, 'tool_name')
match tool_name:
    case 'read_file':
        path       = _parse_tool_param(options, 'path')
        start_line = int(_parse_tool_param(options, 'start_line', '1') or '1')
        end_line   = int(_parse_tool_param(options, 'end_line', '1000') or '1000')
        start_char = int(_parse_tool_param(options, 'start_char', '0') or '0')
        end_char   = int(_parse_tool_param(options, 'end_char', '100000') or '100000')
        max_chars  = int(_parse_tool_param(options, 'max_chars', '1000000') or '1000000')
        return _tool_read_file(path, start_line, end_line, start_char, end_char, max_chars)
    case 'write_to_file':
        path    = _parse_tool_param(options, 'path')
        content = _parse_tool_param(options, 'content')
        return _tool_write_to_file(path, content)
    case 'replace_in_file':
        path    = _parse_tool_param(options, 'path')
        search  = _parse_tool_param(options, 'search')
        replace = _parse_tool_param(options, 'replace')
        return _tool_replace_in_file(path, search, replace)
    case 'bash':
        command   = _parse_tool_param(options, 'command')
        timeout   = int(_parse_tool_param(options, 'timeout', '60') or '60')
        directory = _parse_tool_param(options, 'directory', '/home/agent/')
        venv      = _parse_tool_param(options, 'venv')
        max_chars = int(_parse_tool_param(options, 'max_chars', '100000') or '100000')
        return _tool_bash(command, timeout, directory, venv, max_chars)
    case 'ask_user':
        question = _parse_tool_param(options, 'question')
        return _tool_ask_user(question)
    case _:
        return f'Unknown tool: {tool_name}'
```
