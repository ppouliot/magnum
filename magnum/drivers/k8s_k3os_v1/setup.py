#!/usr/bin/env python
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

import setuptools

setuptools.setup(
    name="K3OSTemplate",
    version="0.1",
    packages=[templates'],
    install_requires=['magnum'],
    package_data={
        'template': ['config.yaml']
    },
    author="Peter J. Pouliot",
    author_email="peter@pouliot.net",
    description="This is an Template to deploy K3OS K8S clusters in Magnum",
    license="Apache",
    keywords="magnum k30s template",
    entry_points={
        'magnum.template_definitions': [
            'example_template = example_template:K3osTemplate'
        ]
    }
)
