# -*- coding: utf-8 -*-

# Copyright (c) 2014-2016 CoNWeT Lab., Universidad Politécnica de Madrid

# This file is part of Wirecloud.

# Wirecloud is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Wirecloud is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with Wirecloud.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import json
import six
from six.moves.urllib.parse import parse_qsl

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.urlresolvers import reverse
from mock import MagicMock, Mock, patch

from wirecloud.commons.utils.testcases import WirecloudTestCase
from wirecloud.proxy.views import proxy_request


# Avoid nose to repeat these tests (they are run through wirecloud/fiware/tests/__init__.py)
__test__ = False


TEST_TOKEN = 'yLCdDImTd6V5xegxyaQjBvC8ENRziFchYKXN0ur1y__uQ2ig3uIEaP6nJ0WxiRWGyCKquPQQmTIlhhYCMQWPXg'
TEST_WORKSPACE_TOKEN = 'rtHdDImTd6V5xegxyaQjBvC8ENRziFchYKXN0ur1y..uQ2ig3uIEaP6nJ0WxiRWGyCKquPQQmTIlhhYCMQWPx6'


class ProxyTestCase(WirecloudTestCase):

    fixtures = ('selenium_test_data', 'user_with_workspaces', 'fiware_proxy_test_data')
    tags = ('wirecloud-fiware-proxy', 'wirecloud-proxy', 'wirecloud-noselenium')

    @classmethod
    def setUpClass(cls):

        super(ProxyTestCase, cls).setUpClass()

        def echo_headers_response(method, url, *args, **kwargs):
            body = json.dumps(kwargs['headers'])
            return {
                'headers': {
                    'Content-Type': 'application/json',
                    'Content-Length': len(body),
                },
                'content': body,
            }

        cls.echo_headers_response = echo_headers_response

    def setUp(self):
        admin_tokens_mock = Mock()
        admin_tokens_mock.access_token = TEST_TOKEN
        self.admin_mock = Mock()
        self.admin_mock.social_auth.get.return_value = admin_tokens_mock

        user_with_workspaces_tokens_mock = Mock()
        user_with_workspaces_tokens_mock.access_token = TEST_WORKSPACE_TOKEN
        self.user_with_workspaces_mock = Mock()
        self.user_with_workspaces_mock.social_auth.get.return_value = user_with_workspaces_tokens_mock

        self.normuser_mock = Mock()
        self.normuser_mock.social_auth.get.side_effect = Exception

        self.patcher = patch('wirecloud.fiware.proxy.IDM_SUPPORT_ENABLED', new=True)
        self.patcher.start()
        super(ProxyTestCase, self).setUp()

    def tearDown(self):
        self.patcher.stop()
        super(ProxyTestCase, self).tearDown()

    def read_response(self, response):

        if getattr(response, 'streaming', False) is True:
            return b"".join(response.streaming_content).decode('utf-8')
        else:
            return response.content.decode('utf-8')

    def prepare_request_mock(self, data=None, referer='http://localhost/user_with_workspaces/Public Workspace', user=None, extra_headers={}, GET='', use_deprecated_code=False):

        request = Mock()
        request.get_host.return_value = 'localhost'
        GET_PARAMETERS = parse_qsl(GET)
        request.GET = MagicMock()
        request.GET.__len__.side_effect = lambda: len(GET_PARAMETERS)
        request.GET.__getitem__.side_effect = lambda key: GET_PARAMETERS[key]
        request.GET.urlencode.side_effect = lambda: GET
        request.COOKIES = {
            settings.SESSION_COOKIE_NAME: 'test',
        }
        request.META = {
            'HTTP_ACCEPT': 'application/json',
            'SERVER_PROTOCOL': 'http',
            'REMOTE_ADDR': '127.0.0.1',
            'HTTP_HOST': 'localhost',
            'HTTP_REFERER': referer,
        }
        if data is not None:
            request.method = 'POST'
            data = data.encode('utf-8')
            request.META['content_type'] = 'application/json'
            request.META['content_length'] = len(data)
            request.read.return_value = data
        else:
            request.method = 'GET'


        if use_deprecated_code:
            request.META['HTTP_X_FI_WARE_OAUTH_TOKEN'] = 'true'
            extra_headers = {self.deprecation_mapping[key]: value for key, value in six.iteritems(extra_headers)}
        else:
            request.META['HTTP_FIWARE_OAUTH_TOKEN'] = 'true'

        request.META.update(extra_headers)
        if user is None:
            request.user = self.admin_mock
        else:
            request.user = user

        return request

    deprecation_mapping = {
        "HTTP_FIWARE_OAUTH_BODY_PATTERN": "HTTP_X_FI_WARE_OAUTH_TOKEN_BODY_PATTERN",
        "HTTP_FIWARE_OAUTH_GET_PARAMETER": "HTTP_X_FI_WARE_OAUTH_GET_PARAMETER",
        "HTTP_FIWARE_OAUTH_HEADER_NAME": "HTTP_X_FI_WARE_OAUTH_HEADER_NAME",
        "HTTP_FIWARE_OAUTH_SOURCE": "HTTP_X_FI_WARE_OAUTH_SOURCE",
    }

    def invalid_request_validator(self):
        def validator(response):
            self.assertEqual(response.status_code, 422)
            json.loads(self.read_response(response))

        return validator

    def check_proxy_request(self, validator=lambda response: True, path='/path', **kwargs):
        request = self.prepare_request_mock(use_deprecated_code=False, **kwargs)
        response = proxy_request(request=request, protocol='http', domain='example.com', path=path)
        validator(response)

        request = self.prepare_request_mock(use_deprecated_code=True, **kwargs)
        response = proxy_request(request=request, protocol='http', domain='example.com', path=path)
        validator(response)

    def test_fiware_idm_processor_header(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            headers = json.loads(self.read_response(response))
            self.assertIn('X-Auth-Token', headers)
            self.assertEqual(headers['X-Auth-Token'], TEST_TOKEN)

        self.check_proxy_request(validator=validator, data='{}', extra_headers={
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
        })


    def test_fiware_idm_processor_header_authorization(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            headers = json.loads(self.read_response(response))
            self.assertIn('Authorization', headers)
            self.assertEqual(headers['Authorization'], 'Bearer ' + TEST_TOKEN)

        self.check_proxy_request(validator=validator, data='{}', extra_headers={
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'Authorization',
        })

    def test_fiware_idm_processor_body(self):

        def echo_response(method, url, *args, **kwargs):
            self.assertEqual(int(kwargs['headers']['content-length']), 99) # Content Length after token injection
            return {'content': kwargs['data'].read()}

        self.network._servers['http']['example.com'].add_response('POST', '/path', echo_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            data = json.loads(self.read_response(response))
            self.assertEqual(data['token'], TEST_TOKEN)

        self.check_proxy_request(validator=validator, data='{"token": "%token%"}', extra_headers={
            'HTTP_FIWARE_OAUTH_BODY_PATTERN': '%token%',
        })

    def test_fiware_idm_processor_get_parameter(self):

        def echo_response(method, url, *args, **kwargs):
            return {'content': url}

        self.network._servers['http']['example.com'].add_response('GET', '/path', echo_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            new_url = self.read_response(response)
            self.assertEqual(new_url, 'http://example.com/path?test=a&access_token_id=' + TEST_TOKEN)

        self.check_proxy_request(validator=validator, extra_headers={
            'HTTP_FIWARE_OAUTH_GET_PARAMETER': 'access_token_id',
        }, GET='test=a')

    def test_fiware_idm_processor_get_parameter_post(self):

        def echo_response(method, url, *args, **kwargs):
            return {'content': url}

        self.network._servers['http']['example.com'].add_response('POST', '/path', echo_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            new_url = self.read_response(response)
            self.assertEqual(new_url, 'http://example.com/path?test=a&access_token_id=' + TEST_TOKEN)

        self.check_proxy_request(validator=validator, data="body", extra_headers={
            'HTTP_FIWARE_OAUTH_GET_PARAMETER': 'access_token_id',
        }, GET='test=a')

    def test_fiware_idm_processor_get_parameter_emtpy_query(self):

        def echo_response(method, url, *args, **kwargs):
            return {'content': url}

        self.network._servers['http']['example.com'].add_response('GET', '/path', echo_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            new_url = self.read_response(response)
            self.assertEqual(new_url, 'http://example.com/path?access_token_id=' + TEST_TOKEN)

        self.check_proxy_request(validator=validator, extra_headers={
            'HTTP_FIWARE_OAUTH_GET_PARAMETER': 'access_token_id',
        })

    def test_fiware_idm_anonymous_user(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        self.check_proxy_request(validator=self.invalid_request_validator(), extra_headers={
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
        }, user=AnonymousUser())

    def test_fiware_idm_processor_requires_valid_referer(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)
        proxied_url = reverse('wirecloud|proxy', kwargs={'protocol': 'http', 'domain': 'example.com', 'path': '/path'})

        self.check_proxy_request(validator=self.invalid_request_validator(), data='{}', extra_headers={
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
            }, referer='http://localhost' + proxied_url, user=self.admin_mock)

    def test_fiware_idm_no_token_available(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        self.check_proxy_request(validator=self.invalid_request_validator(), data='{}', extra_headers={
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
            }, user=self.normuser_mock)

    def test_fiware_idm_token_from_workspace_owner_header(self):

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        def validator(response):
            self.assertEqual(response.status_code, 200)
            request_headers = json.loads(self.read_response(response))
            self.assertIn('X-Auth-Token', request_headers)
            self.assertEqual(request_headers['X-Auth-Token'], TEST_WORKSPACE_TOKEN)

        with patch('wirecloud.proxy.views.Workspace') as Workspace_orm_mock:
            Workspace_orm_mock.objects.get().creator = self.user_with_workspaces_mock
            self.check_proxy_request(validator=validator, data='{}', extra_headers={
                "HTTP_FIWARE_OAUTH_SOURCE": 'workspaceowner',
                "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
            }, user=self.normuser_mock)

    def test_fiware_idm_token_from_workspace_owner_no_token(self):

        # Remove user_with_workspaces access_token
        self.user_with_workspaces_mock.social_auth.get(provider='fiware').access_token = None

        self.network._servers['http']['example.com'].add_response('POST', '/path', self.echo_headers_response)

        with patch('wirecloud.proxy.views.Workspace') as Workspace_orm_mock:
            Workspace_orm_mock.objects.get().creator = self.user_with_workspaces_mock
            self.check_proxy_request(validator=self.invalid_request_validator(), data='{}', extra_headers={
                "HTTP_FIWARE_OAUTH_SOURCE": 'workspaceowner',
                "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
            }, user=self.normuser_mock)

    def test_fiware_idm_token_invalid_source(self):

        self.check_proxy_request(validator=self.invalid_request_validator(), data='{}', extra_headers={
            "HTTP_FIWARE_OAUTH_SOURCE": 'invalidsource',
            "HTTP_FIWARE_OAUTH_HEADER_NAME": 'X-Auth-Token',
        }, user=self.normuser_mock)
