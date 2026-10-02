from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from cvbankas_tracker.sources.career_budget import CareerBudgetExceeded, RunBudget
from cvbankas_tracker.sources.careers import (
    _AllowedHostRedirectHandler,
    _PublicRedirectHandler,
    _read_response,
)


class CareerNetworkLimitsTests(unittest.TestCase):
    def test_redirects_count_requests_and_never_drain_response_bodies(self):
        for handler_type in (_AllowedHostRedirectHandler, _PublicRedirectHandler):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(handler=handler_type, status=status):
                    budget = RunBudget(max_requests=2)
                    budget.before_request()
                    handler = handler_type(budget)
                    handler.parent = Mock()
                    request = Request('https://api.ashbyhq.com/old')
                    request.timeout = 20
                    response = Mock()
                    response.read.side_effect = AssertionError('Redirect body must not be drained')
                    with patch('cvbankas_tracker.sources.careers._validate_public_url'):
                        getattr(handler, f'http_error_{status}')(request, response, status, 'Moved', {'location': '/new'})
                    self.assertEqual(budget.request_count, 2)
                    self.assertEqual(budget.total_bytes, 0)
                    response.read.assert_not_called()
                    response.close.assert_called_once()
                    redirected = handler.parent.open.call_args[0][0]
                    self.assertEqual(redirected.full_url, 'https://api.ashbyhq.com/new')

    def test_redirect_cannot_open_after_request_budget_is_exhausted(self):
        budget = RunBudget(max_requests=1)
        budget.before_request()
        handler = _AllowedHostRedirectHandler(budget)
        handler.parent = Mock()
        request = Request('https://api.ashbyhq.com/old')
        request.timeout = 20
        response = Mock()
        with self.assertRaises(CareerBudgetExceeded):
            handler.http_error_302(request, response, 302, 'Moved', {'location': '/new'})
        response.read.assert_not_called()
        response.close.assert_called_once()
        handler.parent.open.assert_not_called()

    def test_redirect_loop_is_bounded(self):
        handler = _AllowedHostRedirectHandler(RunBudget())
        request = Request('https://api.ashbyhq.com/old')
        request.timeout = 20
        request.redirect_dict = {'https://api.ashbyhq.com/new': handler.max_repeats}
        handler.parent = Mock()
        response = Mock()
        with self.assertRaises(HTTPError):
            handler.http_error_302(request, response, 302, 'Moved', {'location': '/new'})
        response.close.assert_called()
        handler.parent.open.assert_not_called()

    def test_redirect_preserves_private_host_and_method_boundaries(self):
        for url in ('http://127.0.0.1/private', 'https://evil.example/secret', 'file:///etc/passwd'):
            handler = _AllowedHostRedirectHandler(RunBudget())
            handler.parent = Mock()
            response = Mock()
            with self.subTest(url=url), self.assertRaises(HTTPError):
                handler.http_error_302(Request('https://api.ashbyhq.com/old'), response, 302, 'Moved', {'location': url})
            response.close.assert_called()
            handler.parent.open.assert_not_called()
        handler = _AllowedHostRedirectHandler(RunBudget())
        with self.assertRaises(HTTPError):
            handler.http_error_307(Request('https://api.ashbyhq.com/old', data=b'{}'), Mock(), 307, 'Moved', {'location': '/new'})

    def test_response_stream_checks_deadline_between_chunks(self):
        clock = [0.0]
        budget = RunBudget(max_seconds=1, clock=lambda: clock[0])
        budget.start()
        response = Mock()
        def read_chunk(size):
            clock[0] += 2
            return b'data'
        response.read1.side_effect = read_chunk
        with self.assertRaises(CareerBudgetExceeded):
            _read_response(response, budget, 100)
        self.assertEqual(response.read1.call_count, 1)

    def test_response_stream_caps_individual_and_total_bytes(self):
        for budget, limit, error in ((RunBudget(), 3, ValueError), (RunBudget(max_total_bytes=3), 100, CareerBudgetExceeded)):
            response = Mock()
            response.read1.return_value = b'abcd'
            with self.subTest(error=error), self.assertRaises(error):
                _read_response(response, budget, limit)

    def test_exact_byte_limit_and_empty_response_are_successful(self):
        response = Mock()
        response.read1.side_effect = [b'ab', b'c', b'']
        budget = RunBudget(max_total_bytes=3)
        self.assertEqual(_read_response(response, budget, 3), b'abc')
        self.assertEqual(budget.total_bytes, 3)
        self.assertEqual(response.read1.call_args[0][0], 1)
        response.read1.side_effect = [b'']
        self.assertEqual(_read_response(response, RunBudget(), 3), b'')
