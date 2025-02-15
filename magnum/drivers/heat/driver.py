# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import abc
import collections
import os
import six

from string import ascii_letters
from string import digits

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils

from heatclient.common import template_utils
from heatclient import exc as heatexc

from magnum.common import clients
from magnum.common import context as mag_ctx
from magnum.common import exception
from magnum.common import keystone
from magnum.common import octavia
from magnum.common import short_id
from magnum.conductor.handlers.common import cert_manager
from magnum.conductor.handlers.common import trust_manager
from magnum.conductor import utils as conductor_utils
from magnum.drivers.common import driver
from magnum.drivers.common import k8s_monitor
from magnum.i18n import _
from magnum.objects import fields

LOG = logging.getLogger(__name__)


NodeGroupStatus = collections.namedtuple('NodeGroupStatus',
                                         'name status reason is_default')


@six.add_metaclass(abc.ABCMeta)
class HeatDriver(driver.Driver):
    """Base Driver class for using Heat

       Abstract class for implementing Drivers that leverage OpenStack Heat for
       orchestrating cluster lifecycle operations
    """

    def _extract_template_definition_up(self, context, cluster,
                                        cluster_template,
                                        scale_manager=None):
        ct_obj = conductor_utils.retrieve_ct_by_name_or_uuid(
            context,
            cluster_template)
        definition = self.get_template_definition()
        return definition.extract_definition(context, ct_obj,
                                             cluster,
                                             scale_manager=scale_manager)

    def _extract_template_definition(self, context, cluster,
                                     scale_manager=None,
                                     nodegroups=None):
        cluster_template = conductor_utils.retrieve_cluster_template(context,
                                                                     cluster)
        definition = self.get_template_definition()
        return definition.extract_definition(context, cluster_template,
                                             cluster,
                                             nodegroups=nodegroups,
                                             scale_manager=scale_manager)

    def _get_env_files(self, template_path, env_rel_paths):
        template_dir = os.path.dirname(template_path)
        env_abs_paths = [os.path.join(template_dir, f) for f in env_rel_paths]
        environment_files = []
        env_map, merged_env = (
            template_utils.process_multiple_environments_and_files(
                env_paths=env_abs_paths, env_list_tracker=environment_files))
        return environment_files, env_map

    @abc.abstractmethod
    def get_template_definition(self):
        """return an implementation of

           magnum.drivers.common.drivers.heat.TemplateDefinition
        """

        raise NotImplementedError("Must implement 'get_template_definition'")

    def create_federation(self, context, federation):
        return NotImplementedError("Must implement 'create_federation'")

    def update_federation(self, context, federation):
        return NotImplementedError("Must implement 'update_federation'")

    def delete_federation(self, context, federation):
        return NotImplementedError("Must implement 'delete_federation'")

    def update_nodegroup(self, context, cluster, nodegroup):
        # we just need to save the nodegroup here. This is because,
        # at the moment, this method is used to update min and max node
        # counts.
        nodegroup.save()

    def delete_nodegroup(self, context, cluster, nodegroup):
        # Default nodegroups share stack_id so it will be deleted
        # as soon as the cluster gets destroyed
        if not nodegroup.stack_id:
            nodegroup.destroy()
        else:
            osc = clients.OpenStackClients(context)
            self._delete_stack(context, osc, nodegroup.stack_id)

    def update_cluster_status(self, context, cluster):
        if cluster.stack_id is None:
            # NOTE(mgoddard): During cluster creation it is possible to poll
            # the cluster before its heat stack has been created. See bug
            # 1682058.
            return
        stack_ctx = mag_ctx.make_cluster_context(cluster)
        poller = HeatPoller(clients.OpenStackClients(stack_ctx), context,
                            cluster, self)
        poller.poll_and_check()

    def create_cluster(self, context, cluster, cluster_create_timeout):
        stack = self._create_stack(context, clients.OpenStackClients(context),
                                   cluster, cluster_create_timeout)
        # TODO(randall): keeping this for now to reduce/eliminate data
        # migration. Should probably come up with something more generic in
        # the future once actual non-heat-based drivers are implemented.
        cluster.stack_id = stack['stack']['id']

    def update_cluster(self, context, cluster, scale_manager=None,
                       rollback=False):
        self._update_stack(context, cluster, scale_manager, rollback)

    def create_nodegroup(self, context, cluster, nodegroup):
        stack = self._create_stack(context, clients.OpenStackClients(context),
                                   cluster, cluster.create_timeout,
                                   nodegroup=nodegroup)
        nodegroup.stack_id = stack['stack']['id']

    def get_nodegroup_extra_params(self, cluster, osc):
        raise NotImplementedError("Must implement "
                                  "'get_nodegroup_extra_params'")

    @abc.abstractmethod
    def upgrade_cluster(self, context, cluster, cluster_template,
                        max_batch_size, nodegroup, scale_manager=None,
                        rollback=False):
        raise NotImplementedError("Must implement 'upgrade_cluster'")

    def delete_cluster(self, context, cluster):
        self.pre_delete_cluster(context, cluster)

        LOG.info("Starting to delete cluster %s", cluster.uuid)
        osc = clients.OpenStackClients(context)
        for ng in cluster.nodegroups:
            ng.status = fields.ClusterStatus.DELETE_IN_PROGRESS
            ng.save()
            if ng.is_default:
                continue
            self._delete_stack(context, osc, ng.stack_id)
        self._delete_stack(context, osc, cluster.default_ng_master.stack_id)

    def resize_cluster(self, context, cluster, resize_manager,
                       node_count, nodes_to_remove, nodegroup=None,
                       rollback=False):
        self._resize_stack(context, cluster, resize_manager,
                           node_count, nodes_to_remove, nodegroup=nodegroup,
                           rollback=rollback)

    def _create_stack(self, context, osc, cluster, cluster_create_timeout,
                      nodegroup=None):

        nodegroups = [nodegroup] if nodegroup else None
        template_path, heat_params, env_files = (
            self._extract_template_definition(context, cluster,
                                              nodegroups=nodegroups))

        tpl_files, template = template_utils.get_template_contents(
            template_path)

        environment_files, env_map = self._get_env_files(template_path,
                                                         env_files)
        tpl_files.update(env_map)

        # Make sure we end up with a valid hostname
        valid_chars = set(ascii_letters + digits + '-')

        # valid hostnames are 63 chars long, leaving enough room
        # to add the random id (for uniqueness)
        if nodegroup is None:
            stack_name = cluster.name[:30]
        else:
            stack_name = "%s-%s" % (cluster.name[:20], nodegroup.name[:9])
        stack_name = stack_name.replace('_', '-')
        stack_name = stack_name.replace('.', '-')
        stack_name = ''.join(filter(valid_chars.__contains__, stack_name))

        # Make sure no duplicate stack name
        stack_name = '%s-%s' % (stack_name, short_id.generate_id())
        stack_name = stack_name.lower()
        if cluster_create_timeout:
            heat_timeout = cluster_create_timeout
        else:
            # no cluster_create_timeout value was passed in to the request
            # so falling back on configuration file value
            heat_timeout = cfg.CONF.cluster_heat.create_timeout

        heat_params['is_cluster_stack'] = nodegroup is None

        if nodegroup:
            # In case we are creating a new stack for a new nodegroup then
            # we need to extract more params.
            heat_params.update(self.get_nodegroup_extra_params(cluster, osc))

        fields = {
            'stack_name': stack_name,
            'parameters': heat_params,
            'environment_files': environment_files,
            'template': template,
            'files': tpl_files,
            'timeout_mins': heat_timeout
        }
        created_stack = osc.heat().stacks.create(**fields)

        return created_stack

    def _update_stack(self, context, cluster, scale_manager=None,
                      rollback=False):
        definition = self.get_template_definition()

        osc = clients.OpenStackClients(context)
        heat_params = {}

        # Find what changed checking the stack params
        # against the ones in the template_def.
        stack = osc.heat().stacks.get(cluster.stack_id,
                                      resolve_outputs=True)
        stack_params = stack.parameters
        definition.add_nodegroup_params(cluster)
        heat_params = definition.get_stack_diff(context, stack_params, cluster)
        LOG.debug('Updating stack with these params: %s', heat_params)
        scale_params = definition.get_scale_params(context,
                                                   cluster,
                                                   scale_manager)
        heat_params.update(scale_params)

        fields = {
            'parameters': heat_params,
            'existing': True,
            'disable_rollback': not rollback
        }

        osc.heat().stacks.update(cluster.stack_id, **fields)

    def _resize_stack(self, context, cluster, resize_manager,
                      node_count, nodes_to_remove, nodegroup=None,
                      rollback=False):
        definition = self.get_template_definition()
        osc = clients.OpenStackClients(context)

        # Find what changed checking the stack params
        # against the ones in the template_def.
        stack = osc.heat().stacks.get(nodegroup.stack_id,
                                      resolve_outputs=True)
        stack_params = stack.parameters
        definition.add_nodegroup_params(cluster, nodegroups=[nodegroup])
        heat_params = definition.get_stack_diff(context, stack_params, cluster)
        LOG.debug('Updating stack with these params: %s', heat_params)

        scale_params = definition.get_scale_params(context,
                                                   cluster,
                                                   resize_manager,
                                                   nodes_to_remove)
        heat_params.update(scale_params)
        fields = {
            'parameters': heat_params,
            'existing': True,
            'disable_rollback': not rollback
        }

        osc = clients.OpenStackClients(context)
        osc.heat().stacks.update(nodegroup.stack_id, **fields)

    def _delete_stack(self, context, osc, stack_id):
        osc.heat().stacks.delete(stack_id)


class KubernetesDriver(HeatDriver):
    """Base driver for Kubernetes clusters."""

    def get_monitor(self, context, cluster):
        return k8s_monitor.K8sMonitor(context, cluster)

    def get_scale_manager(self, context, osclient, cluster):
        # FIXME: Until the kubernetes client is fixed, remove
        # the scale_manager.
        # https://bugs.launchpad.net/magnum/+bug/1746510
        return None

    def pre_delete_cluster(self, context, cluster):
        """Delete cloud resources before deleting the cluster."""
        if keystone.is_octavia_enabled():
            LOG.info("Starting to delete loadbalancers for cluster %s",
                     cluster.uuid)
            octavia.delete_loadbalancers(context, cluster)

    def upgrade_cluster(self, context, cluster, cluster_template,
                        max_batch_size, nodegroup, scale_manager=None,
                        rollback=False):
        raise NotImplementedError("Must implement 'upgrade_cluster'")


class HeatPoller(object):

    def __init__(self, openstack_client, context, cluster, cluster_driver):
        self.openstack_client = openstack_client
        self.context = context
        self.cluster = cluster
        self.cluster_template = conductor_utils.retrieve_cluster_template(
            self.context, cluster)
        self.template_def = cluster_driver.get_template_definition()

    def poll_and_check(self):
        # TODO(yuanying): temporary implementation to update api_address,
        # node_addresses and cluster status
        ng_statuses = list()
        self.default_ngs = list()
        for nodegroup in self.cluster.nodegroups:
            self.nodegroup = nodegroup
            if self.nodegroup.is_default:
                self.default_ngs.append(self.nodegroup)
            status = self.extract_nodegroup_status()
            # In case a non-default nodegroup is deleted, None
            # is returned. We shouldn't add None in the list
            if status is not None:
                ng_statuses.append(status)
        self.aggregate_nodegroup_statuses(ng_statuses)

    def extract_nodegroup_status(self):

        if self.nodegroup.stack_id is None:
            # There is a slight window for a race condition here. If
            # a nodegroup is created and just before the stack_id is
            # assigned to it, this periodic task is executed, the
            # periodic task would try to find the status of the
            # stack with id = None. At that time the nodegroup status
            # is already set to CREATE_IN_PROGRESS by the conductor.
            # Keep this status for this loop until the stack_id is assigned.
            return NodeGroupStatus(name=self.nodegroup.name,
                                   status=self.nodegroup.status,
                                   is_default=self.nodegroup.is_default,
                                   reason=self.nodegroup.status_reason)

        try:
            # Do not resolve outputs by default. Resolving all
            # node IPs is expensive on heat.
            stack = self.openstack_client.heat().stacks.get(
                self.nodegroup.stack_id, resolve_outputs=False)

            # poll_and_check is detached and polling long time to check
            # status, so another user/client can call delete cluster/stack.
            if stack.stack_status == fields.ClusterStatus.DELETE_COMPLETE:
                if self.nodegroup.is_default:
                    self._check_delete_complete()
                else:
                    self.nodegroup.destroy()
                    return

            if stack.stack_status in (fields.ClusterStatus.CREATE_COMPLETE,
                                      fields.ClusterStatus.UPDATE_COMPLETE):
                # Resolve all outputs if the stack is COMPLETE
                stack = self.openstack_client.heat().stacks.get(
                    self.nodegroup.stack_id, resolve_outputs=True)

                self._sync_cluster_and_template_status(stack)
            elif stack.stack_status != self.nodegroup.status:
                self.template_def.nodegroup_output_mappings = list()
                self.template_def.update_outputs(
                    stack, self.cluster_template, self.cluster,
                    nodegroups=[self.nodegroup])
                self._sync_cluster_status(stack)

            if stack.stack_status in (fields.ClusterStatus.CREATE_FAILED,
                                      fields.ClusterStatus.DELETE_FAILED,
                                      fields.ClusterStatus.UPDATE_FAILED,
                                      fields.ClusterStatus.ROLLBACK_COMPLETE,
                                      fields.ClusterStatus.ROLLBACK_FAILED):
                self._sync_cluster_and_template_status(stack)
                self._nodegroup_failed(stack)
        except heatexc.NotFound:
            self._sync_missing_heat_stack()
        return NodeGroupStatus(name=self.nodegroup.name,
                               status=self.nodegroup.status,
                               is_default=self.nodegroup.is_default,
                               reason=self.nodegroup.status_reason)

    def aggregate_nodegroup_statuses(self, ng_statuses):
        # NOTE(ttsiouts): Aggregate the nodegroup statuses and set the
        # cluster overall status.
        FAILED = '_FAILED'
        IN_PROGRESS = '_IN_PROGRESS'
        COMPLETE = '_COMPLETE'
        UPDATE = 'UPDATE'

        previous_state = self.cluster.status
        self.cluster.status_reason = None

        # Both default nodegroups will have the same status so it's
        # enough to check one of them.
        self.cluster.status = self.cluster.default_ng_master.status
        default_ng = self.cluster.default_ng_master
        if (default_ng.status.endswith(IN_PROGRESS) or
                default_ng.status == fields.ClusterStatus.DELETE_COMPLETE):
            self.cluster.save()
            return

        # Keep priority to the states below
        for state in (IN_PROGRESS, FAILED, COMPLETE):
            if any(ns.status.endswith(state) for ns in ng_statuses
                   if not ns.is_default):
                status = getattr(fields.ClusterStatus, UPDATE+state)
                self.cluster.status = status
                if state == FAILED:
                    reasons = ["%s failed" % (ns.name)
                               for ns in ng_statuses
                               if ns.status.endswith(FAILED)]
                    self.cluster.status_reason = ' ,'.join(reasons)
                break

        if self.cluster.status == fields.ClusterStatus.CREATE_COMPLETE:
            # Consider the scenario where the user:
            # - creates the cluster (cluster: create_complete)
            # - adds a nodegroup (cluster: update_complete)
            # - deletes the nodegroup
            # The cluster should go to CREATE_COMPLETE only if the previous
            # state was CREATE_COMPLETE or CREATE_IN_PROGRESS. In all other
            # cases, just go to UPDATE_COMPLETE.
            if previous_state not in (fields.ClusterStatus.CREATE_COMPLETE,
                                      fields.ClusterStatus.CREATE_IN_PROGRESS):
                self.cluster.status = fields.ClusterStatus.UPDATE_COMPLETE

        self.cluster.save()

    def _delete_complete(self):
        LOG.info('Cluster has been deleted, stack_id: %s',
                 self.cluster.stack_id)
        try:
            trust_manager.delete_trustee_and_trust(self.openstack_client,
                                                   self.context,
                                                   self.cluster)
            cert_manager.delete_certificates_from_cluster(self.cluster,
                                                          context=self.context)
            cert_manager.delete_client_files(self.cluster,
                                             context=self.context)

        except exception.ClusterNotFound:
            LOG.info('The cluster %s has been deleted by others.',
                     self.cluster.uuid)

    def _sync_cluster_status(self, stack):
        self.nodegroup.status = stack.stack_status
        self.nodegroup.status_reason = stack.stack_status_reason
        self.nodegroup.save()

    def get_version_info(self, stack):
        stack_param = self.template_def.get_heat_param(
            cluster_attr='coe_version')
        if stack_param:
            self.cluster.coe_version = stack.parameters[stack_param]

        version_module_path = self.template_def.driver_module_path+'.version'
        try:
            ver = importutils.import_module(version_module_path)
            container_version = ver.container_version
        except Exception:
            container_version = None
        self.cluster.container_version = container_version

    def _sync_cluster_and_template_status(self, stack):
        self.template_def.nodegroup_output_mappings = list()
        self.template_def.update_outputs(stack, self.cluster_template,
                                         self.cluster,
                                         nodegroups=[self.nodegroup])
        self.get_version_info(stack)
        self._sync_cluster_status(stack)

    def _nodegroup_failed(self, stack):
        LOG.error('Nodegroup error, stack status: %(ng_status)s, '
                  'stack_id: %(stack_id)s, '
                  'reason: %(reason)s',
                  {'ng_status': stack.stack_status,
                   'stack_id': self.nodegroup.stack_id,
                   'reason': self.nodegroup.status_reason})

    def _sync_missing_heat_stack(self):
        if self.nodegroup.status == fields.ClusterStatus.DELETE_IN_PROGRESS:
            self._sync_missing_stack(fields.ClusterStatus.DELETE_COMPLETE)
            if self.nodegroup.is_default:
                self._check_delete_complete()
        elif self.nodegroup.status == fields.ClusterStatus.CREATE_IN_PROGRESS:
            self._sync_missing_stack(fields.ClusterStatus.CREATE_FAILED)
        elif self.nodegroup.status == fields.ClusterStatus.UPDATE_IN_PROGRESS:
            self._sync_missing_stack(fields.ClusterStatus.UPDATE_FAILED)

    def _check_delete_complete(self):
        default_ng_statuses = [ng.status for ng in self.default_ngs]
        if all(status == fields.ClusterStatus.DELETE_COMPLETE
               for status in default_ng_statuses):
            self._delete_complete()

    def _sync_missing_stack(self, new_status):
        self.nodegroup.status = new_status
        self.nodegroup.status_reason = _("Stack with id %s not found in "
                                         "Heat.") % self.cluster.stack_id
        self.nodegroup.save()
        LOG.info("Nodegroup with id %(id)s has been set to "
                 "%(status)s due to stack with id %(sid)s "
                 "not found in Heat.",
                 {'id': self.nodegroup.uuid, 'status': self.nodegroup.status,
                  'sid': self.nodegroup.stack_id})
