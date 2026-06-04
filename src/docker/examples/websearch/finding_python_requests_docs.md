The websearch software returns 5 lines of text per option.
<tool_call>
<tool_name>bash</tool_name>
<command>/home/agent/software/websearch "requests.Session.auth" > websearch_requests_session_auth.txt && head -n 15 websearch_requests_session_auth.txt</command>
<directory>/home/agent/tmp/</directory>
</tool_call>
