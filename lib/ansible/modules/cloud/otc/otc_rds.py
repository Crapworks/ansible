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
short_description: Create/Delete RDS Instances from OTC
extends_documentation_fragment: openstack
version_added: "2.5"
author: "Christian Eichelmann ()"
description:
   - Create or Remove RDS Instances from OTC
options:
   name:
     description:
        - Name that has to be given to the instance
     required: true
   region:
     description:
        - Region to use
     required: true
   datastore:
     description:
        - 
     required: true
   flavor:
     description:
        - 
     required: true
   volume:
     description:
        - 
     required: true
requirements:
    - "python >= 2.6"
    - "shade"
'''

EXAMPLES = '''
- name: create mysql database
  otc_rds:
    name: test-mysql
    state: present
    region: eu-de
    datastore:
      type: MySQL
      version: "5.6.30"
    flavor: s1.medium
    volume:
      type: COMMON
      size: 100
    region: eu-de
    availabilityZone: eu-de-01
    vpc: "{{ testnet_router.id }}"
    nics:
      subnetId: "{{ testnet_network.id }}"
    securityGroup:
      id: "{{ testsecgrp.id }}"
    backupStrategy:
      startTime: "01:00:00"
      keepDays: 3
    dbRtPd: Test@123
'''

try:
    import shade
    HAS_SHADE = True
except ImportError:
    HAS_SHADE = False

import requests

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.openstack import openstack_full_argument_spec


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

    def _get_updates(self, instance):
        update_list = {}
        for key, value in self.module.params.items():
            if key in instance and instance[key] != value:
                if key == 'name' and instance[key].startswith(value + '-' + self.module.params['datastore']['type']):
                    continue
                if key == 'flavor' and self.get_rds_flavor_id() == instance[key]['id']:
                    continue
                if key == 'volume' and value['type'] == instance[key]['type'] and value['size'] == instance[key]['size']:
                    continue
                update_list.update({key: {'old': instance[key], 'new': value}})

        return update_list

    def delete(self):
        instance = self.get_instance()
        if not instance:
            self.module.exit_json(changed=False, result='ok')

        uri = 'instances/{}'.format(
            instance['id']
        )
        data = self._api_call(uri, method='delete')
        self.module.exit_json(changed=True, result='deleted', api=data)

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

    def get_instance(self):
        uri = 'instances'
        resp = self._api_call(uri)
        for instance in resp['instances']:
            if instance['name'].startswith(self.module.params['name'] + '-' + self.module.params['datastore']['type']):
                return instance
        return None

    def update_volume_size(self, instance_id, size):
        uri = 'instances/{}/action'.format(instance_id)
        resp = self._api_call(uri, method='post', json={'resize': {'volume': {'size': size}}})
        return resp

    def update_flavor(self, instance_id):
        uri = 'instances/{}/action'.format(instance_id)
        resp = self._api_call(uri, method='post', json={'resize': {'flavorRef': self.get_rds_flavor_id()}})
        return resp

    def update(self):
        instance = self.get_instance()
        if not instance:
            return None

        uri = 'instances/' + instance['id']
        resp = self._api_call(uri)
        updates = self._get_updates(resp['instance'])
        if not updates:
            self.module.exit_json(changed=False, database=instance)
        else:
            results = {}
            for prop, changes in updates.items():
                if prop == 'volume':
                    if changes['old']['type'] != changes['new']['type']:
                        self.module.fail_json(msg='only volume size can be changed on the fly, not volume type', changes=changes)
                    resp = self.update_volume_size(instance['id'], changes['new']['size'])
                    results['volume'] = resp
                elif prop == 'flavor':
                    resp = self.update_flavor(instance['id'])
                    results['flavor'] = resp
                else:
                    self.module.fail_json(msg='only volume size and flavor can be changed on the fly')

            self.module.exit_json(changed=True, results=results)


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
            database.update()
            database.create()
        elif state == 'absent':
            database.delete()
    except shade.OpenStackCloudException as e:
        module.fail_json(msg=str(e), extra_data=e.extra_data)


if __name__ == '__main__':
    main()
