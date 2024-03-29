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

from __future__ import unicode_literals

from io import BytesIO
import json
import os
import zipfile

from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext as _
from six import string_types

from wirecloud.catalogue import utils as catalogue
from wirecloud.catalogue.models import CatalogueResource
from wirecloud.commons.baseviews import Resource, Service
from wirecloud.commons.utils.db import save_alternative
from wirecloud.commons.utils.http import authentication_required, authentication_required_cond, build_error_response, get_content_type, normalize_boolean_param, consumes, parse_json_request, produces
from wirecloud.commons.utils.template import is_valid_name, is_valid_vendor, is_valid_version, TemplateParser
from wirecloud.commons.utils.transaction import commit_on_http_success
from wirecloud.commons.utils.wgt import WgtFile
from wirecloud.platform.models import IWidget, Tab, UserWorkspace, Workspace
from wirecloud.platform.preferences.views import update_workspace_preferences
from wirecloud.platform.settings import ALLOW_ANONYMOUS_ACCESS
from wirecloud.platform.wiring.utils import get_wiring_skeleton
from wirecloud.platform.workspace.mashupTemplateGenerator import build_json_template_from_workspace, build_xml_template_from_workspace
from wirecloud.platform.workspace.mashupTemplateParser import check_mashup_dependencies, buildWorkspaceFromTemplate, fillWorkspaceUsingTemplate, MissingDependencies
from wirecloud.platform.workspace.utils import deleteTab, createTab, get_workspace_list, get_workspace_data, get_global_workspace_data, setVisibleTab
from wirecloud.platform.markets.utils import get_local_catalogue


def createEmptyWorkspace(workspaceName, user, allow_renaming=False):
    active = False
    workspaces = UserWorkspace.objects.filter(user__id=user.id, active=True)
    if workspaces.count() == 0:
        # there isn't yet an active workspace
        active = True

    workspace = Workspace(name=workspaceName, creator=user, wiringStatus=get_wiring_skeleton())
    if allow_renaming is True:
        save_alternative(Workspace, 'name', workspace)
    else:
        workspace.save()
    UserWorkspace.objects.create(user=user, workspace=workspace, active=active)

    # Tab creation
    createTab(_('Tab'), workspace)

    return workspace


def setActiveWorkspace(user, workspace):

    UserWorkspace.objects.filter(user=user, active=True).exclude(workspace=workspace).update(active=False)
    UserWorkspace.objects.filter(user=user, workspace=workspace).update(active=True)


class WorkspaceCollection(Resource):

    @produces(('application/json',))
    @commit_on_http_success
    def read(self, request):

        workspaces, _junk = get_workspace_list(request.user)
        data_list = [get_workspace_data(workspace, request.user) for workspace in workspaces]

        return HttpResponse(json.dumps(data_list, sort_keys=True), content_type='application/json; charset=UTF-8')

    @authentication_required
    @consumes(('application/json',))
    @produces(('application/json',))
    @commit_on_http_success
    def create(self, request):

        data = parse_json_request(request)

        workspace_name = data.get('name', '').strip()
        workspace_id = data.get('workspace', '')
        mashup_id = data.get('mashup', '')
        initial_pref_values = data.get('preferences', {})
        allow_renaming = normalize_boolean_param(request, 'allow_renaming', data.get('allow_renaming', False))
        dry_run = normalize_boolean_param(request, 'dry_run', data.get('dry_run', False))

        if mashup_id == '' and workspace_id == '' and workspace_name == '':
            return build_error_response(request, 422, _('Missing name parameter'))
        elif  mashup_id != '' and workspace_id != '':
            return build_error_response(request, 422, _('Workspace and mashup parameters cannot be used at the same time'))

        if mashup_id == '' and workspace_id == '':

            if not is_valid_name(workspace_name):
                return build_error_response(request, 422, _('invalid workspace name'))

            if dry_run:
                return HttpResponse(status=204)

            try:
                workspace = createEmptyWorkspace(workspace_name, request.user, allow_renaming=allow_renaming)
            except IntegrityError:
                msg = _('A workspace with the given name already exists')
                return build_error_response(request, 409, msg)
        else:

            if mashup_id != '':
                values = mashup_id.split('/', 3)
                if len(values) != 3:
                    return build_error_response(request, 422, _('invalid mashup id'))

                (mashup_vendor, mashup_name, mashup_version) = values
                try:
                    resource = CatalogueResource.objects.get(vendor=mashup_vendor, short_name=mashup_name, version=mashup_version)
                    if not resource.is_available_for(request.user) or resource.resource_type() != 'mashup':
                        raise CatalogueResource.DoesNotExist
                except CatalogueResource.DoesNotExist:
                    return build_error_response(request, 422, _('Mashup not found: %(mashup_id)s') % {'mashup_id': mashup_id})

                base_dir = catalogue.wgt_deployer.get_base_dir(mashup_vendor, mashup_name, mashup_version)
                wgt_file = WgtFile(os.path.join(base_dir, resource.template_uri))
                template = TemplateParser(wgt_file.get_template())

            else:

                from_ws = get_object_or_404(Workspace, id=workspace_id)
                if from_ws.public is False and not request.user.is_superuser and from_ws.creator != request.user:
                    return build_error_response(request, 403, _('You are not allowed to read from workspace %s') % workspace_id)

                options = {
                    'vendor': 'api',
                    'name': from_ws.name,
                    'version': '1.0',
                    'title': '',
                    'description': 'Temporal mashup for the workspace copy operation',
                    'email': 'a@example.com',
                }

                template = TemplateParser(build_json_template_from_workspace(options, from_ws, from_ws.creator))

            try:
                check_mashup_dependencies(template, request.user)
            except MissingDependencies as e:
                details = {
                    'missingDependencies': e.missing_dependencies,
                }
                return build_error_response(request, 422, e, details=details)

            if dry_run:
                return HttpResponse(status=204)

            if workspace_name == '':
                workspace_name = None

            try:
                workspace, _junk = buildWorkspaceFromTemplate(template, request.user, allow_renaming=allow_renaming, new_name=workspace_name)
            except IntegrityError:
                msg = _('A workspace with the given name already exists')
                return build_error_response(request, 409, msg)

        if len(initial_pref_values) > 0:
            update_workspace_preferences(workspace, initial_pref_values, invalidate_cache=False)

        workspace_data = get_global_workspace_data(workspace, request.user)

        return workspace_data.get_response(status_code=201, cacheable=False)


class WorkspaceEntry(Resource):

    @authentication_required_cond(ALLOW_ANONYMOUS_ACCESS)
    @produces(('application/json',))
    def read(self, request, workspace_id):

        workspace = get_object_or_404(Workspace, pk=workspace_id)

        if not workspace.is_available_for(request.user):
            return build_error_response(request, 403, _("You don't have permission to access this workspace"))

        workspace_data = get_global_workspace_data(workspace, request.user)

        return workspace_data.get_response()

    @authentication_required
    @consumes(('application/json',))
    @commit_on_http_success
    def create(self, request, workspace_id):

        ts = parse_json_request(request)

        workspace = get_object_or_404(Workspace, pk=workspace_id)
        if not (request.user.is_superuser or workspace.users.filter(pk=request.user.pk).exists()):
            return build_error_response(request, 403, _('You are not allowed to update this workspace'))

        if 'active' in ts:

            active = ts.get('active', False)
            if isinstance(active, string_types):
                active = ts['active'].lower() == 'true'

            if active:
                # Only one active workspace
                setActiveWorkspace(request.user, workspace)
            else:
                currentUserWorkspace = UserWorkspace.objects.get(workspace=workspace, user=request.user)
                currentUserWorkspace.active = False
                currentUserWorkspace.save()

        if 'name' in ts:
            workspace.name = ts['name']

        workspace.save()

        return HttpResponse(status=204)

    @authentication_required
    @commit_on_http_success
    def delete(self, request, workspace_id):

        workspace = get_object_or_404(Workspace, pk=workspace_id)
        if not workspace.users.filter(pk=request.user.pk).exists():
            return build_error_response(request, 403, _('You are not allowed to delete this workspace'))

        UserWorkspace.objects.filter(user=request.user, workspace=workspace).delete()
        if workspace.users.count() == 0:

            # Remove the workspace
            iwidgets = IWidget.objects.filter(tab__workspace=workspace)
            for iwidget in iwidgets:
                iwidget.delete()
            workspace.delete()

        return HttpResponse(status=204)


class TabCollection(Resource):

    @authentication_required
    @consumes(('application/json',))
    @produces(('application/json',))
    @commit_on_http_success
    def create(self, request, workspace_id):

        workspace = get_object_or_404(Workspace, pk=workspace_id)
        data = parse_json_request(request)

        if 'name' not in data:
            return build_error_response(request, 400, _('Malformed tab JSON: expecting tab name.'))

        tab_name = data['name']
        if not (request.user.is_superuser or workspace.creator == request.user):
            return build_error_response(request, 403, _('You are not allowed to create new tabs for this workspace'))

        try:
            tab = createTab(tab_name, workspace)
        except IntegrityError:
            msg = _('A tab with the given name already exists for the workspace')
            return build_error_response(request, 409, msg)

        # Returning created Ids
        ids = {'id': tab.id, 'name': tab.name}

        return HttpResponse(json.dumps(ids), status=201, content_type='application/json; charset=UTF-8')


class TabOrderService(Service):

    @authentication_required
    @consumes(('application/json',))
    @commit_on_http_success
    def process(self, request, workspace_id):

        workspace = get_object_or_404(Workspace, pk=workspace_id)
        if not (request.user.is_superuser or workspace.creator == request.user):
            return build_error_response(request, 403, _('You are not allowed to create new tabs for this workspace'))

        order = parse_json_request(request)

        tabs = Tab.objects.filter(id__in=order)

        for tab in tabs:
            tab.position = order.index(tab.id)
            tab.save()

        return HttpResponse(status=204)


class TabEntry(Resource):

    @authentication_required
    @consumes(('application/json',))
    @commit_on_http_success
    def update(self, request, workspace_id, tab_id):

        tab = get_object_or_404(Tab.objects.select_related('workspace'), workspace__pk=workspace_id, pk=tab_id)
        if tab.workspace.creator != request.user:
            return build_error_response(request, 403, _('You are not allowed to update this workspace'))

        user_workspace = UserWorkspace.objects.get(user__id=request.user.id, workspace__id=workspace_id)
        if user_workspace.manager != '':
            return build_error_response(request, 403, _('You are not allowed to update this workspace'))

        data = parse_json_request(request)

        if 'visible' in data:
            visible = data['visible']
            if isinstance(visible, string_types):
                visible = visible.strip().lower()
                if visible not in ('true', 'false'):
                    return build_error_response(request, 422, _('Invalid visible value'))
                visible = visible == 'true'
            elif not isinstance(visible, bool):
                return build_error_response(request, 422, _('Invalid visible value'))

            if visible:
                #Only one visible tab
                setVisibleTab(request.user, workspace_id, tab)
            else:
                tab.visible = False

        if 'name' in data:
            tab.name = data['name']

        tab.save()

        return HttpResponse(status=204)

    @authentication_required
    @commit_on_http_success
    def delete(self, request, workspace_id, tab_id):

        # Get tab, if it does not exist, an http 404 error is returned
        tab = get_object_or_404(Tab.objects.select_related('workspace'), workspace__pk=workspace_id, pk=tab_id)
        if not request.user.is_superuser and not tab.workspace.users.filter(id=request.user.id).exists():
            return build_error_response(request, 403, _('You are not allowed to remove this tab'))

        tabs = Tab.objects.filter(workspace__pk=workspace_id).order_by('position')[::1]
        if len(tabs) == 1:
            msg = _("Tab cannot be deleted as workspaces need at least one tab")
            return build_error_response(request, 403, msg)

        if tab.iwidget_set.filter(readOnly=True).exists():
            msg = _("Tab cannot be deleted as it contains widgets that cannot be deleted")
            return build_error_response(request, 403, msg)

        # decrease the position of the following tabs
        for t in range(tab.position + 1, len(tabs)):
            tabs[t].position = tabs[t].position - 1
            tabs[t].save()

        # Remove the tab
        tabs.remove(tab)
        deleteTab(tab, request.user)

        if tab.visible:
            # set a new visible tab (first tab by default)
            activeTab = tabs[0]
            setVisibleTab(request.user, workspace_id, activeTab)

        return HttpResponse(status=204)


class MashupMergeService(Service):

    @authentication_required
    @consumes(('application/json',))
    @commit_on_http_success
    def process(self, request, to_ws_id):

        to_ws = get_object_or_404(Workspace, id=to_ws_id)
        if not request.user.is_superuser and to_ws.creator != request.user:
            return build_error_response(request, 403, _('You are not allowed to update this workspace'))

        data = parse_json_request(request)

        mashup_id = data.get('mashup', '')
        workspace_id = data.get('workspace', '')

        if mashup_id == '' and workspace_id == '':
            return build_error_response(request, 422, _('Missing workspace or mashup parameter'))
        elif  mashup_id != '' and workspace_id != '':
            return build_error_response(request, 422, _('Workspace and mashup parameters cannot be used at the same time'))

        if mashup_id != '':
            values = mashup_id.split('/', 3)
            if len(values) != 3:
                return build_error_response(request, 422, _('invalid mashup id'))

            (mashup_vendor, mashup_name, mashup_version) = values
            try:
                resource = CatalogueResource.objects.get(vendor=mashup_vendor, short_name=mashup_name, version=mashup_version)
                if not resource.is_available_for(request.user) or resource.resource_type() != 'mashup':
                    raise CatalogueResource.DoesNotExist
            except CatalogueResource.DoesNotExist:
                return build_error_response(request, 422, _('Mashup not found: %(mashup_id)s') % {'mashup_id': mashup_id})

            base_dir = catalogue.wgt_deployer.get_base_dir(mashup_vendor, mashup_name, mashup_version)
            wgt_file = WgtFile(os.path.join(base_dir, resource.template_uri))
            template = TemplateParser(wgt_file.get_template())

        else:

            from_ws = get_object_or_404(Workspace, id=workspace_id)
            if not request.user.is_superuser and from_ws.creator != request.user:
                return build_error_response(request, 403, _('You are not allowed to read from workspace %s') % workspace_id)

            options = {
                'vendor': 'api',
                'name': 'merge_op',
                'version': '1.0',
                'title': '',
                'description': 'Temporal mashup for merging operation',
                'email': 'a@example.com',
            }

            template = TemplateParser(build_json_template_from_workspace(options, from_ws, from_ws.creator))

        try:
            check_mashup_dependencies(template, request.user)
        except MissingDependencies as e:
            details = {
                'missingDependencies': e.missing_dependencies,
            }
            return build_error_response(request, 422, e, details=details)

        fillWorkspaceUsingTemplate(to_ws, template)
        return HttpResponse(status=204)


def check_json_fields(json, fields):
    missing_fields = []

    for field in fields:
        if not field in json:
            missing_fields.append(field)

    return missing_fields


class WorkspacePublisherEntry(Resource):

    @authentication_required
    @consumes(('application/json', 'multipart/form-data'))
    @commit_on_http_success
    def create(self, request, workspace_id):

        import wirecloud.catalogue.utils as catalogue_utils

        content_type = get_content_type(request)[0]
        image_file = None
        smartphoneimage_file = None
        extra_files = []

        if content_type == 'application/json':
            received_json = request.body.decode('utf-8')
        else:
            received_json = request.POST['json']
            image_file = request.FILES.get('image', None)
            smartphoneimage_file = request.FILES.get('smartphoneimage', None)

        try:
            options = json.loads(received_json)
        except ValueError as e:
            msg = _("malformed json data: %s") % e
            return build_error_response(request, 400, msg)

        missing_fields = check_json_fields(options, ('name', 'vendor', 'version'))
        if len(missing_fields) > 0:
            return build_error_response(request, 400, _('Malformed JSON. The following field(s) are missing: %(fields)s.') % {'fields': missing_fields})

        if not is_valid_vendor(options['vendor']):
            return build_error_response(request, 400, _('Invalid vendor'))

        if not is_valid_name(options['name']):
            return build_error_response(request, 400, _('Invalid name'))

        if not is_valid_version(options['version']):
            return build_error_response(request, 400, _('Invalid version number'))

        workspace = get_object_or_404(Workspace, id=workspace_id)
        if image_file is not None:
            options['image'] = 'images/catalogue' + os.path.splitext(image_file.name)[1]
            extra_files.append((options['image'], image_file))
        if smartphoneimage_file is not None:
            options['smartphoneimage'] = 'images/smartphone' + os.path.splitext(smartphoneimage_file.name)[1]
            extra_files.append((options['smartphoneimage'], smartphoneimage_file))
        if 'longdescription' in options:
            extra_files.append(('DESCRIPTION.md', BytesIO(options['longdescription'].encode('utf-8'))))
            options['longdescription'] = 'DESCRIPTION.md'

        description = build_xml_template_from_workspace(options, workspace, request.user)

        # Build mashup wgt file
        f = BytesIO()
        zf = zipfile.ZipFile(f, 'w')
        zf.writestr('config.xml', description.encode('utf-8'))
        for filename, extra_file in extra_files:
            zf.writestr(filename, extra_file.read())
        for resource_info in options['embedded']:
            (vendor, name, version) = (resource_info['vendor'], resource_info['name'], resource_info['version'])
            resource = CatalogueResource.objects.get(vendor=vendor, short_name=name, version=version)
            base_dir = catalogue_utils.wgt_deployer.get_base_dir(vendor, name, version)
            zf.write(os.path.join(base_dir, resource.template_uri), resource_info['src'])
        zf.close()
        wgt_file = WgtFile(f)

        resource = get_local_catalogue().publish(None, wgt_file, request.user, options, request)

        return HttpResponse(json.dumps(resource.get_processed_info(request), ensure_ascii=False), status=201, content_type='application/json; charset=utf-8')
