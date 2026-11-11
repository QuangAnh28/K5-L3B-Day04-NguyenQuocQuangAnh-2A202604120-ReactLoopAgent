"""Chứng minh model không thể gọi vượt budget retry hoặc Retry-After."""
import json
import unittest
from unittest.mock import patch

from agent import run_agent, _retry_delay
from bonus.tools import BonusTools
from test_agent import ScriptedProvider, call, final


class RetryPolicyTests(unittest.TestCase):
    def test_provider_coverage_feedback_allows_finishing_missing_queries(self):
        tools = BonusTools()
        provider = ScriptedProvider([
            call('1', 'get_github_status', {}), final('Chưa kiểm tra Vercel.'),
            call('2', 'get_vercel_status', {}), final('Đã có dữ liệu GitHub và Vercel.'),
        ])
        result = run_agent('Kiểm tra GitHub và Vercel.', provider, tools, max_steps=5)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(tools.calls[('get_vercel_status', 'vercel')], 1)
        notices = [t for t in result['trace'] if t['event'] == 'validation_missing_provider']
        self.assertEqual(notices[0]['providers'], ['vercel'])

    def test_provider_mention_does_not_force_excluded_tool(self):
        tools = BonusTools()
        provider = ScriptedProvider([final('Không kiểm tra Vercel theo yêu cầu.'),
                                     final('Không kiểm tra Vercel theo yêu cầu.')])
        result = run_agent('Không kiểm tra Vercel.', provider, tools, max_steps=3)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(tools.calls, {})

    def test_persistent_rate_limit_blocks_extra_model_requests(self):
        tools = BonusTools(scenario='rate_limit_always')
        provider = ScriptedProvider([
            call(str(i), 'get_vercel_status', {'component': str(i)}) for i in range(5)
        ] + [final('Chưa biết trạng thái Vercel vì rate limit.')])
        result = run_agent('Kiểm tra', provider, tools, max_steps=7)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(tools.calls[('get_vercel_status', 'vercel')], 3)
        blocked = [t for t in result['trace'] if t['event'] == 'tool_blocked']
        self.assertEqual(len(blocked), 2)
        self.assertEqual(blocked[0]['result']['error'], 'RETRY_LIMIT_REACHED')
        self.assertEqual(json.loads(provider.histories[4][-1]['content'])['error'],
                         'RETRY_LIMIT_REACHED')

    def test_retry_after_blocks_calls_before_deadline(self):
        tools = BonusTools()
        provider = ScriptedProvider([call('1', 'get_npm_status', {}),
                                     call('2', 'get_npm_status', {}), final('Chưa biết status.')])
        with patch.object(tools, 'execute', return_value={
            'ok': False, 'error': 'RATE_LIMITED', 'retry_after': '120'
        }) as execute, patch('agent.time.time', return_value=100):
            result = run_agent('Kiểm tra', provider, tools, max_steps=4)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(provider.histories[2][-1]['content'])['error'], 'RETRY_DEFERRED')

    def test_retry_after_accepts_http_date_and_rejects_nonfinite(self):
        self.assertEqual(_retry_delay({'retry_after': 'Thu, 01 Jan 1970 00:02:00 GMT'}, 100), 20)
        self.assertEqual(_retry_delay({'retry_after': 'invalid'}, 100), 1)
        self.assertEqual(_retry_delay({'retry_after': 'inf'}, 100), 1)
        self.assertEqual(_retry_delay({'retry_after_seconds': 0}, 100), 0)


if __name__ == '__main__':
    unittest.main()
