from __future__ import annotations

import os

from ansible import constants as C
from ansible.errors import AnsibleParserError, AnsibleUndefinedVariable, AnsibleAssertionError
from ansible.module_utils.common.text.converters import to_native
from ansible.parsing.mod_args import ModuleArgsParser
from ansible.utils.display import Display

display = Display()


def _validate_ds_type(ds, expected_type, error_message):
    """Helper to validate the type of ds."""
    if not isinstance(ds, expected_type):
        raise AnsibleAssertionError(error_message % (ds, type(ds)))


def load_list_of_blocks(ds, play, parent_block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    """
    Given a list of mixed task/block data (parsed from YAML),
    return a list of Block() objects, where implicit blocks
    are created for each bare Task.
    """
    from ansible.playbook.block import Block

    _validate_ds_type(ds, (list, type(None)), '%s should be a list or None but is %s')

    block_list = []
    if ds:
        count = iter(range(len(ds)))
        for i in count:
            block_ds = ds[i]
            implicit_blocks = []
            while block_ds is not None and not Block.is_block(block_ds):
                implicit_blocks.append(block_ds)
                i += 1
                next(count, None)
                try:
                    block_ds = ds[i]
                except IndexError:
                    block_ds = None

            for b in (implicit_blocks, block_ds):
                if b:
                    block_list.append(
                        Block.load(
                            b,
                            play=play,
                            parent_block=parent_block,
                            role=role,
                            task_include=task_include,
                            use_handlers=use_handlers,
                            variable_manager=variable_manager,
                            loader=loader,
                        )
                    )

    return block_list


def _load_task_or_include(task_ds, play, block, role, task_include, use_handlers, variable_manager, loader):
    """Helper to load a task or include object."""
    from ansible.playbook.block import Block
    from ansible.playbook.handler import Handler
    from ansible.playbook.task import Task
    from ansible.playbook.task_include import TaskInclude
    from ansible.playbook.role_include import IncludeRole
    from ansible.playbook.handler_task_include import HandlerTaskInclude
    from ansible.template import Templar

    if 'block' in task_ds:
        if use_handlers:
            raise AnsibleParserError("Using a block as a handler is not supported.", obj=task_ds)
        return Block.load(
            task_ds,
            play=play,
            parent_block=block,
            role=role,
            task_include=task_include,
            use_handlers=use_handlers,
            variable_manager=variable_manager,
            loader=loader,
        )

    args_parser = ModuleArgsParser(task_ds)
    try:
        action, args, delegate_to = args_parser.parse(skip_action_validation=True)
    except AnsibleParserError as e:
        raise AnsibleParserError(to_native(e), obj=task_ds, orig_exc=e) if not e.obj else e

    if action in C._ACTION_ALL_INCLUDE_IMPORT_TASKS:
        include_class = HandlerTaskInclude if use_handlers else TaskInclude
        t = include_class.load(
            task_ds,
            block=block,
            role=role,
            task_include=None,
            variable_manager=variable_manager,
            loader=loader
        )

        if action in C._ACTION_IMPORT_TASKS:
            if t.loop is not None:
                raise AnsibleParserError("You cannot use loops on 'import_tasks' statements. Use 'include_tasks' instead.", obj=task_ds)
            t.statically_loaded = True
            _process_static_import(t, play, block, role, task_include, use_handlers, variable_manager, loader)
            return None  # Handled in _process_static_import

        return t

    elif action in C._ACTION_ALL_PROPER_INCLUDE_IMPORT_ROLES:
        if use_handlers:
            raise AnsibleParserError(f"Using '{action}' as a handler is not supported.", obj=task_ds)

        ir = IncludeRole.load(
            task_ds,
            block=block,
            role=role,
            task_include=None,
            variable_manager=variable_manager,
            loader=loader,
        )

        if action in C._ACTION_IMPORT_ROLE:
            if ir.loop is not None:
                raise AnsibleParserError("You cannot use loops on 'import_role' statements. Use 'include_role' instead.", obj=task_ds)
            ir.statically_loaded = True
            _process_static_role_import(ir, play, variable_manager, loader)
            return None  # Handled in _process_static_role_import

        return ir

    else:
        task_class = Handler if use_handlers else Task
        t = task_class.load(task_ds, block=block, role=role, task_include=task_include, variable_manager=variable_manager, loader=loader)
        if t.action in C._ACTION_META and t.args.get('_raw_params') == "end_role":
            if use_handlers or role is None:
                raise AnsibleParserError("Cannot execute 'end_role' from a handler or outside of a role")
        return t


def _process_static_import(t, play, block, role, task_include, use_handlers, variable_manager, loader):
    """Helper to process static imports."""
    from ansible.template import Templar

    all_vars = variable_manager.get_vars(play=play, task=t)
    templar = Templar(loader=loader, variables=all_vars)

    parent_include = block
    cumulative_path = None
    found = False
    subdir = 'handlers' if use_handlers else 'tasks'

    while parent_include is not None:
        if not isinstance(parent_include, TaskInclude):
            parent_include = parent_include._parent
            continue

        try:
            parent_include_dir = os.path.dirname(templar.template(parent_include.args.get('_raw_params')))
        except AnsibleUndefinedVariable as e:
            if not parent_include.statically_loaded:
                raise AnsibleParserError(
                    "Error when evaluating variable in dynamic parent include path: %s. "
                    "When using static imports, the parent dynamic include cannot utilize host facts "
                    "or variables from inventory" % parent_include.args.get('_raw_params'),
                    obj=task_ds,
                    suppress_extended_error=True,
                    orig_exc=e
                )
            raise

        cumulative_path = os.path.join(parent_include_dir, cumulative_path) if cumulative_path else parent_include_dir
        include_target = templar.template(t.args['_raw_params'])

        if t._role:
            new_basedir = os.path.join(t._role._role_path, subdir, cumulative_path)
            include_file = loader.path_dwim_relative(new_basedir, subdir, include_target)
        else:
            include_file = loader.path_dwim_relative(loader.get_basedir(), cumulative_path, include_target)

        if os.path.exists(include_file):
            found = True
            break
        else:
            parent_include = parent_include._parent

    if not found:
        try:
            include_target = templar.template(t.args['_raw_params'])
        except AnsibleUndefinedVariable as e:
            raise AnsibleParserError(
                "Error when evaluating variable in import path: %s.\n\n"
                "When using static imports, ensure that any variables used in their names are defined in vars/vars_files\n"
                "or extra-vars passed in from the command line. Static imports cannot use variables from facts or inventory\n"
                "sources like group or host vars." % t.args['_raw_params'],
                obj=task_ds,
                suppress_extended_error=True,
                orig_exc=e)

        if t._role:
            include_file = loader.path_dwim_relative(t._role._role_path, subdir, include_target)
        else:
            include_file = loader.path_dwim(include_target)

    data = loader.load_from_file(include_file)
    if not data:
        display.warning('file %s is empty and had no tasks to include' % include_file)
        return
    elif not isinstance(data, list):
        raise AnsibleParserError("included task files must contain a list of tasks", obj=data)

    display.vv("statically imported: %s" % include_file)

    ti_copy = t.copy(exclude_parent=True)
    ti_copy._parent = block
    included_blocks = load_list_of_blocks(
        data,
        play=play,
        parent_block=None,
        task_include=ti_copy,
        role=role,
        use_handlers=use_handlers,
        loader=loader,
        variable_manager=variable_manager,
    )

    tags = ti_copy.tags[:]
    for b in included_blocks:
        b.tags = list(set(b.tags).union(tags))

    if use_handlers:
        for b in included_blocks:
            task_list.extend(b.block)
    else:
        task_list.extend(included_blocks)


def _process_static_role_import(ir, play, variable_manager, loader):
    """Helper to process static role imports."""
    from ansible.template import Templar

    all_vars = variable_manager.get_vars(play=play, task=ir)
    templar = Templar(loader=loader, variables=all_vars)
    ir.post_validate(templar=templar)
    ir._role_name = templar.template(ir._role_name)
    blocks, _ = ir.get_block_list(variable_manager=variable_manager, loader=loader)
    task_list.extend(blocks)


def load_list_of_tasks(ds, play, block=None, role=None, task_include=None, use_handlers=False, variable_manager=None, loader=None):
    """
    Given a list of task datastructures (parsed from YAML),
    return a list of Task() or TaskInclude() objects.
    """
    _validate_ds_type(ds, list, 'The ds (%s) should be a list but was a %s')

    task_list = []
    for task_ds in ds:
        _validate_ds_type(task_ds, dict, 'The ds (%s) should be a dict but was a %s')
        task = _load_task_or_include(task_ds, play, block, role, task_include, use_handlers, variable_manager, loader)
        if task:
            task_list.append(task)

    return task_list


def load_list_of_roles(ds, play, current_role_path=None, variable_manager=None, loader=None, collection_search_list=None):
    """
    Loads and returns a list of RoleInclude objects from the ds list of role definitions.
    """
    from ansible.playbook.role.include import RoleInclude

    _validate_ds_type(ds, list, 'ds (%s) should be a list but was a %s')

    roles = []
    for role_def in ds:
        roles.append(RoleInclude.load(role_def, play=play, current_role_path=current_role_path, variable_manager=variable_manager,
                                     loader=loader, collection_list=collection_search_list))

    return roles