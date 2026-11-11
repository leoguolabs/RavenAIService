"""Exercise the official SDK loop against a local Responses SSE server."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager, aclosing
from types import SimpleNamespace

import pytest
import uvicorn
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, ToolResultBlock,
    ToolUseBlock, UserMessage, create_sdk_mcp_server, tool,
)
from claude_agent_sdk.types import StreamEvent
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.openai_agents_executor import build_tools, input_items, query
from app.agents.antigravity_control import Control
from app.agents.openai_agents_tools import WorkspaceTools
from app.agents.openai_bridge import _LoopbackServer


@asynccontextmanager
async def responses_server(handler):
    app = FastAPI()
    app.post('/v1/responses')(handler)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = _LoopbackServer(uvicorn.Config(app, log_level='error', lifespan='off', timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


def response_events(output, *, text='', index=1):
    response = {'id': f'resp_{index}', 'object': 'response', 'created_at': 1,
        'model': 'fixture/model', 'status': 'completed', 'output': output, 'tools': [],
        'tool_choice': 'auto', 'parallel_tool_calls': False,
        'usage': {'input_tokens': 12, 'input_tokens_details': {'cached_tokens': 2},
                  'output_tokens': 4, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 16}}
    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}]
    if text:
        for delta in (text[:3], text[3:]):
            events.append({'type': 'response.output_text.delta', 'item_id': f'msg_{index}',
                           'content_index': 0, 'output_index': 0, 'delta': delta, 'logprobs': []})
    for i, item in enumerate(output):
        events.append({'type': 'response.output_item.done', 'output_index': i, 'item': item})
    events.append({'type': 'response.completed', 'response': response})
    return ''.join('event: ' + row['type'] + '\ndata: ' + json.dumps({**row, 'sequence_number': i}) + '\n\n'
                   for i, row in enumerate(events))


def message(text):
    return {'id': 'msg_final', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': [], 'logprobs': []}]}


def options(tmp_path, url, **kwargs):
    return ClaudeAgentOptions(cwd=str(tmp_path), model='fixture/model', max_turns=5,
        env={'ANTHROPIC_BASE_URL': url, 'ANTHROPIC_API_KEY': 'ephemeral-fixture-token',
             'API_TIMEOUT_MS': '10000'}, **kwargs)


@pytest.mark.asyncio
async def test_official_sdk_streams_real_mcp_roundtrip_and_usage(tmp_path):
    requests, invoked = [], []
    @tool('read', 'Read fixture evidence', {'value': str})
    async def read(args):
        invoked.append(args)
        return {'content': [{'type': 'text', 'text': 'proof-' + args['value']}]}

    async def respond(request: Request):
        assert request.headers['authorization'] == 'Bearer ephemeral-fixture-token'
        body = await request.json()
        requests.append(body)
        assert body['model'] == 'fixture/model' and body['stream'] and not body['store']
        if len(requests) == 1:
            output = [{'id': 'fc_one', 'type': 'function_call', 'name': 'mcp__fixture__read',
                       'call_id': 'call_one', 'arguments': '{"value":"unique"}', 'status': 'completed'}]
            wire = response_events(output)
        else:
            assert any(item.get('type') == 'function_call_output' and 'proof-unique' in str(item['output'])
                       for item in body['input'])
            wire = response_events([message('proof-unique')], text='proof-unique', index=2)
        return StreamingResponse(iter([wire]), media_type='text/event-stream')

    async with responses_server(respond) as url:
        opts = options(tmp_path, url, permission_mode='bypassPermissions',
            mcp_servers={'fixture': create_sdk_mcp_server('fixture', tools=[read])})
        messages = [row async for row in query(prompt='Read evidence', options=opts)]
    assert invoked == [{'value': 'unique'}] and len(requests) == 2
    assert ''.join(row.event['delta']['text'] for row in messages if isinstance(row, StreamEvent)) == 'proof-unique'
    calls = [block for row in messages if isinstance(row, AssistantMessage) for block in row.content if isinstance(block, ToolUseBlock)]
    outputs = [block for row in messages if isinstance(row, UserMessage) for block in row.content if isinstance(block, ToolResultBlock)]
    assert len(calls) == len(outputs) == 1 and calls[0].id == outputs[0].tool_use_id == 'call_one'
    terminal = messages[-1]
    assert isinstance(terminal, ResultMessage) and not terminal.is_error and terminal.result == 'proof-unique'
    assert terminal.usage['input_tokens'] == 20
    assert terminal.usage['output_tokens'] == 8 and terminal.usage['cache_read_input_tokens'] == 4


@pytest.mark.asyncio
async def test_tool_permissions_rewrite_hooks_skills_and_disabled_surface(tmp_path):
    (tmp_path / 'evidence.txt').write_text('one\ntwo\nthree\n')
    skill = tmp_path / '.claude/skills/demo'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('Skill evidence')
    decisions, messages = [], []
    async def permission(name, args, context):
        decisions.append((name, context.tool_use_id))
        return {'behavior': 'deny'} if name == 'Write' else {'behavior': 'allow'}
    async def before(data, tool_id, context):
        return {'hookSpecificOutput': {'updatedInput': {**data['tool_input'], 'offset': 2, 'limit': 1}}}
    async def after(data, tool_id, context):
        return {'hookSpecificOutput': {'additionalContext': 'verified-hook'}}
    async def emit(row):
        messages.append(row)
    opts = options(tmp_path, 'http://unused', allowed_tools=['Read', 'Write', 'Skill', 'Bash'],
        disallowed_tools=['Bash'], setting_sources=['project'], can_use_tool=permission,
        hooks={'PreToolUse': [HookMatcher(matcher='Read', hooks=[before])],
               'PostToolUse': [HookMatcher(hooks=[after])]})
    tools = {tool.name: tool for tool in build_tools(Control(opts), emit, 'fixture')}
    assert 'Bash' not in tools
    for name, args in [('Read', {'file_path': 'evidence.txt'}), ('Write', {'file_path': 'denied.txt', 'content': 'bad'}), ('Skill', {'skill': 'demo'})]:
        result = await tools[name].on_invoke_tool(SimpleNamespace(tool_call_id=name), json.dumps(args))
        if name == 'Read':
            assert '2\ttwo' in result and 'one' not in result and 'verified-hook' in result
        elif name == 'Skill':
            assert 'Skill evidence' in result
    assert not (tmp_path / 'denied.txt').exists()
    assert decisions == [('Read', 'Read'), ('Write', 'Write'), ('Skill', 'Skill')]
    assert messages[3].content[0].is_error


@pytest.mark.asyncio
@pytest.mark.parametrize('always_block', [False, True])
async def test_stop_continuation_obeys_total_turn_budget(tmp_path, always_block):
    requests, stops = [], []
    async def respond(request: Request):
        requests.append(await request.json())
        text = 'first' if len(requests) == 1 else 'reviewed'
        return StreamingResponse(iter([response_events([message(text)], text=text, index=len(requests))]), media_type='text/event-stream')
    async def stop(data, tool_id, context):
        stops.append(data)
        return {'decision': 'block', 'reason': 'Review the evidence'} if always_block or len(stops) == 1 else {}
    async with responses_server(respond) as url:
        opts = options(tmp_path, url, hooks={'Stop': [HookMatcher(hooks=[stop])]})
        opts.max_turns = 2
        async with asyncio.timeout(10):
            messages = [row async for row in query(prompt='answer', options=opts)]
    assert stops[0]["stop_hook_active"] is False
    assert stops[1]["stop_hook_active"] is True
    assert len(requests) == 2 and requests[1]['input'][-1]['content'] == 'Review the evidence'
    assert messages[-1].is_error == always_block
    assert messages[-1].num_turns == 2
    if not always_block:
        assert messages[-1].result == 'reviewed'


@pytest.mark.asyncio
async def test_cancellation_closes_official_sdk_and_tool_execution(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()
    @tool('wait', 'Wait for cancellation', {})
    async def wait(args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()
    async def respond(request: Request):
        output = [{'id': 'fc_wait', 'type': 'function_call', 'name': 'mcp__fixture__wait',
                   'call_id': 'call_wait', 'arguments': '{}', 'status': 'completed'}]
        return StreamingResponse(iter([response_events(output)]), media_type='text/event-stream')
    async with responses_server(respond) as url:
        opts = options(tmp_path, url, permission_mode='bypassPermissions',
            mcp_servers={'fixture': create_sdk_mcp_server('fixture', tools=[wait])})
        async def consume():
            async with aclosing(query(prompt='wait', options=opts)) as stream:
                async for _ in stream:
                    pass
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(closed.wait(), 2)


@pytest.mark.asyncio
async def test_workspace_tools_cannot_escape_or_inherit_keys(tmp_path, monkeypatch):
    workspace = WorkspaceTools(options(tmp_path, 'http://unused'))
    with pytest.raises(ValueError, match='outside'):
        await workspace.call('Read', {'file_path': '../secret'})
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-never-pass')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/run/fixture/agent.sock')
    result = await workspace.call('Bash', {'command': 'printf "%s\\n" "${OPENAI_API_KEY-unset}" "${SSH_AUTH_SOCK-unset}"'})
    assert result['stdout'].splitlines() == ['unset', 'unset']
    await workspace.call('Write', {'file_path': 'file', 'content': 'hello'})
    await workspace.call('Edit', {'file_path': 'file', 'old_string': 'hello', 'new_string': 'updated'})
    assert (tmp_path / 'file').read_text() == 'updated'


@pytest.mark.asyncio
async def test_bash_cancellation_terminates_children_after_shell_exits(tmp_path):
    import os
    import subprocess

    workspace = WorkspaceTools(options(tmp_path, 'http://unused'))
    task = asyncio.create_task(workspace.call('Bash', {
        'command': 'sleep 300 & echo "$$ $!" > processes.txt'}))
    child = None
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / 'processes.txt').exists():
                await asyncio.sleep(.01)
            parent, child = map(int, (tmp_path / 'processes.txt').read_text().split())
            while True:
                try:
                    os.kill(parent, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
        os.kill(child, 0)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        async with asyncio.timeout(3):
            while True:
                status = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
                if not status or status.startswith('Z'):
                    break
                await asyncio.sleep(.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if child is not None:
            from contextlib import suppress
            import signal
            with suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


@pytest.mark.asyncio
async def test_materialized_skill_links_are_readable_but_not_writable(tmp_path):
    from app.services.skills_service import _materialize_skill_sources

    shared = tmp_path / 'shared/demo'
    shared.mkdir(parents=True)
    (shared / 'SKILL.md').write_text('Read references/sample.log for proof')
    (shared / 'references').mkdir()
    (shared / 'references/sample.log').write_text('skill-link-proof')
    cwd = tmp_path / 'workspace'
    _materialize_skill_sources([({'name': 'demo'}, shared)], cwd)
    workspace = WorkspaceTools(options(cwd, 'http://unused', setting_sources=['project']))
    assert 'Read references' in await workspace.call('Skill', {'skill': 'demo'})
    reference = '.claude/skills/demo/references/sample.log'
    assert 'skill-link-proof' in await workspace.call('Read', {'file_path': reference})
    assert 'sample.log' in await workspace.call('Glob', {'path': '.claude/skills/demo', 'pattern': '**/*.log'})
    assert 'skill-link-proof' in await workspace.call('Grep', {'path': reference, 'pattern': 'proof', 'output_mode': 'content'})
    with pytest.raises(ValueError, match='outside'):
        await workspace.call('Write', {'file_path': reference, 'content': 'changed'})
    with pytest.raises(ValueError, match='outside'):
        await workspace.call('Edit', {'file_path': reference, 'old_string': 'proof', 'new_string': 'changed'})
    secret = tmp_path / 'secret.txt'
    secret.write_text('private')
    (shared / 'leak.txt').symlink_to(secret)
    (cwd / '.claude/skills/late').symlink_to(tmp_path, target_is_directory=True)
    for path in ['.claude/skills/demo/leak.txt', '.claude/skills/late/secret.txt']:
        with pytest.raises(ValueError, match='outside'):
            await workspace.call('Read', {'file_path': path})
    disabled = WorkspaceTools(options(cwd, 'http://unused'))
    with pytest.raises(ValueError, match='outside'):
        await disabled.call('Read', {'file_path': reference})


@pytest.mark.asyncio
async def test_multimodal_prompt_preserves_images(tmp_path):
    async def prompt():
        yield {'message': {'content': [{'type': 'text', 'text': 'describe'},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}]}}
    items = await input_items(prompt(), tmp_path)
    assert items[0]['content'][1]['image_url'] == 'data:image/png;base64,fixture'


@pytest.mark.asyncio
async def test_mcp_long_names_images_and_denied_calls_keep_original_ids(tmp_path):
    events, dispatched, decisions = [], [], []
    server = 'fixture_' + 'long_' * 8
    remote = 'read_' + 'long_' * 8
    canonical = f'mcp__{server}__{remote}'
    async def permission(name, args, context):
        decisions.append((name, context.tool_use_id))
        return {'behavior': 'deny' if args.get('deny') else 'allow'}
    async def emit(row):
        events.append(row)
    control = Control(options(tmp_path, 'http://unused', can_use_tool=permission))
    control.catalog = {server: [{'name': remote, 'inputSchema': {'type': 'object', 'properties': {}}}]}
    async def call(name, args):
        dispatched.append(args)
        return SimpleNamespace(model_dump=lambda **kw: {'content': [
            {'type': 'image', 'mimeType': 'image/png', 'data': 'fixture'}]})
    control.clients[server] = SimpleNamespace(call_tool=call)
    wrapped, = build_tools(control, emit, 'fixture')
    assert len(wrapped.name) <= 64
    for denied in (True, False):
        result = await wrapped.on_invoke_tool(SimpleNamespace(tool_call_id=f'id-{denied}'), json.dumps({'deny': denied}))
    assert result == [{'type': 'image', 'image_url': 'data:image/png;base64,fixture'}]
    assert dispatched == [{'deny': False}]
    assert decisions == [(canonical, 'id-True'), (canonical, 'id-False')]
    assert events[0].content[0].name == canonical and events[1].content[0].is_error


@pytest.mark.asyncio
async def test_model_turn_cap_returns_failure_and_usage(tmp_path):
    (tmp_path / 'evidence.txt').write_text('evidence')
    async def respond(request: Request):
        output = [{'id': 'fc_read', 'type': 'function_call', 'name': 'Read',
                   'call_id': 'call_read', 'arguments': '{"file_path":"evidence.txt"}', 'status': 'completed'}]
        return StreamingResponse(iter([response_events(output)]), media_type='text/event-stream')
    async with responses_server(respond) as url:
        opts = options(tmp_path, url, allowed_tools=['Read'], permission_mode='bypassPermissions')
        opts.max_turns = 1
        async with asyncio.timeout(5):
            messages = [row async for row in query(prompt='read', options=opts)]
    assert messages[-1].is_error and messages[-1].subtype == 'error_max_turns'
    assert messages[-1].usage['output_tokens'] == 4


@pytest.mark.asyncio
async def test_run_configuration_disables_tracing_and_does_not_set_global_credentials(tmp_path, monkeypatch):
    from agents import Runner
    original = Runner.run_streamed
    configs = []
    def start(*args, **kwargs):
        configs.append(kwargs['run_config'])
        return original(*args, **kwargs)
    monkeypatch.setattr(Runner, 'run_streamed', start)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    async def respond(request: Request):
        return StreamingResponse(iter([response_events([message('ok')], text='ok')]), media_type='text/event-stream')
    async with responses_server(respond) as url:
        messages = [row async for row in query(prompt='ok', options=options(tmp_path, url))]
    import os
    assert 'OPENAI_API_KEY' not in os.environ and messages[-1].result == 'ok'
    assert all(c.tracing_disabled and not c.trace_include_sensitive_data for c in configs)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 404])
async def test_official_sdk_through_magpie_bridge_routes_and_sanitizes_errors(tmp_path, monkeypatch, status):
    from app.agents import provider_query
    from app.agents.magpie_bridge import GatewayRequestError
    from app.services import agent_runtime_service as runtime
    requests = []
    async def respond(request: Request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer gateway-private'
        assert request.headers['user-agent'] == 'RavenAI/openai_agents'
        assert request.headers['x-magpie-agent'] == 'raven-general_agent'
        if status != 200:
            return JSONResponse({'error': 'provider switched off in Magpie gateway-private raw-secret'}, status_code=status)
        return StreamingResponse(iter([response_events([message('served')], text='served')]),
            media_type='text/event-stream', headers={'x-magpie-provider': 'fixture', 'x-magpie-model': 'fixture/model'})
    async with responses_server(respond) as url:
        config = runtime.RuntimeConfig(enabled=True, executor='openai_agents', api_key='gateway-private', model='group/test', base_url=url)
        monkeypatch.setattr(runtime, 'get_config', lambda: config)
        opts = options(tmp_path, url)
        opts.model = 'group/test'
        opts.env.update(RAVEN_GATEWAY='magpie', RAVEN_EXECUTOR='openai_agents', RAVEN_AGENT_KIND='general_agent', ANTHROPIC_API_KEY='gateway-private')
        if status == 200:
            rows = [row async for row in provider_query.query(prompt='test', options=opts)]
            route = next(row.data for row in rows if getattr(row, 'subtype', '') == 'raven_route')
            assert route['executor'] == 'openai_agents' and route['model'] == 'fixture/model'
            assert route['requested_model'] == 'group/test' and rows[-1].result == 'served'
        else:
            with pytest.raises(GatewayRequestError, match='Selected provider is disabled') as error:
                _ = [row async for row in provider_query.query(prompt='test', options=opts)]
            assert 'raw-secret' not in str(error.value) and 'gateway-private' not in str(error.value)
    assert len(requests) == 1
