# -*- coding: utf-8 -*-

# Copyright (c) 2012-2016 CoNWeT Lab., Universidad Politécnica de Madrid

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

import gettext as gettext_module
import importlib
import json
import os
from six.moves.urllib.parse import urlparse, urlunparse, parse_qs

from django.conf import settings
from django.contrib.auth.views import redirect_to_login as django_redirect_to_login
from django.core import urlresolvers
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.template import RequestContext, TemplateDoesNotExist
from django.template.loader import get_template
from django.utils._os import upath
from django.utils.encoding import force_text
from django.utils.functional import Promise
from django.utils.http import urlencode
from django.utils.translation import check_for_language, get_language, to_locale
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_safe
from django.views.i18n import render_javascript_catalog
from user_agents import parse as ua_parse
import six

from wirecloud.commons.utils.cache import patch_cache_headers
from wirecloud.commons.utils.http import build_error_response
from wirecloud.platform.core.plugins import get_version_hash
from wirecloud.platform.plugins import get_active_features_info, get_plugins
from wirecloud.platform.models import Workspace
from wirecloud.platform.themes import get_active_theme_name, get_available_themes
from wirecloud.platform.workspace.utils import get_workspace_list

LANGUAGE_QUERY_PARAMETER = 'language'


@cache_page(60 * 60 * 24, key_prefix='wirecloud-features-%s' % get_version_hash())
@require_safe
def feature_collection(request):
    features = get_active_features_info()

    response = HttpResponse(json.dumps(features, sort_keys=True), content_type='application/json; charset=UTF-8')
    return patch_cache_headers(response)


def resolve_url(to, *args, **kwargs):  # pragma: no cover
    """
    Return a URL appropriate for the arguments passed.
    The arguments could be:
        * A model: the model's `get_absolute_url()` function will be called.
        * A view name, possibly with arguments: `urlresolvers.reverse()` will
          be used to reverse-resolve the name.
        * A URL, which will be returned as-is.

    > Copied from django for workaround versions of django not including this patch:
    > https://code.djangoproject.com/ticket/24097
    """
    # If it's a model, use get_absolute_url()
    if hasattr(to, 'get_absolute_url'):
        return to.get_absolute_url()

    if isinstance(to, Promise):
        # Expand the lazy instance, as it can cause issues when it is passed
        # further to some Python functions like urlparse.
        to = force_text(to)

    if isinstance(to, six.string_types):
        # Handle relative URLs
        if to.startswith(('./', '../')):
            return to

    # Next try a reverse URL resolution.
    try:
        return urlresolvers.reverse(to, args=args, kwargs=kwargs)
    except urlresolvers.NoReverseMatch:
        # If this is a callable, re-raise.
        if callable(to):
            raise
        # If this doesn't "feel" like a URL, re-raise.
        if '/' not in to and '.' not in to:
            raise

    # Finally, fall back and assume it's a URL
    return to


def redirect_to_login(*args, **kwargs):
    kwargs['login_url'] = resolve_url(kwargs.get('login_url') or settings.LOGIN_URL)
    return django_redirect_to_login(*args, **kwargs)


def get_javascript_catalog(locale, domain, packages):
    default_locale = to_locale(settings.LANGUAGE_CODE)
    t = {}
    paths = []
    en_selected = locale.startswith('en')
    en_catalog_missing = True
    # paths of requested packages
    for package in packages:
        p = importlib.import_module(package)
        path = os.path.join(os.path.dirname(upath(p.__file__)), 'locale')
        paths.append(path)
    # add the filesystem paths listed in the LOCALE_PATHS setting
    paths.extend(reversed(settings.LOCALE_PATHS))
    # first load all english languages files for defaults
    for path in paths:
        try:
            catalog = gettext_module.translation(domain, path, ['en'])
            t.update(catalog._catalog)
        except IOError:
            pass
        else:
            # 'en' is the selected language and at least one of the packages
            # listed in `packages` has an 'en' catalog
            if en_selected:
                en_catalog_missing = False
    # next load the settings.LANGUAGE_CODE translations if it isn't english
    if default_locale != 'en':
        for path in paths:
            try:
                catalog = gettext_module.translation(domain, path, [default_locale])
            except IOError:
                catalog = None
            if catalog is not None:
                t.update(catalog._catalog)
    # last load the currently selected language, if it isn't identical to the default.
    if locale != default_locale:
        # If the currently selected language is English but it doesn't have a
        # translation catalog (presumably due to being the language translated
        # from) then a wrong language catalog might have been loaded in the
        # previous step. It needs to be discarded.
        if en_selected and en_catalog_missing:
            t = {}
        else:
            locale_t = {}
            for path in paths:
                try:
                    catalog = gettext_module.translation(domain, path, [locale])
                except IOError:
                    catalog = None
                if catalog is not None:
                    locale_t.update(catalog._catalog)
            if locale_t:
                t = locale_t
    plural = None
    if '' in t:
        for l in t[''].split('\n'):
            if l.startswith('Plural-Forms:'):
                plural = l.split(':', 1)[1].strip()
    if plural is not None:
        # this should actually be a compiled function of a typical plural-form:
        # Plural-Forms: nplurals=3; plural=n%10==1 && n%100!=11 ? 0 :
        #               n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2;
        plural = [el.strip() for el in plural.split(';') if el.strip().startswith('plural=')][0].split('=', 1)[1]

    pdict = {}
    maxcnts = {}
    catalog = {}
    for k, v in t.items():
        if k == '':
            continue
        if isinstance(k, six.string_types):
            catalog[k] = v
        elif isinstance(k, tuple):
            msgid = k[0]
            cnt = k[1]
            maxcnts[msgid] = max(cnt, maxcnts.get(msgid, 0))
            pdict.setdefault(msgid, {})[cnt] = v
        else:
            raise TypeError(k)
    for k, v in pdict.items():
        catalog[k] = [v.get(i, '') for i in range(maxcnts[msgid] + 1)]

    return catalog, plural


@cache_page(60 * 60 * 24, key_prefix='js18n-%s' % get_version_hash())
def cached_javascript_catalog(request):

    language = request.GET.get(LANGUAGE_QUERY_PARAMETER)
    if not (language and check_for_language(language)):
        language = get_language()
    locale = to_locale(language)

    packages = ['wirecloud.commons', 'wirecloud.catalogue', 'wirecloud.platform']

    for plugin in get_plugins():
        packages.append(plugin.__module__.rsplit('.', 1)[0])

    for theme in get_available_themes():
        packages.append(theme)

    catalog, plural = get_javascript_catalog(locale, 'djangojs', packages)
    return render_javascript_catalog(catalog, plural)


def render_root_page(request):
    return auto_select_workspace(request, request.GET.get('mode', None))


def auto_select_workspace(request, mode=None):

    if settings.ALLOW_ANONYMOUS_ACCESS is False and request.user.is_authenticated() is False:
        return redirect_to_login(request.get_full_path())

    _junk1, active_workspace = get_workspace_list(request.user)

    if active_workspace is not None:
        url = urlresolvers.reverse('wirecloud.workspace_view', kwargs={
            'owner': active_workspace.workspace.creator.username,
            'name': active_workspace.workspace.name,
        })

        if mode:
            url += '?' + urlencode({'mode': mode})

        return HttpResponseRedirect(url)
    elif request.user.is_authenticated():
        return render_wirecloud(request, mode)
    else:
        context = {
            'THEME': get_active_theme_name(),
            'VIEW_MODE': 'classic',
            'WIRECLOUD_VERSION_HASH': get_version_hash()
        }
        context = RequestContext(request, context)
        return render(request, 'wirecloud/landing_page.html', context_instance=context, content_type="application/xhtml+xml; charset=UTF-8")


def render_workspace_view(request, owner, name):

    if settings.ALLOW_ANONYMOUS_ACCESS is False and request.user.is_authenticated() is False:
        return redirect_to_login(request.get_full_path())

    get_workspace_list(request.user)

    workspace = get_object_or_404(Workspace, creator__username=owner, name=name)
    if not workspace.public and request.user not in workspace.users.all():
        if request.user.is_authenticated():
            return build_error_response(request, 403, 'forbidden')
        else:
            return redirect_to_login(request.get_full_path())
    elif not request.user.is_authenticated():
        # Ensure user has a session
        request.session[settings.LANGUAGE_COOKIE_NAME] = request.session.get(settings.LANGUAGE_COOKIE_NAME, None)

    return render_wirecloud(request)


def get_default_view(request):

    if 'default_mode' not in request.session:
        user_agent = ua_parse(request.META.get('HTTP_USER_AGENT', ''))
        if user_agent.is_mobile:
            mode = 'smartphone'
        else:
            mode = 'classic'

        request.session['default_mode'] = mode

    return request.session['default_mode']


def remove_query_parameter(request, parameter):
    url = urlparse(request.build_absolute_uri())
    query_params = parse_qs(url.query, True)
    del query_params[parameter]
    return HttpResponseRedirect(urlunparse((
        url.scheme,
        url.netloc,
        url.path,
        url.params,
        urlencode(query_params, True),
        url.fragment
    )))


def render_wirecloud(request, view_type=None):

    if view_type is None:
        if 'mode' in request.GET:
            view_type = request.GET['mode']
        else:
            view_type = get_default_view(request)

    theme = request.GET.get('theme', get_active_theme_name())
    if theme not in get_available_themes():
        return remove_query_parameter(request, 'theme')

    try:

        template = get_template(theme + ':wirecloud/views/%s.html' % view_type)

    except TemplateDoesNotExist:

        if 'mode' in request.GET:
            return remove_query_parameter(request, 'mode')
        else:
            view_type = get_default_view(request)
            return render_wirecloud(request, view_type)

    context = {
        'THEME': theme,
        'VIEW_MODE': view_type,
        'WIRECLOUD_VERSION_HASH': get_version_hash()
    }
    content = template.render(RequestContext(request, context))
    return HttpResponse(content, content_type="application/xhtml+xml; charset=UTF-8")
