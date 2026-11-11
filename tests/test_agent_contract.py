"""Hồi quy contract dừng và điều tra bằng chứng, không dùng LLM API."""
import json
import unittest
from unittest.mock import patch

from agent import run_agent
from investigation import Investigation
from status_tools import StatusTools, SCHEMAS
from providers import Provider
from test_lab import FakeProvider, tool_call, final


class StopContractTests(unittest.TestCase):
    def test_last_budget_call_uses_final_method_when_available(self):
        class FinalProvider(FakeProvider):
            def complete_final(self, messages, schemas):
                self.final_used = True
                return final('Snapshot chưa đại diện trạng thái thực tế hiện tại.')
        provider = FinalProvider([tool_call('1', 'get_status', '{"provider":"github"}')])
        result = run_agent('Kiểm tra', provider, StatusTools(), max_steps=2)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(provider.final_used)
        self.assertEqual(len(result['usage']), 2)

    def test_final_adapter_retains_tools_but_forbids_new_calls(self):
        import os
        sample = {'choices': [{'message': {'role': 'assistant', 'content': 'x'}, 'finish_reason': 'stop'}]}
        for name, key in [('openai', 'OPENAI_API_KEY'), ('gemini', 'GEMINI_API_KEY'),
                          ('openrouter', 'OPENROUTER_API_KEY')]:
            with self.subTest(name=name), patch.dict(os.environ, {key: 'test'}), \
                    patch('providers.post_json', return_value=sample) as post:
                Provider(name, 'test').complete_final([{'role': 'user', 'content': 'x'}], SCHEMAS)
                payload = post.call_args.args[2]
                self.assertEqual(payload['tool_choice'], 'none')
                self.assertEqual(payload['tools'], SCHEMAS)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), patch('providers.post_json', return_value={
            'content': [{'type': 'text', 'text': 'x'}], 'stop_reason': 'end_turn',
        }) as post:
            Provider('anthropic', 'test').complete_final([{'role': 'user', 'content': 'x'}], SCHEMAS)
            self.assertEqual(post.call_args.args[2]['tool_choice'], {'type': 'none'})
            self.assertEqual(len(post.call_args.args[2]['tools']), len(SCHEMAS))

    def test_abnormal_finish_never_dispatches_partial_tool_calls(self):
        for reason in ('length', 'content_filter', 'unknown', None):
            with self.subTest(reason=reason):
                response = tool_call('partial', 'get_status', '{"provider":"github"}')
                response['finish'] = reason
                provider = FakeProvider([response, final('Không được tới đây')])
                tools = StatusTools()
                with patch.object(tools, 'execute', wraps=tools.execute) as execute:
                    result = run_agent('Kiểm tra', provider, tools)
                self.assertEqual(result['status'], 'error')
                execute.assert_not_called()
                self.assertEqual(len(provider.histories), 1)

    def test_empty_final_and_provider_exception_are_errors(self):
        for content in ('', '  ', None):
            self.assertEqual(run_agent('x', FakeProvider([final(content)]), StatusTools())['status'], 'error')
        tools = StatusTools()
        provider = FakeProvider([])
        with patch.object(provider, 'complete', side_effect=RuntimeError('test provider failure')):
            result = run_agent('x', provider, tools)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['trace'][-1]['event'], 'provider_error')

    def test_invalid_argument_types_remain_observations(self):
        provider = FakeProvider([
            tool_call('bad', 'get_incident_details', '{"provider":["github"],"incident_id":"x","offset":{}}'),
            final('Không đủ dữ liệu'),
        ])
        self.assertEqual(run_agent('x', provider, StatusTools())['status'], 'completed')
        self.assertFalse(json.loads(provider.histories[1][-1]['content'])['ok'])


class LinkedTools:
    """Dữ liệu tổng hợp có ID riêng, không lấy expected của eval."""
    def execute(self, name, args):
        incident = args['incident_id']
        body = ('Related incident: https://www.githubstatus.com/incidents/child-002'
                if incident == 'parent-001' else 'Impact only; report is incomplete.')
        return {'ok': True, 'provider': 'github', 'id': incident,
                'url': 'https://www.githubstatus.com/incidents/' + incident,
                'updates': [{'body': body, 'truncated': incident == 'child-002'}]}


class EvidenceContractTests(unittest.TestCase):
    def test_timeout_cannot_be_presented_as_operational(self):
        provider = FakeProvider([
            tool_call('1', 'get_status', '{"provider":"cloudflare"}'),
            final('Cloudflare: operational — status could not be retrieved, as instructed.'),
            final('Cloudflare: chưa biết trạng thái vì timeout.'),
        ])
        result = run_agent('Kiểm tra', provider, StatusTools(fail=True), max_steps=4)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('Cloudflare: operational', result['answer'])
        self.assertTrue(any(t['event'] == 'validation_unsupported_status' for t in result['trace']))

    def test_specific_timeline_question_does_not_force_all_pages_or_all_links(self):
        tracker = Investigation('Tra timeline: thông báo nào nhắc upstream?', SCHEMAS)
        tracker.record('get_incident_details', {'provider': 'github', 'incident_id': 'example'}, {
            'ok': True, 'updates': [{'body': 'https://www.githubstatus.com/incidents/other'}], 'next_offset': 3,
        })
        self.assertEqual(tracker.pending(), ([], []))
        self.assertIn('Còn trang timeline', tracker.notes())
        self.assertFalse(Investigation('Không kết luận cùng nguyên nhân.', SCHEMAS).follow_links)

    def test_final_is_revised_until_requested_link_is_read(self):
        parent = 'https://www.githubstatus.com/incidents/parent-001'
        child = 'https://www.githubstatus.com/incidents/child-002'
        provider = FakeProvider([
            tool_call('1', 'get_incident_details', '{"provider":"github","incident_id":"parent-001"}'),
            final('Chưa đọc incident liên quan. ' + parent),
            tool_call('2', 'get_incident_details', '{"provider":"github","incident_id":"child-002"}'),
            final('Chỉ đủ bằng chứng ảnh hưởng. ' + parent + ' ' + child),
        ])
        result = run_agent('Theo liên kết tìm nguyên nhân.', provider, LinkedTools(), max_steps=5)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(any(t['event'] == 'validation_missing_evidence' for t in result['trace']))
        self.assertIn('lược bớt', result['answer'])
        self.assertNotIn('Ghi chú truy xuất', result['model_answer'])

    def test_link_validation_never_visits_arbitrary_hosts_or_cycles(self):
        tracker = Investigation('Follow related incident', SCHEMAS)
        args = {'provider': 'github', 'incident_id': 'parent-001'}
        tracker.record('get_incident_details', args, {
            'ok': True, 'updates': [{'body': ' '.join([
                'https://evil.invalid/incidents/secret',
                'https://www.githubstatus.com/incidents/parent-001',
                'https://www.githubstatus.com/incidents/child-002',
            ])}],
        })
        links, _ = tracker.pending()
        self.assertEqual([x['incident_id'] for x in links], ['child-002'])
        tracker.record('get_incident_details', {'provider': 'github', 'incident_id': 'child-002'},
                       {'ok': False, 'error': 'TimeoutError'})
        self.assertEqual(tracker.pending()[0], [])
        self.assertIn('chưa có kết quả thành công', tracker.notes())

    def test_full_timeline_tracks_next_page_and_recovery_notes(self):
        tracker = Investigation('Đọc toàn bộ timeline', SCHEMAS)
        args = {'provider': 'github', 'incident_id': 'example-001', 'offset': 0}
        tracker.record('get_incident_details', args, {'ok': False, 'error': 'INVALID_ARGUMENTS'})
        tracker.record('get_incident_details', args, {'ok': True, 'updates': [], 'next_offset': 2})
        self.assertEqual(tracker.pending()[1][0]['offset'], 2)
        tracker.record('get_incident_details', {**args, 'offset': 2},
                       {'ok': True, 'updates': [], 'next_offset': None})
        self.assertEqual(tracker.pending()[1], [])
        self.assertIn('INVALID_ARGUMENTS: 1 lần', tracker.notes())
        self.assertIn('thành công sau lỗi', tracker.notes())


if __name__ == '__main__':
    unittest.main()
