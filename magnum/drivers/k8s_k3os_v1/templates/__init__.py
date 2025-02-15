# Copyright (c) 2019 Ampere Computing Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from magnum.drivers.common import template_def


class K3osTemplate(template_def.BaseTemplateDefinition):
    provides = [
        {'server_type': 'vm', 'os': 'k3os', 'coe': 'k3os_coe'},
        {'server_type': 'vm', 'os': 'k3os2', 'coe': 'k3os_coe'},
    ]

    def __init__(self):
        super(K3osTemplate, self).__init__()

        self.add_output('server_address',
                        bay_attr='api_address')
        self.add_output('node_addresses',
                        bay_attr='node_addresses')

    def template_path(self):
        return os.path.join(os.path.dirname(__file__), 'k3os.yaml')
