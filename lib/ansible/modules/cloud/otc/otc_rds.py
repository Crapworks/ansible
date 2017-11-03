#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2017 Christian Eichelmann - T-Systems International GmbH
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}


DOCUMENTATION = '''
---
module: otc_rds
short_description: Create/Delete Compute Instances from OpenStack
extends_documentation_fragment: openstack
version_added: "2.5"
author: "Christian Eichelmann ()"
description:
   - Create or Remove compute instances from OpenStack.
options:
   name:
     description:
        - Name that has to be given to the instance
     required: true
   image:
     description:
        - The name or id of the base image to boot.
     required: true
requirements:
    - "python >= 2.6"
    - "shade"
'''

EXAMPLES = '''
- name: Create a new instance and attaches to a network and passes metadata to the instance
  os_server:
       state: present
       auth:
         auth_url: https://region-b.geo-1.identity.hpcloudsvc.com:35357/v2.0/
         username: admin
         password: admin
         project_name: admin
       name: vm1
       image: 4f905f38-e52a-43d2-b6ec-754a13ffb529
       key_name: ansible_key
       timeout: 200
       flavor: 4
       nics:
         - net-id: 34605f38-e52a-25d2-b6ec-754a13ffb723
         - net-name: another_network
       meta:
         hostname: test1
         group: uge_master

'''

try:
    import shade
    from shade import meta
    HAS_SHADE = True
except ImportError:
    HAS_SHADE = False

import requests

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.openstack import openstack_full_argument_spec


def _exit_hostvars(module, cloud, server, changed=True):
    hostvars = meta.get_hostvars_from_server(cloud, server)
    module.exit_json(
        changed=changed, server=server, id=server.id, openstack=hostvars)


class OTCDatabase(object):
    endpoint = 'https://rds.eu-de.otc.t-systems.com/rds/v1/{}/'

    def __init__(self, module):
        self.module = module
        cloud_params = dict(self.module.params)
        self.cloud = shade.openstack_cloud(**cloud_params)

    def _api_call(self, uri, method='get', json=None, params={}):
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Language': 'en-us',
            'X-Auth-Token': self.cloud.auth_token
        }
        func = getattr(requests, method)
        resp = func(self.endpoint.format(self.cloud.current_project_id) + uri, json=json, params=params, headers=headers)
        json_data = resp.json()
        if 'message' in json_data and json_data['message'] == 'Wait response timeout.':
            self.module.fail_json(msg="Internal RDS API Timeout: {} {}".format(method.upper(), resp.url))

        return json_data

    def _needs_update(self, instance):
        for key, value in self.module.params.items():
            if key in instance and instance[key] != value:
                if key == 'name' and instance[key].startswith(value + '-' + self.module.params['datastore']['type']):
                    continue
                if key == 'flavor' and self.get_rds_flavor_id(value) == instance[key]['id']:
                    continue
                self.module.exit_json(changed=True, msg="SHOULD: {} = {} IS: {} = {}".format(key, value, key, instance[key]))
                return True
        return False

    def delete(self):
        self.module.exit_json(changed=True, result='deleted')

    def get_db_id(self):
        uri = 'datastores/{}/versions'.format(
            self.module.params['datastore']['type']
        )
        data = self._api_call(uri)
        for store in data['dataStores']:
            if store['name'] == self.module.params['datastore']['version']:
                return store['id']

        return None

    def get_rds_flavor_id(self):
        uri = 'flavors'
        params = {
            'dbId': self.get_db_id(),
            'region': self.module.params['region']
        }
        data = self._api_call(uri, params=params)
        for flavor in data['flavors']:
            if flavor['specCode'] == 'rds.{}.{}'.format(self.module.params['datastore']['type'].lower(), self.module.params['flavor']):
                return flavor['id']

        return None

    def create(self):
        params = self.module.params
        try:
            flavor_id = self.get_rds_flavor_id()
        except Exception as e:
            self.module.fail_json(msg="Error retrieving RDS flavors: {}".format(e))

        if not flavor_id:
            self.module.fail_json(msg="Could not find flavor {}".format(params['flavor']))

        api_body = dict(
            name=params['name'],
            datastore=params['datastore'],
            flavorRef=flavor_id,
            volume=params['volume'],
            region=params['region'],
            availabilityZone=params['availabilityZone'],
            vpc=params['vpc'],
            nics=params['nics'],
            securityGroup=params['securityGroup'],
            backupStrategy=params['backupStrategy'],
            dbRtPd=params['dbRtPd'],
        )
        uri = 'instances'
        resp = self._api_call(uri, method='post', json={'instance': api_body})

        self.module.exit_json(changed=True, server=resp, req={'instance': api_body})
        # _exit_hostvars(module, cloud, api_body)

    def _debug(self, msg):
        with open('/tmp/debug.log', 'w+') as fh:
            fh.write(msg)

    def state(self):
        uri = 'instances'
        resp = self._api_call(uri)
        for instance in resp['instances']:
            if instance['name'].startswith(self.module.params['name'] + '-' + self.module.params['datastore']['type']):
                uri = 'instances/' + instance['id']
                resp = self._api_call(uri)
                if not self._needs_update(resp['instance']):
                    self.module.exit_json(changed=False, database=instance)

    def _update_rds(module, cloud, server):
        changed = False

        # cloud.set_server_metadata only updates the key=value pairs, it doesn't
        # touch existing ones
        update_meta = {}
        for (k, v) in module.params['meta'].items():
            if k not in server.metadata or server.metadata[k] != v:
                update_meta[k] = v

        if update_meta:
            cloud.set_server_metadata(server, update_meta)
            changed = True
            # Refresh server vars
            server = cloud.get_server(module.params['name'])

        return (changed, server)


def main():
    argument_spec = openstack_full_argument_spec(
        name=dict(required=True),
        datastore=dict(required=True, type='dict'),
        flavor=dict(required=True),
        volume=dict(required=True, type='dict'),
        region=dict(default=None),
        availabilityZone=dict(default=None),
        vpc=dict(required=True),
        nics=dict(required=True, type='dict'),
        securityGroup=dict(required=True, type='dict'),
        backupStrategy=dict(required=True, type='dict'),
        dbRtPd=dict(required=True),
        state=dict(default='present', choices=['absent', 'present']),
    )
    module = AnsibleModule(argument_spec)

    if not HAS_SHADE:
        module.fail_json(msg='shade is required for this module')

    state = module.params['state']

    try:
        database = OTCDatabase(module)

        if state == 'present':
            database.state()
            database.create()
        elif state == 'absent':
            database.delete()
    except shade.OpenStackCloudException as e:
        module.fail_json(msg=str(e), extra_data=e.extra_data)


if __name__ == '__main__':
    main()
