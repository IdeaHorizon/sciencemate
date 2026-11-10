import pytest
from nodes.literature.tools.search_engines import source_failure_message

@pytest.mark.parametrize('status', [500, 503])
def test_public_source_notice_omits_upstream_html_and_preserves_evidence(status):
    diagnostic = {'http_status': status, 'status': 'error', 'error': '<html>private upstream diagnostic</html>'}
    notice = source_failure_message('pubmed', diagnostic)
    assert f'HTTP {status}' in notice
    assert '不影响其他来源' in notice
    assert '<html>' not in notice
    assert diagnostic['error'].startswith('<html>')

def test_rate_limit_is_not_confused_with_server_failure():
    assert '限流' in source_failure_message('crossref', {'http_status': 429})
    assert '超时' in source_failure_message('pubmed', {'status': 'timeout'})
