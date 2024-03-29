# -*- coding: utf-8 -*-

# Copyright (c) 2011-2015 CoNWeT Lab., Universidad Politécnica de Madrid

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

from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext as _
import six

from wirecloud.catalogue.models import CatalogueResource
from wirecloud.commons.utils.db import save_alternative
from wirecloud.commons.utils.template import TemplateParser
from wirecloud.platform.context.utils import get_context_values
from wirecloud.platform.widget.utils import get_or_add_widget_from_catalogue
from wirecloud.platform.iwidget.utils import SaveIWidget, set_initial_values
from wirecloud.platform.preferences.views import update_tab_preferences, update_workspace_preferences
from wirecloud.platform.models import Workspace, UserWorkspace
from wirecloud.platform.wiring.utils import get_wiring_skeleton, get_endpoint_name, is_empty_wiring
from wirecloud.platform.workspace.utils import createTab, normalize_forced_values, TemplateValueProcessor


def buildWorkspaceFromTemplate(template, user, allow_renaming=False, new_name=None):

    if not isinstance(template, TemplateParser):
        template = TemplateParser(template)

    if template.get_resource_type() != 'mashup':
        raise TypeError('Unsupported resource type: %s' % template.get_resource_type())

    if new_name is not None:
        name = new_name
    else:
        name = template.get_resource_processed_info(process_urls=False)['title']

    # Workspace creation
    workspace = Workspace(name=name, creator=user)
    if allow_renaming:
        save_alternative(Workspace, 'name', workspace)
    else:
        workspace.save()

    # Adding user reference to workspace in the many to many relationship
    user_workspace = UserWorkspace(user=user, workspace=workspace, active=False)
    user_workspace.save()

    fillWorkspaceUsingTemplate(workspace, template)

    return (workspace, user_workspace)


@python_2_unicode_compatible
class MissingDependencies(Exception):

    def __init__(self, missing_dependencies):
        self.missing_dependencies = missing_dependencies

    def __str__(self):
        return _('Missing dependencies')


def check_mashup_dependencies(template, user):

    missing_dependencies = set()
    dependencies = template.get_resource_dependencies()

    for dependency in dependencies:
        (vendor, name, version) = dependency.split('/')
        try:
            catalogue_resource = CatalogueResource.objects.get(vendor=vendor, short_name=name, version=version)
            if not catalogue_resource.is_available_for(user):
                raise CatalogueResource.DoesNotExist
        except CatalogueResource.DoesNotExist:
            missing_dependencies.add(dependency)

    if len(missing_dependencies) > 0:
        raise MissingDependencies(list(missing_dependencies))


def map_id(endpoint_view, id_mapping):
    return id_mapping[endpoint_view['type']]["%s" % endpoint_view['id']]['id']


def _remap_component_ids(id_mapping, components_description, isGlobal=False):

    operators = {}
    for key, operator in six.iteritems(components_description['operator']):
        operators[six.text_type(id_mapping['operator'][key]['id'])] = operator
    components_description['operator'] = operators

    widgets = {}
    for key, widget in six.iteritems(components_description['widget']):
        if isGlobal:
            widget['name'] = id_mapping['widget'][key]['name']

        widgets[six.text_type(id_mapping['widget'][key]['id'])] = widget
    components_description['widget'] = widgets


def _create_new_behaviour(mashup_description, title, description):

    operators = {}
    for key, operator in six.iteritems(mashup_description['components']['operator']):
        operators[six.text_type(key)] = {}

    widgets = {}
    for key, widget in six.iteritems(mashup_description['components']['widget']):
        widgets[six.text_type(key)] = {}

    connections = []
    for connection in mashup_description['connections']:
        connections.append({
            'sourcename': connection['sourcename'],
            'targetname': connection['targetname']
        })

    mashup_description['behaviours'].append({
        'title': title,
        'description': description,
        'components': {
            'operator': operators,
            'widget': widgets,
        },
        'connections': connections,
    })


def _remap_connection_endpoints(source_mapping, target_mapping, description):
    connections = []

    for connection in description['connections']:
        new_connection = connection
        new_connection['sourcename'] = source_mapping[connection['sourcename']]
        new_connection['targetname'] = target_mapping[connection['targetname']]
        connections.append(new_connection)

    description['connections'] = connections


def fillWorkspaceUsingTemplate(workspace, template):

    if not isinstance(template, TemplateParser):
        template = TemplateParser(template)

    if template.get_resource_type() != 'mashup':
        raise TypeError('Unsupported resource type: %s' % template.get_resource_type())

    user = workspace.creator

    context_values = get_context_values(workspace, workspace.creator)
    processor = TemplateValueProcessor({'user': user, 'context': context_values})

    mashup_description = template.get_resource_info()

    new_values = {}
    id_mapping = {
        'operator': {},
        'widget': {},
    }
    for preference_name in mashup_description['preferences']:
        new_values[preference_name] = {
            'inherit': False,
            'value': mashup_description['preferences'][preference_name],
        }

    if len(new_values) > 0:
        update_workspace_preferences(workspace, new_values)

    new_forced_values = {
        'extra_prefs': [],
        'iwidget': {},
        'ioperator': {},
    }
    for param in mashup_description['params']:
        new_forced_values['extra_prefs'].append({
            'name': param['name'],
            'inheritable': False,
            'label': param.get('label'),
            'type': param.get('type'),
        })

    for tab_entry in mashup_description['tabs']:
        tab = createTab(tab_entry.get('name'), workspace, allow_renaming=True)

        new_values = {}
        for preference_name in tab_entry['preferences']:
            new_values[preference_name] = {
                'inherit': False,
                'value': tab_entry['preferences'][preference_name],
            }

        if len(new_values) > 0:
            update_tab_preferences(tab, new_values)

        for resource in tab_entry['resources']:

            position = resource['position']
            rendering = resource['rendering']

            widget = get_or_add_widget_from_catalogue(resource.get('vendor'), resource.get('name'), resource.get('version'), user)

            iwidget_data = {
                "widget": widget.uri,
                "title": resource.get('title'),
                "left": int(position.get('x')),
                "top": int(position.get('y')),
                "icon_left": 0,
                "icon_top": 0,
                "zIndex": int(position.get('z')),
                "width": int(rendering.get('width')),
                "height": int(rendering.get('height')),
                "layout": int(rendering.get('layout')),
                "minimized": rendering['minimized'],
                "fulldragboard": rendering['fulldragboard'],
            }

            iwidget = SaveIWidget(iwidget_data, user, tab, commit=False)
            if resource.get('readonly'):
                iwidget.readOnly = True

            initial_variable_values = {}
            iwidget_forced_values = {}
            iwidget_info = widget.resource.get_processed_info(process_variables=True)
            for prop_name in resource['properties']:
                prop = resource['properties'][prop_name]
                read_only = prop.get('readonly')
                if prop.get('value', None) is not None:
                    value = prop['value']
                else:
                    value = iwidget_info['variables']['properties'][prop_name]['default']
                if read_only:
                    iwidget_forced_values[prop_name] = {'value': value}
                else:
                    initial_variable_values[prop_name] = processor.process(value)

            for pref_name in resource['preferences']:
                pref = resource['preferences'][pref_name]
                read_only = pref.get('readonly')
                if pref.get('value', None) is not None:
                    value = pref['value']
                else:
                    value = iwidget_info['variables']['preferences'][pref_name]['default']
                if read_only:
                    iwidget_forced_values[pref_name] = {'value': value, 'hidden': pref.get('hidden', False)}
                else:
                    initial_variable_values[pref_name] = processor.process(value)

            set_initial_values(iwidget, initial_variable_values, iwidget_info)
            iwidget.save()

            if len(iwidget_forced_values) > 0:
                new_forced_values['iwidget'][six.text_type(iwidget.id)] = iwidget_forced_values

            id_mapping['widget'][resource.get('id')] = {
                'id': iwidget.id,
                'name': resource.get('vendor') + "/" + resource.get('name') + "/" + resource.get('version')
            }

    # wiring
    if len(workspace.wiringStatus) == 0:
        workspace.wiringStatus = get_wiring_skeleton()

    max_id = 0

    for id_ in workspace.wiringStatus['operators'].keys():
        if int(id_) > max_id:
            max_id = int(id_)

    # Process operators info
    for operator_id, operator in six.iteritems(mashup_description['wiring']['operators']):
        max_id += 1
        new_id = "%s" % max_id
        id_mapping['operator'][operator_id] = {
            'id': new_id
        }
        workspace.wiringStatus['operators'][new_id] = {
            'id': new_id,
            'name': operator['name'],
            'preferences': operator['preferences'],
        }

        ioperator_forced_values = {}
        for pref_id, pref in six.iteritems(operator['preferences']):
            if pref.get('readonly', False):
                ioperator_forced_values[pref_id] = {'value': pref.get('value'), 'hidden': pref.get('hidden', False)}

        if len(ioperator_forced_values) > 0:
            new_forced_values['ioperator'][new_id] = ioperator_forced_values

    # Remap connection ids
    source_mapping = {}
    target_mapping = {}

    for connection in mashup_description['wiring']['connections']:
        old_source_name = get_endpoint_name(connection['source'])
        old_target_name = get_endpoint_name(connection['target'])

        connection['source']['id'] = map_id(connection['source'], id_mapping)
        connection['target']['id'] = map_id(connection['target'], id_mapping)

        source_mapping[old_source_name] = get_endpoint_name(connection['source'])
        target_mapping[old_target_name] = get_endpoint_name(connection['target'])

    # Add new connections
    workspace.wiringStatus['connections'] += mashup_description['wiring']['connections']

    # Merging visual description...
    _remap_component_ids(id_mapping, mashup_description['wiring']['visualdescription']['components'], isGlobal=True)
    _remap_connection_endpoints(source_mapping, target_mapping, mashup_description['wiring']['visualdescription'])

    # Remap mashup description behaviours' ids
    if len(mashup_description['wiring']['visualdescription']['behaviours']) != 0:
        for behaviour in mashup_description['wiring']['visualdescription']['behaviours']:
            _remap_component_ids(id_mapping, behaviour['components'])
            _remap_connection_endpoints(source_mapping, target_mapping, behaviour)

    if len(workspace.wiringStatus['visualdescription']['behaviours']) != 0 or len(mashup_description['wiring']['visualdescription']['behaviours']) != 0:
        if len(workspace.wiringStatus['visualdescription']['behaviours']) == 0 and not is_empty_wiring(workspace.wiringStatus['visualdescription']):
            # *TODO* flag to check if the user really want to merge both workspaces.
            _create_new_behaviour(workspace.wiringStatus['visualdescription'], _("Original wiring"), _("This is the wiring description of the original workspace"))

        if len(mashup_description['wiring']['visualdescription']['behaviours']) == 0:
            _create_new_behaviour(mashup_description['wiring']['visualdescription'], _("Merged wiring"), _("This is the wiring description of the merged mashup."))

        workspace.wiringStatus['visualdescription']['behaviours'] += mashup_description['wiring']['visualdescription']['behaviours']

    # Merge global behaviour components and connections
    workspace.wiringStatus['visualdescription']['components']['operator'].update(mashup_description['wiring']['visualdescription']['components']['operator'])
    workspace.wiringStatus['visualdescription']['components']['widget'].update(mashup_description['wiring']['visualdescription']['components']['widget'])
    workspace.wiringStatus['visualdescription']['connections'] += mashup_description['wiring']['visualdescription']['connections']

    # Forced values
    normalize_forced_values(workspace)

    workspace.forcedValues['extra_prefs'] += new_forced_values['extra_prefs']
    workspace.forcedValues['iwidget'].update(new_forced_values['iwidget'])
    workspace.forcedValues['ioperator'].update(new_forced_values['ioperator'])

    workspace.save()
