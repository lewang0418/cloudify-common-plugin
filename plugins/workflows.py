########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

from cloudify import constants, utils
from cloudify.decorators import workflow
from cloudify.plugins import lifecycle

ALL_TO_ALL = 'all_to_all'
ALL_TO_ONE = 'all_to_one'
ONE_TO_ALL = 'one_to_all'
ONE_TO_ONE = 'one_to_one'

@workflow
def install(ctx, **kwargs):
    """Default install workflow"""

    lifecycle.install_node_instances(
        graph=ctx.graph_mode(),
        node_instances=set(ctx.node_instances))


@workflow
def uninstall(ctx, **kwargs):
    """Default uninstall workflow"""

    lifecycle.uninstall_node_instances(
        graph=ctx.graph_mode(),
        node_instances=set(ctx.node_instances))


@workflow
def auto_heal_reinstall_node_subgraph(
        ctx,
        node_instance_id,
        diagnose_value='Not provided',
        **kwargs):
    """Reinstalls the whole subgraph of the system topology

    The subgraph consists of all the nodes that are hosted in the
    failing node's compute and the compute itself.
    Additionally it unlinks and establishes appropriate relationships

    :param ctx: cloudify context
    :param node_id: failing node's id
    :param diagnose_value: diagnosed reason of failure
    """

    ctx.logger.info("Starting 'heal' workflow on {0}, Diagnosis: {1}"
                    .format(node_instance_id, diagnose_value))
    failing_node = ctx.get_node_instance(node_instance_id)
    failing_node_host = ctx.get_node_instance(
        failing_node._node_instance.host_id
    )
    subgraph_node_instances = failing_node_host.get_contained_subgraph()
    intact_nodes = set(ctx.node_instances) - subgraph_node_instances
    graph = ctx.graph_mode()
    lifecycle.reinstall_node_instances(
        graph=graph,
        node_instances=subgraph_node_instances,
        intact_nodes=intact_nodes)


@workflow
def scale(ctx, node_id, delta, scale_compute, **kwargs):
    """Scales in/out the subgraph of node_id.

    If `scale_compute` is set to false, the subgraph will consist of all
    the nodes that are contained in `node_id` and `node_id` itself.
    If `scale_compute` is set to true, the subgraph will consist of all
    nodes that are contained in the compute node that contains `node_id`
    and the compute node itself.
    If `node_id` is not contained in a compute node and is not a compute node,
    this property is ignored.

    `delta` is used to specify the scale factor.
    For `delta > 0`: If current number of instances is `N`, scale out to
    `N + delta`.
    For `delta < 0`: If current number of instances is `N`, scale in to
    `N - |delta|`.

    :param ctx: cloudify context
    :param node_id: the node_id to scale
    :param delta: scale in/out factor
    :param scale_compute: should scale apply on compute node containing
                          'node_id'
    """
    graph = ctx.graph_mode()
    node = ctx.get_node(node_id)
    if not node:
        raise ValueError("Node {0} doesn't exist".format(node_id))
    if delta == 0:
        ctx.logger.info('delta parameter is 0, so no scaling will take place.')
        return
#    host_node = node.host_node
#    scaled_node = host_node if (scale_compute and host_node) else node

    modified_nodes = {}
    _get_related_modified_nodes(ctx, node, delta, modified_nodes)
    modification = ctx.deployment.start_modification(modified_nodes)

    try:
        ctx.logger.info('Deployment modification started. '
                        '[modification_id={0}]'.format(modification.id))

        if delta > 0:
            added_and_related = set(modification.added.node_instances)
            added = set(i for i in added_and_related
                        if i.modification == 'added')
            related = added_and_related - added
            try:
                lifecycle.install_node_instances(
                    graph=graph,
                    node_instances=added,
                    intact_nodes=related)
            except:
                ctx.logger.error('Scale out failed, scaling back in.')
                for task in graph.tasks_iter():
                    graph.remove_task(task)
                lifecycle.uninstall_node_instances(
                    graph=graph,
                    node_instances=added,
                    intact_nodes=related)
                raise
        else:
            removed_and_related = set(modification.removed.node_instances)
            removed = set(i for i in removed_and_related
                          if i.modification == 'removed')
            related = removed_and_related - removed
            lifecycle.uninstall_node_instances(
                graph=graph,
                node_instances=removed,
                intact_nodes=related)
    except:
        ctx.logger.warn('Rolling back deployment modification. '
                        '[modification_id={0}]'.format(modification.id))
        try:
            modification.rollback()
        except:
            ctx.logger.warn('Deployment modification rollback failed. The '
                            'deployment model is most likely in some corrupted'
                            ' state.'
                            '[modification_id={0}]'.format(modification.id))
            raise
        raise
    else:
        try:
            modification.finish()
        except:
            ctx.logger.warn('Deployment modification finish failed. The '
                            'deployment model is most likely in some corrupted'
                            ' state.'
                            '[modification_id={0}]'.format(modification.id))
            raise


def _get_related_modified_nodes(ctx, node, delta, modified_nodes):
    # These following parameters are not exposed at the moment,
    # but should be used to control which node instances get scaled in
    # (when scaling in).
    # They are mentioned here, because currently, the modification API
    # is not very documented.
    # Special care should be taken because if `scale_compute == True`
    # (which is the default), then these ids should be the compute node
    # instance ids which are not necessarily instances of the node
    # specified by `node_id`.

    # Node instances denoted by these instance ids should be *kept* if
    # possible.
    # 'removed_ids_exclude_hint': [],

    # Node instances denoted by these instance ids should be *removed*
    # if possible.
    # 'removed_ids_include_hint': []

    curr_num_instances = node.number_of_instances
    planned_num_instances = curr_num_instances + delta
    if planned_num_instances < 0:
        raise ValueError('Provided delta: {0} is illegal. current number of'
                         'instances of node {1} is {2}'
                         .format(delta, node_id, curr_num_instances))

    modified_nodes[node.id] = {'instances': planned_num_instances}

    if not node.relationships:
        return

    for relationship in node.relationships:
        if relationship.connection_type == ONE_TO_ONE:
            node = ctx.get_node(relationship.target_id)
            _get_related_modified_nodes(ctx, node, delta, modified_nodes)



def _filter_node_instances(ctx, node_ids, node_instance_ids, type_names):
    filtered_node_instances = []
    for node in ctx.nodes:
        if node_ids and node.id not in node_ids:
            continue
        if type_names and not next((type_name for type_name in type_names if
                                    type_name in node.type_hierarchy), None):
            continue

        for instance in node.instances:
            if node_instance_ids and instance.id not in node_instance_ids:
                continue
            filtered_node_instances.append(instance)
    return filtered_node_instances


def _get_all_host_instances(ctx):
    node_instances = set()
    for node_instance in ctx.node_instances:
        if lifecycle.is_host_node(node_instance):
            node_instances.add(node_instance)
    return node_instances


@workflow
def install_new_agents(ctx, install_agent_timeout, node_ids,
                       node_instance_ids, validate=True, install=True, **_):
    if node_ids or node_instance_ids:
        filtered_node_instances = _filter_node_instances(
            ctx=ctx,
            node_ids=node_ids,
            node_instance_ids=node_instance_ids,
            type_names=[])
        error = False
        for node_instance in filtered_node_instances:
            if not lifecycle.is_host_node(node_instance):
                msg = 'Node instance {0} is not host.'.format(node_instance.id)
                ctx.logger.error(msg)
                error = True
            elif utils.internal.get_install_method(
                    node_instance.node.properties) \
                    == constants.AGENT_INSTALL_METHOD_NONE:
                msg = ('Agent should not be installed on '
                       'node instance {0}').format(node_instance.id)
                ctx.logger.error(msg)
                error = True
        if error:
            raise ValueError('Specified filters are not correct.')
        else:
            hosts = filtered_node_instances
    else:
        hosts = [
            host for host in _get_all_host_instances(ctx)
            if utils.internal.get_install_method(host.node.properties) !=
            constants.AGENT_INSTALL_METHOD_NONE]

    for host in hosts:
        state = host.get_state().get()
        if state != 'started':
            raise RuntimeError('Node {0} is not started (state: {1})'.format(
                host.id,
                state))
    graph = ctx.graph_mode()
    if validate:
        validate_subgraph = graph.subgraph('validate')
        for host in hosts:
            seq = validate_subgraph.sequence()
            seq.add(
                host.send_event('Validating agent connection.'),
                host.execute_operation(
                    'cloudify.interfaces.cloudify_agent.validate_amqp',
                    kwargs={
                        'fail_on_agent_not_installable': True
                    }),
                host.send_event('Validation done'))
    if install:
        install_subgraph = graph.subgraph('install')
        for host in hosts:
            seq = install_subgraph.sequence()
            seq.add(
                host.send_event('Installing new agent'),
                host.execute_operation(
                    'cloudify.interfaces.cloudify_agent.create_amqp',
                    kwargs={'install_agent_timeout': install_agent_timeout},
                    allow_kwargs_override=True),
                host.send_event('New agent installed.'),
                host.execute_operation(
                    'cloudify.interfaces.cloudify_agent.validate_amqp',
                    kwargs={
                        'fail_on_agent_dead': True
                    }),
                *lifecycle.prepare_running_agent(host)
            )
            for subnode in host.get_contained_subgraph():
                seq.add(subnode.execute_operation(
                    'cloudify.interfaces.monitoring.start'))
    if validate and install:
        graph.add_dependency(install_subgraph, validate_subgraph)
    graph.execute()


@workflow
def execute_operation(ctx, operation, operation_kwargs, allow_kwargs_override,
                      run_by_dependency_order, type_names, node_ids,
                      node_instance_ids, **kwargs):
    """ A generic workflow for executing arbitrary operations on nodes """

    graph = ctx.graph_mode()
    subgraphs = {}

    # filtering node instances
    filtered_node_instances = _filter_node_instances(
        ctx=ctx,
        node_ids=node_ids,
        node_instance_ids=node_instance_ids,
        type_names=type_names)

    if run_by_dependency_order:
        # if run by dependency order is set, then create stub subgraphs for the
        # rest of the instances. This is done to support indirect
        # dependencies, i.e. when instance A is dependent on instance B
        # which is dependent on instance C, where A and C are to be executed
        # with the operation on (i.e. they're in filtered_node_instances)
        # yet B isn't.
        # We add stub subgraphs rather than creating dependencies between A
        # and C themselves since even though it may sometimes increase the
        # number of dependency relationships in the execution graph, it also
        # ensures their number is linear to the number of relationships in
        # the deployment (e.g. consider if A and C are one out of N instances
        # of their respective nodes yet there's a single instance of B -
        # using subgraphs we'll have 2N relationships instead of N^2).
        filtered_node_instances_ids = set(inst.id for inst in
                                          filtered_node_instances)
        for instance in ctx.node_instances:
            if instance.id not in filtered_node_instances_ids:
                subgraphs[instance.id] = graph.subgraph(instance.id)

    # preparing the parameters to the execute_operation call
    exec_op_params = {
        'kwargs': operation_kwargs,
        'operation': operation
    }
    if allow_kwargs_override is not None:
        exec_op_params['allow_kwargs_override'] = allow_kwargs_override

    # registering actual tasks to sequences
    for instance in filtered_node_instances:
        start_event_message = 'Starting operation {0}'.format(operation)
        if operation_kwargs:
            start_event_message += ' (Operation parameters: {0})'.format(
                operation_kwargs)
        subgraph = graph.subgraph(instance.id)
        sequence = subgraph.sequence()
        sequence.add(
            instance.send_event(start_event_message),
            instance.execute_operation(**exec_op_params),
            instance.send_event('Finished operation {0}'.format(operation)))
        subgraphs[instance.id] = subgraph

    # adding tasks dependencies if required
    if run_by_dependency_order:
        for instance in ctx.node_instances:
            for rel in instance.relationships:
                graph.add_dependency(subgraphs[instance.id],
                                     subgraphs[rel.target_id])

    graph.execute()
