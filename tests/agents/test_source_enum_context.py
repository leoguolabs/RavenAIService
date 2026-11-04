"""Bounded header context must not discard an enum's initial value."""
import copy
import json
import sys

import pytest

from app.agents.evidence_context import MAX_CONTEXT_BYTES, SourceContextEnricher
from app.agents.source_function_spans import CEnumSpans
from app.agents import source_function_spans


def header(tmp_path, *, wide=False):
    enum = ['typedef enum', '{', '    IDLE = 0x20,' + (' /* ' + 'x' * 2000 + ' */' if wide else ''),
            '    RUNNING,', '    FINISHED,', '} Phase;']
    rows = ['/* prefix */'] * 48 + enum + ['/* padding */'] * 5 + ['#define target 1']
    path = tmp_path / 'repo/phase.h'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(rows) + '\n')
    event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
             'tool_input': {'command': 'rg -n target repo/phase.h'},
             'tool_response': {'stdout': 'repo/phase.h:60:#define target 1', 'exit_code': 0}}
    return path, event


@pytest.mark.asyncio
async def test_window_includes_initial_value_and_all_implicit_members(tmp_path):
    path, event = header(tmp_path)
    before = copy.deepcopy(event)
    result = await SourceContextEnricher(str(tmp_path)).after_tool(event, 'enum-search', {})
    text = result['hookSpecificOutput']['additionalContext']
    assert len(text.encode()) <= MAX_CONTEXT_BYTES
    window = json.loads(text.split('\n', 1)[1])['source_context'][0]
    assert window['lines'][0][0] == 49
    native = path.read_text().splitlines()
    for number in range(49, 55):
        assert [number, 0, native[number - 1]] in window['lines']
    assert [60, 0, '#define target 1'] in window['lines']
    assert 'function' not in window
    assert event == before


@pytest.mark.asyncio
async def test_enum_that_cannot_fit_retains_match_native_output_and_budget(tmp_path):
    _, event = header(tmp_path, wide=True)
    before = copy.deepcopy(event)
    hook = SourceContextEnricher(str(tmp_path))
    hook.window_budget = 350
    result = await hook.after_tool(event, 'bounded-enum', {})
    text = result['hookSpecificOutput']['additionalContext']
    window = json.loads(text.split('\n', 1)[1])['source_context'][0]
    assert len(text.encode()) <= MAX_CONTEXT_BYTES
    assert [60, 0, '#define target 1'] in window['lines']
    assert not any(row[0] == 51 for row in window['lines'])
    assert event == before


def test_enums_use_syntax_not_comment_string_or_forward_declarations(tmp_path):
    path = tmp_path / 'enum.h'
    path.write_text('''/* enum Fake { A, B }; */
const char *text = "enum Fake { A, B };";
enum Forward;
typedef enum Real {
    FIRST = EXTERNAL_VALUE,
    SECOND,
#if FEATURE
    OPTIONAL,
#endif
    LAST
} Real;
''')
    spans = CEnumSpans().get(path)
    assert [(s.start_line, s.end_line) for s in spans] == [(4, 11)]
    # The index gives source bounds, not evaluated values or active branches.
    assert set(vars(spans[0])) == {'start_line', 'end_line'}


@pytest.mark.asyncio
async def test_large_enum_does_not_expand_past_line_budget(tmp_path):
    path = tmp_path / 'repo/large.h'
    path.parent.mkdir()
    rows = ['enum Large {', '    FIRST = 64,'] + [f'    VALUE_{n},' for n in range(170)]
    rows += ['};'] + ['/* padding */'] * 5 + ['#define target 1']
    path.write_text('\n'.join(rows) + '\n')
    data = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': 'rg -n target repo/large.h'},
            'tool_response': {'stdout': f'repo/large.h:{len(rows)}:#define target 1', 'exit_code': 0}}
    windows = SourceContextEnricher(str(tmp_path)).collect(data)
    assert windows[0]['lines'][0]['line'] == len(rows) - 8
    assert windows[0]['lines'][-1]['line'] == len(rows)
    assert not any('FIRST' in row['text'] for row in windows[0]['lines'])


@pytest.mark.parametrize('text', ['enum Broken { FIRST = ;', 'enum Forward;',
                                  'const char *s = "enum Fake { A };";'])
def test_incomplete_or_non_enum_source_has_no_range(tmp_path, text):
    path = tmp_path / 'broken.h'
    path.write_text(text)
    assert CEnumSpans().get(path) == ()


def test_enum_cache_tracks_content_even_with_unchanged_stat_identity(tmp_path, monkeypatch):
    path = tmp_path / 'phase.h'
    path.write_text('typedef enum   { A = 3, B } Phase;\n')
    parser = CEnumSpans()
    identity = source_function_spans.source_identity(path)
    monkeypatch.setattr(source_function_spans, 'source_identity', lambda _: identity)
    assert parser.get(path)
    path.write_text(path.read_text().replace('enum  ', 'struct'))
    assert parser.get(path) == ()
    for i in range(6):
        other = tmp_path / f'phase{i}.h'
        other.write_text('enum Other { X, Y };\n')
        parser.get(other)
    assert len(parser.cache) == 4


def test_enum_mutated_during_parse_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / 'phase.h'
    path.write_text('enum Phase { FIRST, SECOND };\n')
    parser = CEnumSpans()
    original = parser._parse

    def changing(raw):
        result = original(raw)
        path.write_text(path.read_text() + '// changed\n')
        return result

    monkeypatch.setattr(parser, '_parse', changing)
    assert parser.get(path) == ()
    assert not parser.cache


def test_enum_parser_is_optional_and_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'phase.h'
    path.write_text('enum Phase { FIRST, SECOND };\n')
    monkeypatch.setitem(sys.modules, 'tree_sitter', None)
    assert CEnumSpans().get(path) == ()
    monkeypatch.setattr(source_function_spans, 'MAX_PARSE_BYTES', 8)
    assert CEnumSpans().get(path) == ()
    path = tmp_path / 'unsupported.cpp'
    path.write_text('enum Phase { FIRST, SECOND };\n')
    assert CEnumSpans().get(path) == ()
